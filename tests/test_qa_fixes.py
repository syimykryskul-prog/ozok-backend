"""Регрессия дефектов QA: D1 (двойная выплата), D2 (отчёт Кассы), D3 (роль на основе владельца), D4 (модули),
D6 (категория платежа франчайзи), AC1 (воронка по оплаченной сделке)."""
from datetime import timedelta
from decimal import Decimal as D

from django.test import TestCase

from billing import cash
from billing import payouts as payout_svc
from billing.models import (CashAccount, CashCategory, CashTransaction, Charge, CommissionAccrual, CommissionPayout,
                            FranchiseePayment)
from billing.services import finance_map
from core import access
from core.exceptions import BusinessError
from core.models import month_first, today
from core.testing import api, make_fr, make_user

from .test_p9_p8 import Base, V1, at


class D1PayoutTests(Base):
    def test_legacy_manual_payout_not_paid_twice(self):
        self.pay(3000)  # начисление 600
        CommissionPayout.objects.create(franchisee=self.osh, amount=600, method="bank")  # старая ручная выплата
        acc = CommissionAccrual.objects.get()
        self.assertEqual((acc.status, acc.paid_amount), ("paid", D("600.00")))
        self.assertEqual(finance_map([self.osh.id])[self.osh.id]["commission_due"], D("0.00"))
        with self.assertRaises(BusinessError):
            payout_svc.create(self.osh, month_first(today()), self.fin)
        r = self.FIN.post(V1 + "payouts/", {"partner": self.osh.id, "period": today().strftime("%Y-%m")},
                          format="json")
        self.assertEqual(r.status_code, 400)

    def test_partial_legacy_payout_leaves_remainder_in_batch(self):
        self.pay(3000)
        CommissionPayout.objects.create(franchisee=self.osh, amount=250, method="cash")
        p = payout_svc.create(self.osh, month_first(today()), self.fin)
        self.assertEqual(p.amount, D("350.00"))
        self.assertEqual(finance_map([self.osh.id])[self.osh.id]["commission_due"], D("350.00"))

    def test_fifo_allocation_oldest_first(self):
        self.pay(3000, d=today() - timedelta(days=40), months=1)  # 600
        self.pay(1500, months=1)  # 300
        CommissionPayout.objects.create(franchisee=self.osh, amount=700, method="bank")
        old, new = CommissionAccrual.objects.order_by("date", "id")
        self.assertEqual((old.status, new.paid_amount, new.owed), ("paid", D("100.00"), D("200.00")))

    def test_commission_due_and_dashboard_agree(self):
        self.pay(3000)
        CommissionPayout.objects.create(franchisee=self.osh, amount=200, method="bank")
        due = finance_map([self.osh.id])[self.osh.id]["commission_due"]
        self.assertEqual(due, D("400.00"))
        self.assertEqual(D(self.OWN.get(V1 + "dashboard/").json()["commission_due"]), due)
        month = today().strftime("%Y-%m")
        hq = self.A.get(V1 + "dashboard/", {"month": month}).json()
        self.assertEqual(D(hq["commissions_paid"]), D("200.00"))  # старая выплата учтена в «выплачено»
        p = payout_svc.create(self.osh, month_first(today()), self.fin)
        payout_svc.verify(p, self.fin)
        payout_svc.pay(p, self.fin2)
        self.assertEqual(finance_map([self.osh.id])[self.osh.id]["commission_due"], D("0.00"))
        hq = self.A.get(V1 + "dashboard/", {"month": month}).json()
        self.assertEqual(D(hq["commissions_paid"]), D("600.00"))
        cat = [c for c in self.FIN.get(V1 + "reports/cash/").json()["out_by_category"] if c["code"] == "commission_payouts"]
        self.assertEqual(D(cat[0]["amount"]), D("600.00"))

    def test_commission_payouts_endpoint_is_read_only(self):
        for method in ("post", "put", "patch", "delete"):
            r = getattr(self.FIN, method)(V1 + "commission-payouts/", {"franchisee": self.osh.id, "amount": "5"},
                                          format="json") if method == "post" else None
            if r is not None:
                self.assertEqual(r.status_code, 405)
        po = CommissionPayout.objects.create(franchisee=self.osh, amount=5, method="cash")
        self.assertEqual(self.FIN.get(V1 + f"commission-payouts/{po.id}/").status_code, 200)
        self.assertEqual(self.FIN.patch(V1 + f"commission-payouts/{po.id}/", {"amount": "9"}, format="json")
                         .status_code, 405)
        self.assertEqual(self.FIN.delete(V1 + f"commission-payouts/{po.id}/").status_code, 405)


class D2CashReportTests(TestCase):
    def setUp(self):
        self.fin = make_user(role="finance")
        self.F = api(self.fin)
        self.acc = CashAccount.objects.create(name="Касса тест", kind="cash", opening_balance=10000,
                                              opening_date=today() - timedelta(days=30))
        self.out_cat = CashCategory.objects.filter(direction="out", manual_allowed=True).first()

    def report(self):
        return self.F.get(V1 + "reports/cash/").json()

    def test_cancelled_expense_nets_to_zero(self):
        tx = cash.manual_entry(user=self.fin, direction="out", amount=7777, account=self.acc, category_obj=self.out_cat)
        cash.cancel_entry(tx, self.fin, "Ошибка")
        d = self.report()
        self.assertEqual((d["total_in"], d["total_out"]), ("0.00", "0.00"))
        self.assertEqual((d["in_by_category"], d["out_by_category"]), ([], []))
        self.assertEqual(cash.balance(self.acc), D("10000.00"))  # остаток счёта верен
        a = next(x for x in d["accounts"] if x["id"] == self.acc.id)
        self.assertEqual((a["in"], a["out"]), ("0.00", "0.00"))

    def test_refund_reduces_subscription_income(self):
        other = CashAccount.objects.first()
        orig = cash.post(source_type="payment", source_id=1, direction="in", amount=3000, category_code="subscription",
                         account=other)
        cash.reverse(orig, reason="Возврат", amount=1000, source_type="payment_refund", source_id=5)
        d = self.report()
        self.assertEqual((d["total_in"], d["total_out"]), ("2000.00", "0.00"))
        self.assertEqual({r["code"]: r["amount"] for r in d["in_by_category"]}, {"subscription": "2000.00"})
        self.assertEqual(cash.balance(other), D("2000.00"))


class D3D4AccessTests(TestCase):
    def test_custom_role_on_super_admin_rejected_and_not_super(self):
        admin = make_user(role="super_admin")
        r = api(admin).post(V1 + "access-roles/", {"name": "Копия владельца", "code": "owner-copy", "base_role": "super_admin",
                                                    "permissions": {"leads": "read"}, "actions": [],
                                                    "scope": "all"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Нельзя создать роль на основе владельца", str(r.json()))
        # уже существующая кастомная роль с base_role=super_admin (данные до исправления) прав владельца не даёт
        from accounts.models import AccessRole
        role = AccessRole.objects.create(code="legacy-owner", name="x", base_role="super_admin",
                                         permissions={"leads": "read"}, actions=[], scope="all", is_system=False)
        u = make_user(role="super_admin", email="legacy.owner@test.kg")
        u.access_role = role
        u.save()
        eff = access.compute(u)
        self.assertFalse(eff.is_super)
        self.assertEqual(api(u).get(V1 + "auth/me/").json()["modules"].get("cash"), "none")
        self.assertTrue(access.compute(admin).is_super)

    def test_no_finance_modules_for_non_finance_roles(self):
        defs = access.seed_definitions()
        fin_modules = ("cash", "cash_partners", "payouts")
        for role in ("recruiter", "regional_support", "implementer", "quality", "franchisee_staff", "manager"):
            d = defs[role]
            eff = access.Effective(d["permissions"], set(), "all", None, False).as_dict()
            for m in fin_modules:
                self.assertEqual(eff["modules"][m], "none", (role, m))
        self.assertEqual(access.Effective(defs["regional_support"]["permissions"], set(), "all", None,
                                          False).as_dict()["modules"]["clients"], "write")
        self.assertIn("subscription_payments", access.MODULES["clients"])
        self.assertNotIn("subscription_payments", access.MODULES["cash"])


class D6CategoryTests(Base):
    def post_fp(self, **kw):
        return FranchiseePayment.objects.create(franchisee=self.osh, amount=500, method="bank", **kw)

    def category_of(self, fp):
        tx = CashTransaction.objects.get(source_type="franchisee_payment", source_id=fp.id)
        return tx.category.code

    def test_explicit_category_charge_contract_and_default(self):
        Charge.objects.create(franchisee=self.osh, amount=900, kind="nbs_lead_share")
        Charge.objects.create(franchisee=self.osh, amount=900, kind="withhold")  # раньше «угадывалась» категория
        self.assertEqual(self.category_of(self.post_fp()), "other_income")  # без привязки — не угадываем
        ch = Charge.objects.create(franchisee=self.osh, amount=100, kind="nbs_lead_share")
        self.assertEqual(self.category_of(self.post_fp(charge=ch)), "nbs_lead_share")
        wh = Charge.objects.create(franchisee=self.osh, amount=100, kind="withhold")
        self.assertEqual(self.category_of(self.post_fp(charge=wh)), "withholdings")
        self.assertEqual(self.category_of(self.post_fp(income_category="franchise_fee")), "franchise_fee")

    def test_api_accepts_category_and_charge(self):
        ch = Charge.objects.create(franchisee=self.osh, amount=100, kind="withhold")
        r = self.FIN.post(V1 + "franchisee-payments/", {"franchisee": self.osh.id, "amount": "50", "method": "bank",
                                                        "charge": ch.id}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["charge"], ch.id)
        r = self.FIN.post(V1 + "franchisee-payments/", {"franchisee": self.osh.id, "amount": "50", "method": "bank",
                                                        "income_category": "no_such"}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.FIN.post(V1 + "franchisee-payments/", {"franchisee": self.osh.id, "amount": "50", "method": "bank"},
                          format="json")  # обратная совместимость: поля необязательны
        self.assertEqual(r.status_code, 201)


class AC1MarkPaidTests(TestCase):
    def setUp(self):
        from core.testing import make_contract, make_product, make_rule
        self.product = make_product("coca", payment_source="manual")
        self.fr = make_fr("Менеджер", "Ош", status="manager")
        make_rule(name="Std", product=self.product, partner_tier="standard", sale_kind="setup", percent=50)
        self.mgr = make_user(self.fr, role="manager")
        self.admin, self.fin = make_user(role="super_admin"), make_user(role="finance")
        from core.testing import make_client
        self.client_ = make_client(self.fr)

    def deal(self):
        from network.commission import create_tariff
        tariff = create_tariff(product=self.product, code="start", name="Старт", monthly_price=1500)
        r = api(self.mgr).post(V1 + "deals/", {"client": self.client_.id, "product": self.product.id, "lines": [
            {"kind": "setup"}, {"kind": "subscription", "tariff": tariff.id, "months": 1}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        did = r.json()["id"]
        self.assertEqual(api(self.mgr).post(V1 + f"deals/{did}/confirm/").status_code, 200)
        return did

    def test_confirmed_deal_does_not_advance_paid_does(self):
        self.product.setup_price = 10000
        self.product.min_setup_price = 1000
        self.product.save()
        did = self.deal()
        self.fr.refresh_from_db()
        self.assertNotIn(self.fr.funnel_stage, ("first_sale", "second_sale", "third_sale"))
        self.assertEqual(api(self.mgr).post(V1 + f"deals/{did}/mark-paid/").status_code, 403)
        r = api(self.fin).post(V1 + f"deals/{did}/mark-paid/")
        self.assertEqual(r.status_code, 200, r.content)
        self.fr.refresh_from_db()
        self.assertEqual(self.fr.funnel_stage, "first_sale")
        self.assertEqual(api(self.fin).post(V1 + f"deals/{did}/mark-paid/").status_code, 400)  # уже оплачена
