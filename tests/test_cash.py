"""Касса (P0+ п.6): автопроводки по денежным событиям, сторно, остатки, ручные записи, отчёт."""
from datetime import timedelta
from decimal import Decimal as D

from django.test import TestCase
from django.utils import timezone

from billing import cash as csvc
from billing import payments as psvc
from billing import payouts as pout
from billing import recruiters
from billing.models import (CashAccount, CashCategory, CashTransaction, CommissionPayout, FranchiseePayment,
                            RecruiterReward)
from clients import branches as bsvc
from core.models import today
from core.testing import api, make_client, make_contract, make_fr, make_product, make_rule, make_user

V1 = "/api/v1/"
T = CashTransaction


class Base(TestCase):
    def setUp(self):
        self.product = make_product("coca", payment_source="manual", setup_price=1000)
        self.fr = make_fr()
        make_rule(name="R", sale_kind="subscription", percent=20)
        make_contract(self.fr, product=self.product, scheme="cash", signed=today() - timedelta(days=50))
        self.admin, self.fin, self.fin2 = make_user(role="super_admin"), make_user(role="finance"), \
            make_user(role="finance")
        self.head = make_user(role="network_head")
        self.owner = make_user(self.fr)
        self.A, self.FIN, self.FIN2, self.HEAD, self.OWN = (api(self.admin), api(self.fin), api(self.fin2),
                                                            api(self.head), api(self.owner))
        self.cashbox = CashAccount.objects.get(kind="cash")
        self.bank = CashAccount.objects.get(kind="bank")
        self.elqr = CashAccount.objects.get(kind="elqr")
        client = make_client(self.fr)
        self.branch = bsvc.ensure_main_branch(client)
        self.branch.product = self.product
        self.branch.save()
        self.n = 0

    def pay(self, amount=3000, source="manual", months=2):
        self.n += 1
        return psvc.register_payment(branch=self.branch, amount=amount, paid_at=timezone.now(), source=source,
                                     external_id=f"c{self.n}", months=months, check_authority=False)[0]

    def bal(self, acc):
        return csvc.balance(acc)


class SeedTests(Base):
    def test_seeded_accounts_and_categories(self):
        self.assertEqual(set(CashAccount.objects.values_list("kind", flat=True)), {"cash", "bank", "elqr"})
        codes = set(CashCategory.objects.values_list("code", flat=True))
        for c in ("subscription", "franchise_fee", "direct_sales", "nbs_lead_share", "withholdings", "other_income",
                  "commission_payouts", "recruiter_rewards", "implementer_pay", "rent", "salary", "nbs_ads",
                  "equipment_purchase", "taxes", "other_expense"):
            self.assertIn(c, codes)


class AutoPostingTests(Base):
    def test_subscription_payment_posts_in_by_source_and_reversal_on_cancel(self):
        p = self.pay(3000, "manual")
        tx = T.objects.get(source_type="payment", source_id=p.id)
        self.assertEqual((tx.direction, tx.amount, tx.account_id, tx.category.code, tx.is_auto),
                         ("in", D("3000.00"), self.cashbox.id, "subscription", True))
        self.assertEqual(self.pay(1000, "bank_import", 1).__class__.__name__, "Payment")
        self.assertEqual(self.pay(500, "product", 1) and self.bal(self.elqr), D("500.00"))
        self.assertEqual(self.bal(self.bank), D("1000.00"))
        self.assertEqual(self.pay(3000, "manual") and self.bal(self.cashbox), D("6000.00"))
        # отмена оплаты -> сторно, остаток вернулся
        r = self.FIN.post(V1 + f"payments/{p.id}/cancel/", {"reason": "Ошибка"}, format="json")
        self.assertEqual(r.status_code, 200)
        rev = T.objects.get(reversal_of=tx)
        self.assertEqual((rev.direction, rev.amount), ("out", D("3000.00")))
        self.assertEqual(self.bal(self.cashbox), D("3000.00"))

    def test_partial_refund_posts_out(self):
        p = self.pay(3000)
        self.FIN.post(V1 + f"payments/{p.id}/refund/", {"amount": "1000"}, format="json")
        self.assertEqual(self.bal(self.cashbox), D("2000.00"))
        self.FIN.post(V1 + f"payments/{p.id}/refund/", {"amount": "2000"}, format="json")
        self.assertEqual(self.bal(self.cashbox), D("0.00"))

    def test_franchisee_payment_in_and_offset_skipped_and_cancel_reverses(self):
        a = FranchiseePayment.objects.create(franchisee=self.fr, amount=1000, method="bank",
                                             contract=self.fr.contracts.first())
        b = FranchiseePayment.objects.create(franchisee=self.fr, amount=500, method="cash")
        FranchiseePayment.objects.create(franchisee=self.fr, amount=700, method="offset")
        self.assertEqual(T.objects.filter(source_type="franchisee_payment").count(), 2)
        self.assertEqual(T.objects.get(source_id=a.id, source_type="franchisee_payment").category.code,
                         "franchise_fee")
        self.assertEqual(self.bal(self.bank), D("1000.00"))
        self.assertEqual(self.bal(self.cashbox), D("500.00"))
        r = self.FIN.post(V1 + f"franchisee-payments/{a.id}/cancel/", {"reason": "Ошибка"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.bal(self.bank), D("0.00"))
        self.assertEqual(T.objects.filter(reversal_of__isnull=False).count(), 1)

    def test_legacy_commission_payout_out_and_cancel(self):
        po = CommissionPayout.objects.create(franchisee=self.fr, amount=300, method="bank")
        self.assertEqual(self.bal(self.bank), D("-300.00"))
        self.assertEqual(self.FIN.post(V1 + f"commission-payouts/{po.id}/cancel/", {"reason": "Ошибка"},
                                       format="json").status_code, 404)  # D1: история только для чтения
        po.mark_cancelled(None, "Ошибка")  # служебная отмена старой строки (через модель) — сторно в Кассе
        self.assertEqual(self.bal(self.bank), D("0.00"))

    def test_direct_nbs_sale_in_and_cancel(self):
        r = self.A.post(V1 + "deals/", {"client_name": "Кафе", "product": self.product.id,
                                        "lines": [{"kind": "setup"}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        did = r.json()["id"]
        self.assertEqual(self.A.post(V1 + f"deals/{did}/confirm/").status_code, 200)
        tx = T.objects.get(source_type="deal", source_id=did)
        self.assertEqual((tx.direction, tx.amount, tx.category.code), ("in", D("1000.00"), "direct_sales"))
        self.assertEqual(self.bal(self.cashbox), D("1000.00"))
        self.A.post(V1 + f"deals/{did}/cancel/", {"reason": "Отказ"}, format="json")
        self.assertEqual(self.bal(self.cashbox), D("0.00"))
        # партнёрская продажа в кассу OZOK не попадает
        d2 = self.OWN.post(V1 + "deals/", {"client": self.branch.client_id, "product": self.product.id,
                                           "lines": [{"kind": "setup"}]}, format="json").json()["id"]
        self.OWN.post(V1 + f"deals/{d2}/confirm/")
        self.assertFalse(T.objects.filter(source_type="deal", source_id=d2).exists())

    def test_payouts_rewards_implementer_post_out(self):
        self.pay(3000)
        po = pout.create(self.fr, today(), self.fin)
        pout.verify(po, self.fin)
        pout.pay(po, self.fin2)
        tx = T.objects.get(source_type="payout", source_id=po.id)
        self.assertEqual((tx.direction, tx.amount, tx.category.code, tx.partner_id),
                         ("out", D("600.00"), "commission_payouts", self.fr.id))
        rr = RecruiterReward.objects.create(partner=self.fr, recruiter=self.admin, target_amount=100,
                                            accrued_amount=100, status="verified", verified_by=self.fin)
        recruiters.pay(rr, self.fin2)
        self.assertEqual(T.objects.get(source_type="recruiter_reward", source_id=rr.id).amount, D("100.00"))
        from clients.models import Implementation, ImplementerAccrual
        impl = make_user(role="implementer")
        inst = Implementation.objects.create(franchisee=self.fr, client=self.branch.client, product=self.product)
        acc = ImplementerAccrual.objects.create(installation=inst, user=impl, amount=800, status="verified",
                                                verified_by=self.fin)
        self.FIN2.post(V1 + f"implementer-accruals/{acc.id}/pay/")
        self.assertEqual(T.objects.get(source_type="implementer_accrual", source_id=acc.id).category.code,
                         "implementer_pay")

    def test_posting_is_idempotent(self):
        p = self.pay(3000)
        csvc.on_payment_confirmed(p)
        csvc.on_payment_confirmed(p)
        self.assertEqual(T.objects.filter(source_type="payment", source_id=p.id).count(), 1)


class ManualAndJournalTests(Base):
    def post(self, who, **kw):
        body = {"direction": "in", "amount": "1000", "account": self.cashbox.id,
                "category": CashCategory.objects.get(code="other_income").id, **kw}
        return who.post(V1 + "cash-transactions/", body, format="json")

    def test_manual_entries_roles_and_categories(self):
        self.assertEqual(self.post(self.FIN).status_code, 201)
        self.assertEqual(self.post(self.A).status_code, 201)
        self.assertEqual(self.post(self.OWN).status_code, 403)
        self.assertEqual(self.post(self.HEAD).status_code, 403)  # network_head — только чтение
        self.assertEqual(self.HEAD.get(V1 + "cash-transactions/").status_code, 200)
        self.assertEqual(self.OWN.get(V1 + "cash-transactions/").status_code, 403)
        # системная статья вручную нельзя; направление должно совпадать
        sub = CashCategory.objects.get(code="subscription")
        self.assertEqual(self.post(self.FIN, category=sub.id).status_code, 400)
        rent = CashCategory.objects.get(code="rent")
        self.assertEqual(self.post(self.FIN, category=rent.id).status_code, 400)  # in со статьёй расхода
        r = self.post(self.FIN, direction="out", category=rent.id, amount="400", comment="Аренда офиса")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.bal(self.cashbox), D("1600.00"))  # 1000+1000-400

    def test_transfer_between_accounts_and_cancel_reverses(self):
        self.post(self.FIN, amount="2000")
        r = self.FIN.post(V1 + "cash-transactions/", {"direction": "transfer", "amount": "800",
                                                      "account": self.cashbox.id,
                                                      "transfer_account": self.bank.id}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((self.bal(self.cashbox), self.bal(self.bank)), (D("1200.00"), D("800.00")))
        bad = self.FIN.post(V1 + "cash-transactions/", {"direction": "transfer", "amount": "1",
                                                        "account": self.bank.id, "transfer_account": self.bank.id},
                            format="json")
        self.assertEqual(bad.status_code, 400)
        tid = r.json()["id"]
        self.assertEqual(self.FIN.post(V1 + f"cash-transactions/{tid}/cancel/", {}, format="json").status_code, 400)
        c = self.FIN.post(V1 + f"cash-transactions/{tid}/cancel/", {"reason": "Ошибка"}, format="json")
        self.assertEqual(c.status_code, 200, c.content)
        self.assertEqual((self.bal(self.cashbox), self.bal(self.bank)), (D("2000.00"), D("0.00")))
        self.assertEqual(self.FIN.post(V1 + f"cash-transactions/{tid}/cancel/", {"reason": "x"},
                                       format="json").status_code, 400)  # уже отменена
        self.assertTrue(self.FIN.get(V1 + f"cash-transactions/{tid}/").json()["is_reversed"])
        self.assertEqual(self.FIN.get(V1 + "cash-transactions/", {"reversed": "true"}).json()["count"], 1)

    def test_journal_is_immutable_and_no_delete_or_edit(self):
        tid = self.post(self.FIN).json()["id"]
        self.assertEqual(self.FIN.delete(V1 + f"cash-transactions/{tid}/").status_code, 405)
        self.assertEqual(self.FIN.patch(V1 + f"cash-transactions/{tid}/", {"amount": "1"}, format="json").status_code,
                         405)
        tx = T.objects.get(pk=tid)
        with self.assertRaises(RuntimeError):
            tx.amount = 5
            tx.save()
        with self.assertRaises(RuntimeError):
            tx.delete()

    def test_filters_and_audit(self):
        self.post(self.FIN)
        p = self.pay(3000)
        self.assertEqual(self.FIN.get(V1 + "cash-transactions/", {"is_auto": "true"}).json()["count"], 1)
        self.assertEqual(self.FIN.get(V1 + "cash-transactions/", {"account": self.cashbox.id,
                                                                  "direction": "in"}).json()["count"], 2)
        from core.models import AuditLog
        self.assertTrue(AuditLog.objects.filter(entity="billing.CashTransaction", action="create").exists())

    def test_account_opening_balance_and_accounts_api(self):
        r = self.FIN.post(V1 + "cash-accounts/", {"name": "Сейф", "kind": "cash", "opening_balance": "5000",
                                                  "opening_date": str(today() - timedelta(days=10))}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["balance"], "5000.00")
        self.assertEqual(self.OWN.get(V1 + "cash-accounts/").status_code, 403)
        self.assertEqual(self.FIN.delete(V1 + f"cash-accounts/{r.json()['id']}/").status_code, 405)

    def test_nbs_expense_endpoint(self):
        rent = CashCategory.objects.get(code="rent")
        csvc.manual_entry(user=self.fin, direction="in", amount=5000, account=self.bank,
                          category_obj=CashCategory.objects.get(code="other_income"))  # расход нужен остаток (M-2)
        r = self.FIN.post(V1 + "nbs-expenses/", {"account": self.bank.id, "category": rent.id, "amount": "700",
                                                 "description": "Аренда за ноябрь", "supplier": "ИП Асанов",
                                                 "attachment": "https://files/receipt.pdf"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["transaction"]["direction"], "out")
        self.assertEqual(self.bal(self.bank), D("4300.00"))
        self.assertEqual(self.OWN.post(V1 + "nbs-expenses/", {}, format="json").status_code, 403)
        sub = CashCategory.objects.get(code="subscription")
        self.assertEqual(self.FIN.post(V1 + "nbs-expenses/", {"account": self.bank.id, "category": sub.id,
                                                              "amount": "1"}, format="json").status_code, 400)


class ReportTests(Base):
    def test_report_totals_opening_closing_categories_months(self):
        old = today() - timedelta(days=45)
        self.bank.opening_balance, self.bank.opening_date = D("10000"), old - timedelta(days=5)
        self.bank.save()
        T.objects.create(date=old, account=self.bank, direction="in", amount=2000,
                         category=CashCategory.objects.get(code="other_income"), is_auto=False)
        self.pay(3000)                                        # касса +3000 в периоде
        FranchiseePayment.objects.create(franchisee=self.fr, amount=1000, method="bank")  # банк +1000
        rent = CashCategory.objects.get(code="rent")
        csvc.manual_entry(user=self.fin, direction="out", amount=400, account=self.bank, category_obj=rent)
        csvc.manual_entry(user=self.fin, direction="transfer", amount=500, account=self.cashbox,
                          transfer_account=self.bank)
        d1 = today() - timedelta(days=30)
        r = self.HEAD.get(V1 + "reports/cash/", {"date_from": str(d1), "date_to": str(today())})
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        accs = {a["kind"]: a for a in d["accounts"]}
        self.assertEqual((accs["bank"]["opening"], accs["bank"]["closing"]), ("12000.00", "13100.00"))
        self.assertEqual((accs["cash"]["opening"], accs["cash"]["closing"]), ("0.00", "2500.00"))
        self.assertEqual((d["total_in"], d["total_out"], d["net"]), ("4000.00", "400.00", "3600.00"))
        self.assertEqual(d["opening_total"], "12000.00")
        self.assertEqual(d["closing_total"], "15600.00")  # 12000 + 3600 (перевод остаток не меняет)
        inc = {r["code"]: r["amount"] for r in d["in_by_category"]}
        self.assertEqual(inc["subscription"], "3000.00")
        self.assertEqual({r["code"]: r["amount"] for r in d["out_by_category"]}, {"rent": "400.00"})
        self.assertTrue(sum(D(m["in"]) for m in d["by_month"]) == D("4000.00"))
        self.assertEqual(self.OWN.get(V1 + "reports/cash/").status_code, 403)

    def test_report_csv_export(self):
        self.pay(3000)
        r = self.FIN.get(V1 + "reports/cash/", {"export": "csv"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r["Content-Type"])
        self.assertIn("Абонплата клиентов", r.content.decode("utf-8"))

    def test_cancelled_events_net_to_zero_in_report(self):
        p = self.pay(3000)
        self.FIN.post(V1 + f"payments/{p.id}/cancel/", {"reason": "Ошибка"}, format="json")
        d = self.FIN.get(V1 + "reports/cash/").json()
        self.assertEqual((d["total_in"], d["total_out"], d["net"]), ("0.00", "0.00", "0.00"))  # D2: сторно гасит исходный поток
        self.assertEqual(d["closing_total"], d["opening_total"])
