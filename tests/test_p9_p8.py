"""P9 (оплаты, Элкюар, импорт) и P8 (начисления, выплаты): C2, C6, C7, C8, C12, H1, H2, H5, M3."""
import json
from datetime import datetime, timedelta
from decimal import Decimal as D

from django.test import TestCase, override_settings
from django.utils import timezone

from billing import payments as psvc
from billing.models import Charge, CommissionAccrual, FranchiseePayment, Payment, Payout
from billing.services import allocate_overdue, finance_map, franchisee_finance
from clients import branches as bsvc
from clients.models import Branch, ClientSale, Subscription
from core.models import today
from core.testing import api, make_client, make_contract, make_fr, make_product, make_rule, make_user
from network.commission import create_tariff
from network.models import PartnerStatusHistory

V1 = "/api/v1/"


def at(d, h=12):
    return timezone.make_aware(datetime(d.year, d.month, d.day, h))


class Base(TestCase):
    def setUp(self):
        self.product = make_product("coca", payment_source="manual")
        self.osh, self.bsk = make_fr("OZOK Ош", "Ош"), make_fr("OZOK Бишкек", "Бишкек")
        self.admin, self.fin = make_user(role="super_admin"), make_user(role="finance")
        self.fin2 = make_user(role="finance")
        self.owner, self.bowner = make_user(self.osh), make_user(self.bsk)
        self.A, self.FIN, self.FIN2 = api(self.admin), api(self.fin), api(self.fin2)
        self.OWN, self.BSK = api(self.owner), api(self.bowner)
        make_rule(name="Std", product=self.product, partner_tier="standard", sale_kind="subscription", percent=20)
        make_rule(name="Gr", product=self.product, partner_tier="growth", sale_kind="subscription", percent=30)
        for fr in (self.osh, self.bsk):
            make_contract(fr, product=self.product, signed=today() - timedelta(days=200))
        self.bsk.tier = "growth"
        self.bsk.save()
        self.tariff = create_tariff(product=self.product, code="start", name="Старт", monthly_price=1500)
        self.client_ = make_client(self.osh)
        self.branch = bsvc.ensure_main_branch(self.client_)
        self.branch.product, self.branch.tariff, self.branch.elqr_account = self.product, self.tariff, "ELQR-1"
        self.branch.save()
        self.n = 0

    def pay(self, amount=3000, d=None, months=2, ext=None, branch=None, source="manual"):
        self.n += 1
        return psvc.register_payment(branch=branch or self.branch, amount=amount, paid_at=at(d or today()),
                                     source=source, external_id=ext or f"op-{self.n}", months=months, user=self.fin)[0]

    def post_payment(self, who, **kw):
        body = {"branch": self.branch.id, "amount": "3000", "paid_at": at(today()).isoformat(), "months": 2,
                "external_id": "api-1", "source": "manual", "currency": "KGS", **kw}
        return who.post(V1 + "payments/", body, format="json")


class PaymentFlowTests(Base):
    def test_manual_payment_finance_only_creates_payment_sale_accrual(self):
        self.assertEqual(self.post_payment(self.OWN).status_code, 403)
        self.assertEqual(self.post_payment(self.A).status_code, 201)  # super_admin тоже может
        r = self.post_payment(self.FIN, external_id="api-2")
        self.assertEqual(r.status_code, 201, r.content)
        p = Payment.objects.get(external_id="api-2")
        self.assertEqual((p.sale.kind, p.amount, p.source, p.currency if hasattr(p, "currency") else "KGS"),
                         ("renewal", D("3000.00"), "manual", "KGS"))
        acc = CommissionAccrual.objects.get(payment=p)
        self.assertEqual((acc.amount, acc.rate, acc.partner_id, acc.branch_id, acc.status),
                         (D("600.00"), D("20.00"), self.osh.id, self.branch.id, "accrued"))
        self.assertEqual(r.json()["currency"], "KGS")
        sub = Subscription.objects.get(branch=self.branch)
        self.assertEqual((sub.status, sub.paid_until is not None), ("active", True))
        self.assertEqual(self.post_payment(self.FIN, external_id="api-2").status_code, 400)  # дубль
        self.assertEqual(self.post_payment(self.FIN, external_id="x", currency="USD").status_code, 400)

    def test_c6_no_double_counting_in_reports_and_dashboard(self):
        self.pay(3000)
        self.pay(1500, months=1)
        self.assertEqual(ClientSale.objects.filter(kind__in=("subscription", "renewal")).count(), 2)
        self.assertEqual(Payment.objects.count(), 2)
        rep = self.A.get(V1 + "reports/sales/").json()["totals"]
        self.assertEqual(rep["amount"], "4500.00")
        self.assertEqual(rep["commission"], "900.00")
        d = self.A.get(V1 + "dashboard/").json()
        self.assertEqual((d["network_revenue"], d["payments_collected"], d["commissions_accrued"]),
                         ("4500.00", "4500.00", "900.00"))
        self.assertEqual(franchisee_finance(self.osh.id)["commission_due"], D("900.00"))

    def test_legacy_sale_creation_gets_derived_payment_and_accrual(self):
        from clients.services import create_client_sale
        sale, _ = create_client_sale(client=self.client_, product=self.product, kind="subscription", amount=1000)
        self.assertEqual(Payment.objects.filter(sale=sale).count(), 1)
        self.assertEqual(CommissionAccrual.objects.get(sale=sale).amount, D("200.00"))

    def test_h1_rate_at_paid_at_not_import_date(self):
        self.client_.franchisee = self.bsk
        self.branch.implementer_partner = self.branch.responsible_partner = self.bsk
        self.branch.save()
        PartnerStatusHistory.objects.create(partner=self.bsk, field="tier", old="standard", new="growth",
                                            effective_from=today() - timedelta(days=60))
        PartnerStatusHistory.objects.create(partner=self.bsk, field="tier", old="growth", new="standard",
                                            effective_from=today() - timedelta(days=10))
        self.pay(3000, d=today() - timedelta(days=20))
        self.pay(3000, d=today() - timedelta(days=5))
        got = sorted(a.amount for a in CommissionAccrual.objects.filter(partner=self.bsk))
        self.assertEqual(got, [D("600.00"), D("900.00")])

    def test_cancel_payment_reverses_accrual_and_subscription(self):
        p = self.pay(3000)
        r = self.FIN.post(V1 + f"payments/{p.id}/cancel/", {"reason": "Ошибка"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        accs = CommissionAccrual.objects.filter(payment=p)
        self.assertEqual((accs.count(), sum(a.amount for a in accs)), (2, D("0.00")))
        sub = Subscription.objects.get(branch=self.branch)
        self.assertIsNone(sub.paid_until)
        self.assertEqual(sub.status, "awaiting_payment")
        self.assertEqual(Payment.all_objects.get(pk=p.pk).status, "cancelled")
        self.assertEqual(ClientSale.objects.count(), 0)

    def test_refund_partial_reverses_proportionally(self):
        p = self.pay(3000)
        r = self.FIN.post(V1 + f"payments/{p.id}/refund/", {"amount": "1500"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        amounts = sorted(a.amount for a in CommissionAccrual.objects.filter(payment=p))
        self.assertEqual(amounts, [D("-300.00"), D("600.00")])
        p.refresh_from_db()
        self.assertEqual((p.refunded_amount, p.status), (D("1500.00"), "confirmed"))
        self.FIN.post(V1 + f"payments/{p.id}/refund/", {"amount": "1500"}, format="json")
        p.refresh_from_db()
        self.assertEqual(p.status, "refunded")
        self.assertEqual(sum(a.amount for a in CommissionAccrual.objects.filter(payment=p)), D("0.00"))

    def test_periods_endpoint(self):
        sub_id = None
        p = self.pay(3000, months=2)
        sub_id = p.subscription_id
        rows = self.A.get(V1 + f"subscriptions/{sub_id}/periods/").json()["periods"]
        self.assertEqual([r["status"] for r in rows[:2]], ["paid", "paid"])
        self.assertEqual(rows[0]["expected"], "1500.00")
        self.pay(500, months=1, ext="part")
        rows = self.A.get(V1 + f"subscriptions/{sub_id}/periods/").json()["periods"]
        self.assertTrue(len(rows) >= 2)

    def test_deal_confirm_creates_awaiting_subscription_and_payment_status(self):
        # P9: подтверждённая продажа с подпиской создаёт подписку филиала (awaiting_payment)
        make_rule(name="Setup", sale_kind="setup", percent=100)
        self.product.setup_price = 6000
        self.product.save()
        body = {"client": self.client_.id, "product": self.product.id, "branch": self.branch.id,
                "lines": [{"kind": "subscription", "tariff": self.tariff.id, "months": 1},
                          {"kind": "setup"}]}
        r = self.OWN.post(V1 + "deals/", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.OWN.post(V1 + f"deals/{r.json()['id']}/confirm/").status_code, 200)
        sub = Subscription.objects.get(branch=self.branch, product=self.product)
        self.assertEqual(sub.status, "awaiting_payment")
        d = self.OWN.get(V1 + f"deals/{r.json()['id']}/").json()
        self.assertEqual(d["payment_status"], "partial")  # установка оплачена, абонплата — нет
        self.pay(1500, months=1)
        d = self.OWN.get(V1 + f"deals/{r.json()['id']}/").json()
        self.assertEqual(d["payment_status"], "paid")


class C7AuthorityTests(Base):
    def test_non_authoritative_manual_rejected_default_product_source(self):
        self.product.payment_source = "product"
        self.product.save()
        r = self.FIN.post(V1 + "payments/", {"branch": self.branch.id, "amount": "3000", "months": 2,
                                             "paid_at": at(today()).isoformat(), "external_id": "m1"},
                          format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Payment.objects.count(), 0)

    def test_product_event_creates_payment_only_when_product_is_authoritative(self):
        self.product.payment_source = "product"
        self.product.integration_enabled = True
        self.product.save()
        sub = Subscription.objects.create(client=self.client_, branch=self.branch, product=self.product,
                                          external_id="c1")
        ev = {"event_id": "e1", "type": "payment.succeeded", "external_payment_id": "pp1", "external_client_id": "c1",
              "amount": "3000", "months": 2, "paid_until": str(today() + timedelta(days=60)),
              "paid_at": timezone.now().isoformat()}
        r = api().post(V1 + "integrations/events/", ev, format="json", HTTP_X_OZOK_KEY=self.product.api_key)
        self.assertEqual(r.json()["results"][0]["status"], "processed")
        p = Payment.objects.get()
        self.assertEqual((p.source, p.external_id, p.subscription_id), ("product", "pp1", sub.id))
        self.assertEqual(ClientSale.objects.count(), 1)
        self.assertEqual(CommissionAccrual.objects.count(), 1)

    def test_product_event_only_reconciles_when_elqr_is_authoritative(self):
        self.product.payment_source = "elqr"
        self.product.integration_enabled = True
        self.product.save()
        Subscription.objects.create(client=self.client_, branch=self.branch, product=self.product, external_id="c1")
        ev = {"event_id": "e1", "type": "payment.succeeded", "external_payment_id": "pp1", "external_client_id": "c1",
              "amount": "3000", "months": 2, "paid_until": str(today() + timedelta(days=60))}
        r = api().post(V1 + "integrations/events/", ev, format="json", HTTP_X_OZOK_KEY=self.product.api_key)
        self.assertEqual(r.json()["results"][0]["status"], "processed")
        self.assertEqual((Payment.objects.count(), CommissionAccrual.objects.count()), (0, 0))

    @override_settings(ELQR_WEBHOOK_SECRET="s3cret")
    def test_elqr_webhook_hmac_dedupe_and_retry(self):
        self.product.payment_source = "elqr"
        self.product.save()
        body = {"event_id": "q1", "type": "payment.succeeded", "account": "ELQR-1", "operation_id": "op-77",
                "amount": "3000", "paid_at": timezone.now().isoformat(), "months": 2}
        raw = json.dumps(body).encode()
        url = V1 + "integrations/elqr/webhook/"
        bad = api().post(url, raw, content_type="application/json", HTTP_X_ELQR_SIGNATURE="deadbeef")
        self.assertEqual(bad.status_code, 401)
        sig = psvc.sign("s3cret", raw)
        ok = api().post(url, raw, content_type="application/json", HTTP_X_ELQR_SIGNATURE=sig)
        self.assertEqual((ok.status_code, ok.json()["status"]), (200, "processed"))
        again = api().post(url, raw, content_type="application/json", HTTP_X_ELQR_SIGNATURE=sig)
        self.assertEqual(again.json()["status"], "duplicate")
        self.assertEqual(Payment.objects.filter(source="elqr").count(), 1)
        # неизвестный счёт -> unmatched -> привязали -> retry
        body2 = {**body, "event_id": "q2", "account": "NEW-9", "operation_id": "op-78"}
        raw2 = json.dumps(body2).encode()
        r = api().post(url, raw2, content_type="application/json", HTTP_X_ELQR_SIGNATURE=psvc.sign("s3cret", raw2))
        self.assertEqual(r.json()["status"], "unmatched")
        from integrations.models import IntegrationEvent
        ev = IntegrationEvent.objects.get(external_id="q2")
        self.assertEqual(ev.provider, "elqr")
        other = Branch.objects.create(client=self.client_, name="Ф2", product=self.product, elqr_account="NEW-9",
                                      implementer_partner=self.osh, responsible_partner=self.osh)
        rr = self.A.post(V1 + f"integration-events/{ev.id}/retry/")
        self.assertEqual((rr.status_code, rr.json()["status"]), (200, "processed"))
        self.assertEqual(Payment.objects.filter(branch=other).count(), 1)
        self.assertEqual(self.A.post(V1 + f"integration-events/{ev.id}/retry/").status_code, 400)

    def test_webhook_not_configured_is_503(self):
        r = api().post(V1 + "integrations/elqr/webhook/", {"event_id": "1"}, format="json")
        self.assertEqual(r.status_code, 503)


class BankImportTests(Base):
    CSV = ("date,amount,purpose,operation_id,account\n"
           "2026-11-10,3000,Абонплата,B-1,ELQR-1\n"
           "2026-11-10,3000,Абонплата,B-1,ELQR-1\n"
           "2026-11-11,1500,Абонплата,B-2,NOPE\n"
           "2026-11-11,xx,Абонплата,B-3,ELQR-1\n")

    def test_preview_then_confirm_with_duplicates_and_not_found(self):
        r = self.FIN.post(V1 + "payments/import/preview/", {"csv": self.CSV}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        s = r.json()["summary"]
        self.assertEqual((s["new"], s["duplicate"], s["not_found"], s["invalid"]), (1, 1, 1, 1))
        self.assertEqual(Payment.objects.count(), 0)
        c = self.FIN.post(V1 + "payments/import/", {"csv": self.CSV}, format="json")
        self.assertEqual(c.status_code, 200, c.content)
        self.assertEqual(c.json()["summary"]["created"], 1)
        p = Payment.objects.get()
        self.assertEqual((p.source, p.external_id, p.months), ("bank_import", "B-1", 2))
        # повторный импорт — всё дубли
        again = self.FIN.post(V1 + "payments/import/", {"csv": self.CSV}, format="json").json()["summary"]
        self.assertEqual(again.get("created", 0), 0)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(self.OWN.post(V1 + "payments/import/preview/", {"csv": self.CSV},
                                       format="json").status_code, 403)

    def test_import_reconciles_when_product_authoritative(self):
        self.product.payment_source = "product"
        self.product.save()
        sub = Subscription.objects.create(client=self.client_, branch=self.branch, product=self.product,
                                          external_id="c1")
        psvc.register_payment(branch=self.branch, subscription=sub, amount=3000, paid_at=at(today()),
                              source="product", external_id="B-1", months=2, check_authority=False)
        csv = f"date,amount,operation_id,account\n{today()},3000,B-1,ELQR-1\n{today()},999,B-9,ELQR-1\n"
        res = self.FIN.post(V1 + "payments/import/", {"csv": csv}, format="json").json()["summary"]
        self.assertEqual((res.get("reconciled"), res.get("not_authoritative")), (1, 1))
        self.assertIsNotNone(Payment.objects.get().reconciled_at)
        self.assertEqual(Payment.objects.count(), 1)


class AccrualTests(Base):
    def test_h2_suspended_on_hold_then_release(self):
        self.osh.status = "suspended"
        self.osh.save()
        p = self.pay(3000)
        acc = CommissionAccrual.objects.get(payment=p)
        self.assertEqual((acc.status, acc.amount), ("on_hold", D("600.00")))
        self.assertEqual(franchisee_finance(self.osh.id)["commission_due"], D("0.00"))
        self.assertEqual(self.OWN.post(V1 + f"commission-accruals/{acc.id}/release/").status_code, 403)
        r = self.FIN.post(V1 + f"commission-accruals/{acc.id}/release/")
        self.assertEqual((r.status_code, r.json()["status"]), (200, "accrued"))
        self.assertEqual(franchisee_finance(self.osh.id)["commission_due"], D("600.00"))

    def test_h2_hold_can_be_cancelled_with_reason(self):
        self.osh.status = "suspended"
        self.osh.save()
        acc = CommissionAccrual.objects.get(payment=self.pay(3000))
        self.assertEqual(self.FIN.post(V1 + f"commission-accruals/{acc.id}/cancel/", {}, format="json").status_code, 400)
        self.assertEqual(self.FIN.post(V1 + f"commission-accruals/{acc.id}/cancel/", {"reason": "Расторжение"},
                                       format="json").status_code, 200)
        self.assertEqual(CommissionAccrual.objects.count(), 0)

    def test_manager_gets_no_accrual(self):
        self.osh.status = "manager"
        self.osh.save()
        self.pay(3000)
        self.assertEqual(CommissionAccrual.objects.count(), 0)

    def test_c8_payee_by_paid_at_and_reversal_to_original_partner(self):
        old = self.pay(3000, d=today() - timedelta(days=10))
        bsvc.reassign(self.branch, self.bsk, reason_code="other", reason="Передача", user=self.admin,
                      effective_from=today() - timedelta(days=3))
        self.branch.refresh_from_db()
        before = self.pay(3000, d=today() - timedelta(days=5))   # импорт позже, но дата до перехода
        after = self.pay(3000, d=today() - timedelta(days=1))
        self.assertEqual(CommissionAccrual.objects.get(payment=before).partner_id, self.osh.id)
        self.assertEqual(CommissionAccrual.objects.get(payment=after).partner_id, self.bsk.id)
        self.assertEqual(CommissionAccrual.objects.get(payment=after).amount, D("900.00"))
        # возврат по старому платежу — сторно партнёру исходного начисления (C8)
        self.FIN.post(V1 + f"payments/{old.id}/refund/", {"amount": "3000"}, format="json")
        rev = CommissionAccrual.objects.get(payment=old, reversal_of__isnull=False)
        self.assertEqual((rev.partner_id, rev.amount), (self.osh.id, D("-600.00")))

    def test_h10_implementer_not_responsible_no_accrual(self):
        self.branch.responsible_partner = self.bsk
        self.branch.save()
        p = self.pay(3000)
        self.assertEqual(CommissionAccrual.objects.count(), 0)
        self.assertEqual(Payment.objects.get(pk=p.pk).sale.commission_amount, 0)

    def test_closed_partner_gets_nothing_after_closed_at(self):
        closed = today() - timedelta(days=4)
        r = self.A.post(V1 + f"franchisees/{self.osh.id}/close/", {
            "closed_at": str(closed), "reason": "Уход", "transfer_to_partner": self.bsk.id}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.pay(3000, d=closed - timedelta(days=1))
        self.pay(3000, d=closed + timedelta(days=1))
        self.assertEqual(sorted((a.partner_id, a.amount) for a in CommissionAccrual.objects.all()),
                         sorted([(self.osh.id, D("600.00")), (self.bsk.id, D("900.00"))]))
        # докаты: начисленное до закрытия остаётся к выплате (H3)
        self.assertEqual(franchisee_finance(self.osh.id)["commission_due"], D("600.00"))

    def test_isolation_c9_accruals_and_payments(self):
        self.pay(3000)
        b2 = Branch.objects.create(client=self.client_, name="Ф2", product=self.product, elqr_account="E2",
                                   implementer_partner=self.bsk, responsible_partner=self.bsk)
        psvc.register_payment(branch=b2, amount=3000, paid_at=at(today()), source="manual", external_id="b2-1",
                              months=2, user=self.fin)
        mine = self.OWN.get(V1 + "payments/").json()["results"]
        self.assertEqual(len(mine), 1)
        self.assertEqual([a["partner"] for a in self.OWN.get(V1 + "commission-accruals/").json()["results"]],
                         [self.osh.id])
        self.assertEqual(len(self.BSK.get(V1 + "payments/").json()["results"]), 1)
        self.assertEqual(self.BSK.get(V1 + f"payments/{mine[0]['id']}/").status_code, 404)

    def test_summary_endpoint(self):
        p = self.pay(3000)
        self.FIN.post(V1 + f"payments/{p.id}/refund/", {"amount": "1500"}, format="json")
        s = self.OWN.get(V1 + "commission-accruals/summary/").json()
        self.assertEqual((s["accrued"], s["to_pay"]), ("300.00", "300.00"))
        self.assertEqual(len(s["corrections"]), 1)


class PayoutTests(Base):
    def test_payout_flow_four_eyes_and_commission_due(self):
        self.pay(3000)
        self.pay(1500, months=1)
        period = today().strftime("%Y-%m")
        r = self.FIN.post(V1 + "payouts/", {"partner": self.osh.id, "period": period}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        po = r.json()
        self.assertEqual((po["amount"], po["status"]), ("900.00", "draft"))
        self.assertEqual(self.FIN.post(V1 + f"payouts/{po['id']}/pay/").status_code, 400)  # нельзя без проверки
        self.assertEqual(self.FIN.post(V1 + f"payouts/{po['id']}/verify/").status_code, 200)
        self.assertEqual(self.FIN.post(V1 + f"payouts/{po['id']}/pay/").status_code, 400)  # verified_by == paid_by
        r = self.FIN2.post(V1 + f"payouts/{po['id']}/pay/", {"payment_ref": "ПП-1"}, format="json")
        self.assertEqual((r.status_code, r.json()["status"]), (200, "paid"))
        self.assertEqual(set(CommissionAccrual.objects.values_list("status", flat=True)), {"paid"})
        self.assertEqual(franchisee_finance(self.osh.id)["commission_due"], D("0.00"))
        self.assertEqual(self.OWN.post(V1 + "payouts/", {"partner": self.osh.id, "period": period},
                                       format="json").status_code, 403)
        self.assertEqual(len(self.OWN.get(V1 + "payouts/").json()["results"]), 1)

    def test_h5_paid_stays_paid_negative_carried_forward(self):
        p = self.pay(3000)
        period = today().strftime("%Y-%m")
        po = Payout.objects.get(pk=self.FIN.post(V1 + "payouts/", {"partner": self.osh.id, "period": period},
                                                 format="json").json()["id"])
        self.FIN.post(V1 + f"payouts/{po.id}/verify/")
        self.FIN2.post(V1 + f"payouts/{po.id}/pay/")
        # возврат после выплаты: исходное начисление остаётся paid, сторно — отдельная отрицательная запись
        self.FIN.post(V1 + f"payments/{p.id}/refund/", {"amount": "3000"}, format="json")
        orig = CommissionAccrual.objects.get(payment=p, reversal_of__isnull=True)
        rev = CommissionAccrual.objects.get(payment=p, reversal_of__isnull=False)
        self.assertEqual((orig.status, rev.status, rev.amount), ("paid", "accrued", D("-600.00")))
        self.assertEqual(franchisee_finance(self.osh.id)["commission_due"], D("-600.00"))
        # отрицательную сумму не платим — остаток переносится
        r = self.FIN.post(V1 + "payouts/", {"partner": self.osh.id, "period": period}, format="json")
        self.assertEqual(r.status_code, 400)
        # новый платёж гасит минус: выплачивается нетто
        self.pay(4500, months=3)
        r = self.FIN.post(V1 + "payouts/", {"partner": self.osh.id, "period": period}, format="json")
        self.assertEqual((r.status_code, r.json()["amount"]), (201, "300.00"))  # 900 − 600

    def test_cancel_payout_returns_accruals(self):
        self.pay(3000)
        po = self.FIN.post(V1 + "payouts/", {"partner": self.osh.id, "period": today().strftime("%Y-%m")},
                           format="json").json()
        self.assertEqual(self.FIN.post(V1 + f"payouts/{po['id']}/cancel/", {"reason": "Ошибка"},
                                       format="json").status_code, 200)
        self.assertEqual(CommissionAccrual.objects.get().status, "accrued")
        self.assertEqual(self.FIN.post(V1 + f"payouts/{po['id']}/cancel/", {"reason": "x"},
                                       format="json").status_code, 400)


class WithholdingAndAllocationTests(TestCase):
    """C2, C12, C6: удержание из абонплаты и распределение платежей по начислениям."""

    def setUp(self):
        self.product = make_product("coca", payment_source="manual")
        self.fr = make_fr()
        make_rule(name="R", sale_kind="subscription", percent=20)

    def test_c12_hybrid_creates_no_due_fee_charge_for_sales_part(self):
        c = make_contract(self.fr, product=self.product, scheme="hybrid", fee=D("10000"), upfront_amount=D("3000"),
                          installments_total=D("2000"), installments_count=2, withhold_percent=30)
        rows = sorted((ch.kind, ch.amount, ch.due_date is None) for ch in Charge.objects.filter(contract=c))
        self.assertIn(("fee", D("5000.00"), True), rows)
        self.assertEqual(sum(ch.amount for ch in Charge.objects.filter(contract=c)), D("10000.00"))

    def test_c2_offsets_do_not_mask_overdue_withhold_charge(self):
        make_contract(self.fr, product=self.product, scheme="per_sale", fee=D("20000"), commission=20)
        client = make_client(self.fr)
        from clients.services import create_client_sale
        make_rule(name="S", sale_kind="setup", percent=100)
        create_client_sale(client=client, product=self.product, kind="setup", amount=D("10000"),
                           date=today() - timedelta(days=60))
        wh = Charge.objects.get(kind="withhold")
        self.assertTrue(wh.due_date < today())
        f = franchisee_finance(self.fr.id)
        self.assertEqual(f["overdue"], wh.amount)  # зачёт гасит только часть «из продаж»
        FranchiseePayment.objects.create(franchisee=self.fr, amount=wh.amount, method="bank")
        self.assertEqual(franchisee_finance(self.fr.id)["overdue"], D("0.00"))

    def test_allocate_overdue_fifo_unit(self):
        class C:
            def __init__(s, amount, due):
                s.amount, s.due_date = D(amount), due
        t = today()
        ch = [C(100, t - timedelta(days=20)), C(100, t - timedelta(days=10)), C(500, None), C(100, t + timedelta(days=5))]
        self.assertEqual(allocate_overdue(ch, D(0), D(150), t), D(50))
        self.assertEqual(allocate_overdue(ch, D(400), D(0), t), D(200))  # зачёты не трогают просроченные

    def test_c6_withheld_on_accrual_not_in_commission_payouts(self):
        from billing.models import CommissionPayout
        make_contract(self.fr, product=self.product, scheme="per_sale", fee=D("1000"), commission=20)
        client = make_client(self.fr)
        branch = bsvc.ensure_main_branch(client)
        branch.product = self.product
        branch.save()
        psvc.register_payment(branch=branch, amount=10000, paid_at=at(today()), source="manual",
                              external_id="w1", months=1)
        acc = CommissionAccrual.objects.get()
        self.assertEqual((acc.amount, acc.withheld_amount), (D("2000.00"), D("600.00")))
        self.assertEqual(CommissionPayout.objects.count(), 0)
        self.assertEqual(franchisee_finance(self.fr.id)["commission_due"], D("1400.00"))


class Regression500Tests(TestCase):
    def test_client_sales_list_with_page_ordering_and_dates_is_200(self):
        """Регресс: GET client-sales/?page&page_size&ordering=-date&date_from&date_to падал с 500."""
        admin = make_user(role="super_admin")
        product = make_product("coca")
        fr = make_fr()
        from clients.services import create_client_sale
        create_client_sale(client=make_client(fr), product=product, kind="setup", amount=100)
        r = api(admin).get(V1 + "client-sales/", {"page": 1, "page_size": 20, "ordering": "-date",
                                                  "date_from": "2020-01-01", "date_to": "2099-11-30"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["count"], 1)
