"""Регрессионные тесты по замечаниям ревью: удаление данных, устойчивость интеграции, троттлинг, SLA, права."""
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.conf import settings
from django.db.models import ProtectedError
from django.test import TestCase
from django.utils import timezone

from billing.models import CommissionPayout, FranchiseePayment
from billing.services import franchisee_finance
from clients.models import ClientProductAccount, ClientSale, Implementation
from clients.services import create_client_sale, set_commission_percent
from core.exceptions import BusinessError
from core.models import add_months, today
from core.testing import api, days_ago, make_client, make_contract, make_fr, make_product, make_user
from leads.services import create_lead
from quality.services import create_ticket

D = Decimal
EVENTS = "/api/v1/integrations/events/"


class ClientDeletionTests(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.fr = make_fr()
        self.owner = api(make_user(self.fr, "franchisee"))
        self.admin = api(make_user(role="super_admin"))

    def test_client_with_sales_cannot_be_deleted(self):
        client = make_client(self.fr)
        create_client_sale(client=client, product=self.market, kind="setup", amount=1000)
        r = self.owner.delete(f"/api/v1/clients/{client.id}/")
        self.assertEqual(r.status_code, 403)
        r = self.admin.delete(f"/api/v1/clients/{client.id}/")
        self.assertEqual(r.status_code, 400)
        self.assertIn("is_active", r.json()["detail"])
        self.assertEqual(ClientSale.objects.count(), 1)
        with self.assertRaises(ProtectedError):
            client.delete()  # PROTECT на уровне модели

    def test_client_with_accounts_cannot_be_deleted(self):
        client = make_client(self.fr)
        ClientProductAccount.objects.create(client=client, product=self.market, external_id="x")
        self.assertEqual(self.owner.delete(f"/api/v1/clients/{client.id}/").status_code, 403)
        self.assertEqual(self.admin.delete(f"/api/v1/clients/{client.id}/").status_code, 400)
        self.assertEqual(ClientProductAccount.objects.count(), 1)

    def test_empty_client_can_be_deleted_and_deactivation_works(self):
        client = make_client(self.fr)
        self.assertEqual(self.owner.delete(f"/api/v1/clients/{client.id}/").status_code, 204)
        client = make_client(self.fr)
        create_client_sale(client=client, product=self.market, kind="setup", amount=10)
        r = self.owner.patch(f"/api/v1/clients/{client.id}/", {"is_active": False}, format="json")
        self.assertEqual((r.status_code, r.json()["is_active"]), (200, False))


class SaleDeletionTests(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.fr = make_fr()
        self.client_ = make_client(self.fr)
        make_contract(self.fr, product=self.market, commission=20)
        self.owner_user = make_user(self.fr, "franchisee")
        self.owner = api(self.owner_user)
        self.manager = api(make_user(self.fr, "franchisee_staff"))
        self.admin = api(make_user(role="super_admin"))

    def body(self, kind):
        return {"client": self.client_.id, "product": self.market.id, "kind": kind, "amount": "5000"}

    def cancel(self, who, sale_id, reason="Ошибка ввода"):
        return who.post(f"/api/v1/client-sales/{sale_id}/cancel/", {"reason": reason}, format="json")

    def test_delete_is_not_allowed_cancel_instead(self):
        own = self.owner.post("/api/v1/client-sales/", self.body("setup"), format="json").json()["id"]
        self.assertEqual(self.owner.delete(f"/api/v1/client-sales/{own}/").status_code, 405)
        self.assertEqual(self.cancel(self.owner, own, "").status_code, 400)  # причина обязательна
        r = self.cancel(self.owner, own)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["cancelled_at"])
        self.assertTrue(ClientSale.all_objects.filter(pk=own).exists())  # физически не удалена
        self.assertEqual(ClientSale.objects.filter(pk=own).count(), 0)
        self.assertEqual(self.cancel(self.owner, own).status_code, 400)  # повторно нельзя
        svc = self.owner.post("/api/v1/client-sales/", self.body("other"), format="json").json()["id"]
        self.assertEqual(self.cancel(self.manager, svc).status_code, 200)  # сотрудник своего партнёра

    def test_franchisee_cannot_cancel_nbs_entered_or_commission_sales(self):
        hq_license = self.admin.post("/api/v1/client-sales/", self.body("setup"), format="json").json()["id"]
        hq_sub = self.admin.post("/api/v1/client-sales/", self.body("subscription"), format="json").json()["id"]
        integrated, _ = create_client_sale(client=self.client_, product=self.market, kind="renewal", amount=1000,
                                           source="integration", external_payment_id="p1")
        for sale_id in (hq_license, hq_sub, integrated.id):
            self.assertEqual(self.cancel(self.owner, sale_id).status_code, 403, sale_id)
        self.assertEqual(ClientSale.objects.count(), 3)
        for sale_id in (hq_license, hq_sub, integrated.id):
            self.assertEqual(self.cancel(self.admin, sale_id).status_code, 200)
        self.assertEqual(ClientSale.objects.count(), 0)
        self.assertEqual(ClientSale.all_objects.count(), 3)

    def test_sale_without_creator_is_nbs_only(self):
        sale, _ = create_client_sale(client=self.client_, product=self.market, kind="setup", amount=10)
        self.assertEqual(self.cancel(self.owner, sale.id).status_code, 403)

    def test_cannot_cancel_sale_with_refund(self):
        sale, _ = create_client_sale(client=self.client_, product=self.market, kind="subscription", amount=1000,
                                     source="integration", external_payment_id="p1")
        from clients.services import refund_sale
        refund_sale(sale, 100, external_payment_id="p1:refund:x")
        self.assertEqual(self.cancel(self.admin, sale.id).status_code, 400)


class IntegrationRobustnessTests(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.fr = make_fr(code="OSH-01")
        self.n = 0

    def post(self, data, key=None):
        return api().post(EVENTS, data, format="json", HTTP_X_OZOK_KEY=key or self.market.api_key)

    def ev(self, kind, **kw):
        self.n += 1
        return {"event_id": f"r{self.n}", "type": kind, **kw}

    def test_impossible_dates_do_not_crash_or_abort_batch(self):
        upsert = self.ev("client.upsert", external_client_id="c1", partner_code="OSH-01", business_name="Магазин")
        good_pay = dict(external_payment_id="ok-1", external_client_id="c1", amount="1000", months=1,
                        paid_until=add_months(today(), 1).isoformat(), paid_at=timezone.now().isoformat())
        batch = [
            upsert,
            self.ev("payment.succeeded", **{**good_pay, "external_payment_id": "bad-1", "paid_until": "2026-02-30"}),
            self.ev("payment.succeeded", **{**good_pay, "external_payment_id": "bad-2", "paid_at": "2026-13-45"}),
            self.ev("payment.succeeded", **{**good_pay, "external_payment_id": "bad-3", "months": [1]}),
            self.ev("payment.succeeded", **{**good_pay, "external_payment_id": "bad-4", "amount": {"a": 1}}),
            self.ev("client.upsert", external_client_id="c1", partner_code="OSH-01", connected_at="2026-02-30"),
            self.ev("payment.succeeded", **good_pay),
        ]
        r = self.post(batch)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([x["status"] for x in r.json()["results"]],
                         ["processed", "error", "error", "error", "error", "error", "processed"])
        self.assertEqual(ClientSale.objects.count(), 1)
        from integrations.models import IntegrationEvent
        self.assertEqual(IntegrationEvent.objects.count(), 7)  # каждое событие записано в журнал

    def test_unexpected_exception_is_isolated(self):
        from unittest import mock
        self.post(self.ev("client.upsert", external_client_id="c1", partner_code="OSH-01", business_name="М"))
        pay = self.ev("payment.succeeded", external_payment_id="p1", external_client_id="c1", amount="10",
                      months=1, paid_until=today().isoformat())
        with mock.patch("clients.services.create_client_sale", side_effect=RuntimeError("boom")):
            r = self.post([pay, self.ev("client.upsert", external_client_id="c2", partner_code="OSH-01",
                                        business_name="Второй")])
        self.assertEqual([x["status"] for x in r.json()["results"]], ["error", "processed"])

    def test_inactive_product_is_rejected(self):
        self.market.is_active = False
        self.market.save()
        self.assertEqual(self.post(self.ev("client.upsert", external_client_id="c", partner_code="OSH-01")).status_code,
                         401)
        self.assertEqual(api().get("/api/v1/integrations/ping/", HTTP_X_OZOK_KEY=self.market.api_key).status_code, 401)


class RefundTests(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.fr = make_fr(code="OSH-01")
        self.n = 0
        self.post(dict(self.ev("client.upsert"), external_client_id="c1", partner_code="OSH-01", business_name="М"))

    def post(self, data):
        return api().post(EVENTS, data, format="json", HTTP_X_OZOK_KEY=self.market.api_key)

    def ev(self, kind="payment.refunded", **kw):
        self.n += 1
        return {"event_id": f"x{self.n}", "type": kind, **kw}

    def pay(self, pid, amount="10000", months=1, until=None):
        return self.post(self.ev("payment.succeeded", external_payment_id=pid, external_client_id="c1",
                                 amount=amount, months=months,
                                 paid_until=(until or add_months(today(), months)).isoformat()))

    def refund(self, pid="p1", amount="1000", **kw):
        r = self.post(self.ev(external_payment_id=pid, amount=amount, **kw))
        return r.json()["results"][0]["status"]

    def test_multiple_partial_refunds_are_all_processed(self):
        make_contract(self.fr, product=self.market, commission=20)
        self.pay("p1")
        self.assertEqual([self.refund(amount="3000"), self.refund(amount="3000"), self.refund(amount="3000")],
                         ["processed"] * 3)
        refunds = ClientSale.objects.filter(amount__lt=0)
        self.assertEqual((refunds.count(), sum(r.amount for r in refunds)), (3, D("-9000.00")))
        self.assertEqual(franchisee_finance(self.fr.id)["commission_due"], D("200.00"))  # 2000 - 1800
        self.assertEqual(self.refund(amount="1000"), "processed")  # ровно до суммы платежа
        self.assertEqual(franchisee_finance(self.fr.id)["commission_due"], D("0.00"))

    def test_cumulative_refund_cannot_exceed_payment(self):
        self.pay("p1", amount="1000")
        self.assertEqual(self.refund(amount="600"), "processed")
        self.assertEqual(self.refund(amount="600"), "error")
        self.assertEqual(self.refund(amount="400"), "processed")
        self.assertEqual(self.refund(amount="1"), "error")

    def test_dedupe_by_event_id_and_refund_id(self):
        self.pay("p1")
        first = self.ev(external_payment_id="p1", amount="100", external_refund_id="r-77")
        self.assertEqual(self.post(first).json()["results"][0]["status"], "processed")
        self.assertEqual(self.post(first).json()["results"][0]["status"], "duplicate")  # тот же event_id
        again = self.ev(external_payment_id="p1", amount="100", external_refund_id="r-77")
        self.assertEqual(self.post(again).json()["results"][0]["status"], "duplicate")  # тот же refund id
        self.assertEqual(ClientSale.objects.filter(amount__lt=0).count(), 1)

    def test_offset_reversal_never_exceeds_original_offset(self):
        contract = make_contract(self.fr, product=self.market, scheme="per_sale", fee=D("5000"), commission=20)
        self.pay("p1", amount="10000")  # доход 2000, удержано 600
        for amount in ("3333.33", "3333.33", "3333.34"):  # неровные доли: округление не должно давать лишнего
            self.assertEqual(self.refund(amount=amount), "processed")
        reversed_total = sum(p.amount for p in FranchiseePayment.objects.filter(contract=contract, amount__lt=0))
        self.assertEqual(reversed_total, D("-600.00"))
        self.assertEqual(sum(p.amount for p in FranchiseePayment.objects.filter(contract=contract)), D("0.00"))
        self.assertEqual(sum(p.amount for p in CommissionPayout.objects.all()), D("0.00"))
        f = franchisee_finance(self.fr.id)
        self.assertEqual((f["balance"], f["commission_due"]), (D("5000.00"), D("0.00")))

    def test_full_refund_rolls_back_paid_until(self):
        u1, u2 = add_months(today(), 1), add_months(today(), 4)
        self.pay("p1", months=1, until=u1)
        self.pay("p2", months=3, until=u2)
        account = ClientProductAccount.objects.get()
        self.assertEqual(account.paid_until, u2)
        self.assertEqual(self.refund("p2", "10000"), "processed")
        account.refresh_from_db()
        self.assertEqual(account.paid_until, u1)
        self.assertEqual(self.refund("p1", "4000"), "processed")  # частичный возврат срок не трогает
        account.refresh_from_db()
        self.assertEqual(account.paid_until, u1)
        self.assertEqual(self.refund("p1", "6000"), "processed")
        account.refresh_from_db()
        self.assertIsNone(account.paid_until)

    def test_commission_percent_edit_rejected_on_refunds_and_refunded_sales(self):
        make_contract(self.fr, product=self.market, commission=20)
        self.pay("p1")
        self.assertEqual(self.refund(amount="1000"), "processed")
        original = ClientSale.objects.get(external_payment_id="p1")
        refund = ClientSale.objects.get(amount__lt=0)
        acc = api(make_user(role="finance"))
        for sale in (original, refund):
            r = acc.patch(f"/api/v1/client-sales/{sale.id}/", {"commission_percent": "30"}, format="json")
            self.assertEqual(r.status_code, 400, sale.id)
            self.assertIn("detail", r.json() if isinstance(r.json(), dict) else {})
            with self.assertRaises(BusinessError):
                set_commission_percent(sale, 30)
        original.refresh_from_db()
        self.assertEqual(original.commission_percent, D("20"))
        # продажа без возвратов правится как раньше
        fresh, _ = create_client_sale(client=original.client, product=self.market, kind="renewal", amount=100)
        self.assertEqual(acc.patch(f"/api/v1/client-sales/{fresh.id}/", {"commission_percent": "30"},
                                   format="json").status_code, 200)


class ThrottleTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_num_proxies_zero_by_default(self):
        self.assertEqual(settings.REST_FRAMEWORK["NUM_PROXIES"], 0)

    def test_x_forwarded_for_cannot_bypass_public_lead_throttle(self):
        codes = []
        for i in range(12):
            r = api().post("/api/v1/leads/public/", {"name": "Айбек", "phone": "0700 1", "interest": "consult"},
                           format="json", HTTP_X_FORWARDED_FOR=f"10.0.0.{i}, 203.0.113.{i}")
            codes.append(r.status_code)
        self.assertEqual(codes[:10], [201] * 10)
        self.assertEqual(codes[10:], [429, 429])


class TicketGamingTests(TestCase):
    def setUp(self):
        self.fr = make_fr()
        self.client_ = make_client(self.fr)
        self.owner = api(make_user(self.fr, "franchisee"))
        self.admin = api(make_user(role="super_admin"))

    def create(self, who, **extra):
        body = {"client": self.client_.id, "subject": "Касса", "priority": "high", **extra}
        return who.post("/api/v1/tickets/", body, format="json")

    def test_opened_at_is_server_set_for_franchisee(self):
        long_ago = (timezone.now() - timedelta(days=30)).isoformat()
        r = self.create(self.owner, opened_at=long_ago)
        self.assertEqual(r.status_code, 201, r.content)
        opened = timezone.datetime.fromisoformat(r.json()["opened_at"].replace("Z", "+00:00"))
        self.assertLess(abs((timezone.now() - opened).total_seconds()), 60)
        r2 = self.owner.patch(f"/api/v1/tickets/{r.json()['id']}/", {"opened_at": long_ago}, format="json")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["opened_at"], r.json()["opened_at"])

    def test_super_admin_may_set_opened_at_on_create_only(self):
        past = timezone.now() - timedelta(days=2)
        r = self.create(self.admin, opened_at=past.isoformat())
        self.assertEqual(r.status_code, 201)
        self.assertGreater((timezone.now() - timezone.datetime.fromisoformat(
            r.json()["opened_at"].replace("Z", "+00:00"))).total_seconds(), 86400)
        r2 = self.admin.patch(f"/api/v1/tickets/{r.json()['id']}/", {"opened_at": timezone.now().isoformat()},
                              format="json")
        self.assertEqual(r2.json()["opened_at"], r.json()["opened_at"])

    def test_priority_locked_for_franchisee_after_creation(self):
        r = self.create(self.owner, priority="critical")
        self.assertEqual((r.json()["priority"], r.json()["sla_hours"]), ("critical", 1))
        tid = r.json()["id"]
        r2 = self.owner.patch(f"/api/v1/tickets/{tid}/", {"priority": "low", "subject": "Новая тема"}, format="json")
        self.assertEqual((r2.json()["priority"], r2.json()["subject"]), ("critical", "Новая тема"))
        r3 = self.admin.patch(f"/api/v1/tickets/{tid}/", {"priority": "low"}, format="json")
        self.assertEqual(r3.json()["priority"], "low")

    def test_ticket_delete_is_super_admin_only(self):
        t = create_ticket(franchisee=self.fr, client=self.client_, subject="Т")
        self.assertEqual(self.owner.delete(f"/api/v1/tickets/{t.id}/").status_code, 403)
        self.assertEqual(api(make_user(role="network_head")).delete(f"/api/v1/tickets/{t.id}/").status_code, 403)
        self.assertEqual(api(make_user(self.fr, "franchisee_staff")).delete(f"/api/v1/tickets/{t.id}/").status_code, 403)
        self.assertEqual(self.admin.delete(f"/api/v1/tickets/{t.id}/").status_code, 204)


class ManagerVisibilityTests(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.fr = make_fr()
        make_contract(self.fr, product=self.market, signed=days_ago(10))
        self.owner = api(make_user(self.fr, "franchisee"))
        self.manager = api(make_user(self.fr, "franchisee_staff"))

    def test_manager_does_not_see_money_owed_or_contracts(self):
        row = self.manager.get("/api/v1/franchisees/").json()["results"][0]
        for key in ("balance", "overdue_amount", "commission_due"):
            self.assertIsNone(row[key], key)
        self.assertIn("commission_month", row)
        self.assertIsNotNone(row["commission_month"])
        d = self.manager.get(f"/api/v1/franchisees/{self.fr.id}/summary/").json()
        for key in ("balance", "overdue_amount", "commission_due", "contracts"):
            self.assertIsNone(d[key], key)
        for key in ("balance", "overdue_amount", "commission_due"):
            self.assertIsNone(d["franchisee"][key], key)
        self.assertIn("commission_month", d)
        self.assertEqual(self.manager.get("/api/v1/contracts/").status_code, 403)

    def test_manager_dashboard_hides_debts(self):
        d = self.manager.get("/api/v1/dashboard/").json()
        self.assertEqual(d["scope"], "franchisee")
        for key in ("balance", "overdue_amount", "commission_due"):
            self.assertIn(key, d)
            self.assertIsNone(d[key], key)
        self.assertIsNotNone(d["commission_month"])
        self.assertNotIn("attention", d)
        self.assertNotIn("35000", str(d))
        o = self.owner.get("/api/v1/dashboard/").json()
        self.assertEqual((o["balance"], o["overdue_amount"]), ("35000.00", "35000.00"))

    def test_owner_still_sees_them(self):
        row = self.owner.get("/api/v1/franchisees/").json()["results"][0]
        self.assertEqual((row["balance"], row["overdue_amount"]), ("35000.00", "35000.00"))
        d = self.owner.get(f"/api/v1/franchisees/{self.fr.id}/summary/").json()
        self.assertEqual((d["balance"], len(d["contracts"])), ("35000.00", 1))


class ContractValidationTests(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.pkg_fr = make_fr()
        self.admin = api(make_user(role="super_admin"))

    def create(self, **kw):
        return self.admin.post("/api/v1/contracts/", {"franchisee": self.pkg_fr.id, "product": self.market.id,
                                                      **kw}, format="json")

    def test_numeric_ranges(self):
        for bad in ({"prepay_percent": "0"}, {"prepay_percent": "100.01"}, {"prepay_percent": "-5"},
                    {"installment_step_months": 0}, {"fee_total": "-1"},
                    {"upfront_amount": "40000"}, {"installments_total": "10000", "installments_count": 0}):
            self.assertEqual(self.create(**bad).status_code, 400, bad)

    def test_boundaries_ok(self):
        self.assertEqual(self.create(prepay_percent="100", installments_count=1, fee_total="0").status_code, 201)

    def test_update_validated_too(self):
        cid = self.create().json()["id"]
        self.assertEqual(self.admin.patch(f"/api/v1/contracts/{cid}/", {"installment_step_months": 0}, format="json").status_code, 400)
        self.assertEqual(self.admin.patch(f"/api/v1/contracts/{cid}/", {"fee_total": "-5"}, format="json").status_code, 400)


class WebExtrasTests(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.fr = make_fr("A", "Ош")
        self.fr2 = make_fr("B", "Бишкек")
        self.admin = api(make_user(role="super_admin"))

    def test_implementations_overdue_filter(self):
        client = make_client(self.fr)
        mk = lambda **kw: Implementation.objects.create(franchisee=self.fr, client=client, product=self.market, **kw)  # noqa
        late = mk(planned_launch=days_ago(3), stage="setup")
        mk(planned_launch=days_ago(-3), stage="setup")
        mk(planned_launch=days_ago(10), stage="launched")
        mk(planned_launch=days_ago(10), stage="cancelled")
        mk(planned_launch=None, stage="contract")
        owner = api(make_user(self.fr, "franchisee"))
        rows = owner.get("/api/v1/implementations/", {"overdue": "true"}).json()["results"]
        self.assertEqual([r["id"] for r in rows], [late.id])
        self.assertTrue(rows[0]["overdue"])
        self.assertEqual(owner.get("/api/v1/implementations/", {"overdue": "false"}).json()["count"], 4)

    def test_lead_ordering_by_city(self):
        for city in ("Талас", "Ош", "Нарын"):
            create_lead(name="x", phone="1", city=city, interest="program")
        rows = self.admin.get("/api/v1/leads/", {"ordering": "city"}).json()["results"]
        self.assertEqual([r["city"] for r in rows], ["Нарын", "Ош", "Талас"])

    def test_contract_ordering_by_created_at_and_signed_at(self):
        a = make_contract(self.fr, product=self.market, activate=False, signed=days_ago(5))
        b = make_contract(self.fr2, product=self.market, activate=False, signed=days_ago(1))
        rows = self.admin.get("/api/v1/contracts/", {"ordering": "created_at"}).json()["results"]
        self.assertEqual([r["id"] for r in rows], [a.id, b.id])
        rows = self.admin.get("/api/v1/contracts/", {"ordering": "-signed_at"}).json()["results"]
        self.assertEqual([r["id"] for r in rows], [b.id, a.id])

    def test_franchisee_ordering_by_balance(self):
        make_contract(self.fr, product=self.market)  # долг 35000
        FranchiseePayment.objects.create(franchisee=self.fr2, amount=500, method="cash")  # баланс -500
        desc = self.admin.get("/api/v1/franchisees/", {"ordering": "-balance"}).json()["results"]
        self.assertEqual([r["name"] for r in desc], ["A", "B"])
        asc = self.admin.get("/api/v1/franchisees/", {"ordering": "balance"}).json()["results"]
        self.assertEqual([r["name"] for r in asc], ["B", "A"])
        self.assertEqual(asc[1]["balance"], "35000.00")


