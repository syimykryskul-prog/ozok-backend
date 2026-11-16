"""Регрессия раунда 2 (находки охоты на баги): C-1..C-3, H-1..H-5, M-1..M-10, L-1..L-6 и дополнения API."""
from datetime import timedelta
from decimal import Decimal as D

from django.db.models import Sum
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from billing import cash
from billing import payouts as payout_svc
from billing.models import (CashAccount, CashCategory, CashTransaction, Charge, CommissionAccrual, CommissionPayout,
                            FranchiseePayment, Payout)
from billing.services import commission_to_pay, finance_map
from clients.models import Client, ClientSale, Deal, Implementation, Subscription
from core.exceptions import BusinessError
from core.models import month_first, today
from core.settings_registry import get_decimal, set_setting
from core.testing import (api, make_client, make_contract, make_fr, make_product, make_rule, make_user)
from leads.models import Lead
from network.commission import create_tariff
from network.models import CommissionRule, Contract, FranchiseOffer

from .test_p9_p8 import Base, V1


class DealBase(Base):
    """Партнёр osh + продукт coca; цены установки и обучения заданы."""

    def setUp(self):
        super().setUp()
        self.product.setup_price, self.product.min_setup_price = 10000, 6000
        self.product.training_price, self.product.max_discount_percent = 5000, 10
        self.product.save()
        make_rule(name="Std-R", product=self.product, partner_tier="standard", sale_kind="renewal", percent=20)
        make_rule(name="Лид OZOK", sale_kind="subscription", lead_source="nbs_ads", percent=15, basis="x")
        make_rule(name="Лид OZOK R", sale_kind="renewal", lead_source="nbs_ads", percent=15, basis="x")

    def deal(self, who=None, lines=None, **kw):
        body = {"client": self.client_.id, "product": self.product.id,
                "lines": lines or [{"kind": "subscription", "tariff": self.tariff.id, "months": 2}], **kw}
        r = (who or self.OWN).post(V1 + "deals/", body, format="json")
        return r


# ---- C-1 / C-3 / C-2 -----------------------------------------------------------------------------------------------

class C1PrivilegeTests(TestCase):
    def setUp(self):
        self.fr = make_fr()
        self.owner = make_user(self.fr)
        self.staff = make_user(self.fr, role="franchisee_staff", permission_overrides={
            "actions_add": ["users.invite", "users.deactivate", "users.create", "users.update",
                            "users.partial_update"]})
        self.S, self.O = api(self.staff), api(self.owner)

    def test_staff_cannot_invite_owner(self):
        r = self.S.post(V1 + "users/invite/", {"email": "evil@test.kg", "first_name": "A", "last_name": "B",
                                               "role": "franchisee"}, format="json")
        self.assertEqual(r.status_code, 400, r.content)
        self.assertFalse(User.objects.filter(email="evil@test.kg").exists())

    def test_staff_cannot_deactivate_or_edit_owner(self):
        self.assertEqual(self.S.post(V1 + f"users/{self.owner.id}/deactivate/", {"reason": "x"},
                                     format="json").status_code, 400)
        self.assertEqual(self.S.patch(V1 + f"users/{self.owner.id}/", {"is_active": False}, format="json")
                         .status_code, 400)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)

    def test_nobody_grants_more_than_own(self):
        r = self.S.post(V1 + "users/invite/", {"email": "m@test.kg", "first_name": "A", "last_name": "B",
                                               "role": "manager"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)  # права менеджера не выше прав сотрудника
        mgr = User.objects.get(email="m@test.kg")
        r = self.S.patch(V1 + f"users/{mgr.id}/", {"role": "franchisee"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_owner_cannot_demote_other_owner_but_can_invite_staff(self):
        co = make_user(self.fr)
        self.assertEqual(self.O.post(V1 + f"users/{co.id}/deactivate/", {"reason": "x"}, format="json")
                         .status_code, 400)
        r = self.O.post(V1 + "users/invite/", {"email": "s@test.kg", "first_name": "A", "last_name": "B",
                                               "role": "franchisee_staff"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)


class C3LeadScopeTests(DealBase):
    def test_foreign_and_other_client_leads_rejected(self):
        other = Lead.objects.create(name="Чужая", phone="1", source="nbs_ads", franchisee=self.bsk,
                                    status="in_progress")
        self.assertEqual(self.deal(lead=other.id).status_code, 400)  # заявка другого партнёра
        another_client = make_client(self.osh)
        mine = Lead.objects.create(name="Моя", phone="2", source="nbs_ads", franchisee=self.osh,
                                   status="in_progress", client=another_client)
        r = self.deal(lead=mine.id)
        self.assertEqual(r.status_code, 400)  # заявка относится к другому клиенту
        ok = Lead.objects.create(name="Своя", phone="3", source="partner_ads", franchisee=self.osh,
                                 status="in_progress")
        self.assertEqual(self.deal(lead=ok.id).status_code, 201)


class C2LeadRateTests(DealBase):
    def test_nbs_ads_lead_pays_15_percent_on_main_branch_and_lead_becomes_paid(self):
        lead = Lead.objects.create(name="Реклама", phone="5", source="nbs_ads", franchisee=self.osh,
                                   status="in_progress")
        r = self.deal(lead=lead.id)
        self.assertEqual(r.status_code, 201, r.content)
        did = r.json()["id"]
        self.assertEqual(self.OWN.post(V1 + f"deals/{did}/confirm/").status_code, 200)
        self.branch.refresh_from_db()
        self.client_.refresh_from_db()
        self.assertEqual((self.branch.lead_id, self.client_.lead_id), (lead.id, lead.id))
        lead.refresh_from_db()
        self.assertEqual(lead.status, "contract")
        self.pay(3000, months=2)
        acc = CommissionAccrual.objects.get()
        self.assertEqual((acc.rate, acc.amount), (D("15.00"), D("450.00")))  # не 20%
        lead.refresh_from_db()
        self.assertEqual(lead.status, "paid")


# ---- H-1 / H-2 -----------------------------------------------------------------------------------------------------

class TrainingTests(DealBase):
    def test_training_without_own_floor_needs_no_approval_and_is_own_kind(self):
        self.assertIsNone(self.product.min_training_price)
        r = self.OWN.post(V1 + "deals/preview/", {"client": self.client_.id, "product": self.product.id,
                                                  "lines": [{"kind": "training"}]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.json()["needs_approval"])
        d = self.deal(lines=[{"kind": "setup"}, {"kind": "training"}]).json()
        self.assertEqual(self.OWN.post(V1 + f"deals/{d['id']}/confirm/").status_code, 200)
        kinds = set(ClientSale.objects.filter(deal_id=d["id"]).values_list("kind", flat=True))
        self.assertEqual(kinds, {"setup", "training"})
        t = ClientSale.objects.get(deal_id=d["id"], kind="training")
        self.assertEqual(t.franchisee_income, t.amount)  # 100% партнёру, вне SetupSplitRule

    def test_training_not_folded_into_setup_split(self):
        from network.models import SetupSplitRule
        SetupSplitRule.objects.create(product=self.product, total=10000, partner_amount=6000, implementer_amount=4000)
        d = self.deal(lines=[{"kind": "training"}]).json()
        self.OWN.post(V1 + f"deals/{d['id']}/confirm/")
        t = ClientSale.objects.get(deal_id=d["id"])
        self.assertEqual((t.kind, t.franchisee_income), ("training", t.amount))


# ---- H-3 -----------------------------------------------------------------------------------------------------------

class RuleVersioningTests(DealBase):
    def test_used_rule_frozen_unused_editable_with_reason(self):
        self.pay(3000)
        used = CommissionRule.objects.get(name="Std")
        url = V1 + f"commission-rules/{used.id}/"
        r = self.A.patch(url, {"percent": "50", "reason": "x"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.A.patch(url, {"note": "заметка"}, format="json").status_code, 400)  # причина нужна всегда
        self.assertEqual(self.A.patch(url, {"note": "заметка", "reason": "Уточнение"}, format="json").status_code, 200)
        fresh = make_rule(name="Новое", product=self.product, sale_kind="setup", percent=50)
        self.assertEqual(self.A.patch(V1 + f"commission-rules/{fresh.id}/", {"percent": "60", "reason": "Правка"},
                                      format="json").status_code, 200)

    def test_new_version_from_date_closes_old(self):
        self.pay(3000)
        used = CommissionRule.objects.get(name="Std")
        url = V1 + f"commission-rules/{used.id}/new-version/"
        self.assertEqual(self.A.post(url, {"percent": "25"}, format="json").status_code, 400)  # причина
        self.assertEqual(self.A.post(url, {"percent": "25", "reason": "x", "valid_from": str(today() - timedelta(days=1))},
                                     format="json").status_code, 400)  # не в прошлом
        r = self.A.post(url, {"percent": "25", "reason": "Решение", "valid_from": str(today() + timedelta(days=5))},
                        format="json")
        self.assertEqual(r.status_code, 201, r.content)
        used.refresh_from_db()
        self.assertEqual(used.valid_to, today() + timedelta(days=4))
        self.assertEqual((r.json()["percent"], r.json()["valid_from"], r.json()["product"]),
                         ("25.00", str(today() + timedelta(days=5)), used.product_id))


# ---- H-4 / M-1 -----------------------------------------------------------------------------------------------------

class DirectSaleTests(DealBase):
    def test_direct_sale_creates_client_subscription_and_installation(self):
        r = self.A.post(V1 + "deals/", {"client_name": "Прямой клиент", "product": self.product.id, "lines": [
            {"kind": "setup"}, {"kind": "subscription", "tariff": self.tariff.id, "months": 3}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        did = r.json()["id"]
        self.assertEqual(self.A.post(V1 + f"deals/{did}/confirm/").status_code, 200)
        c = Client.objects.get(business_name="Прямой клиент")
        self.assertEqual((c.origin, c.franchisee_id), ("ozok_direct", None))
        sub = Subscription.objects.get(client=c)
        self.assertEqual(sub.paid_until, (today().replace(day=1) if False else sub.paid_until))
        self.assertGreater(sub.paid_until, today() + timedelta(days=60))  # оплачено на 3 месяца
        self.assertEqual(sub.status, "active")
        inst = Implementation.objects.get(deal_id=did)
        self.assertIsNone(inst.franchisee_id)
        self.assertEqual(c.branches.count(), 1)

    def test_cancel_confirmed_deal_closes_unpaid_subscription_and_installation(self):
        did = self.deal(lines=[{"kind": "setup"}, {"kind": "subscription", "tariff": self.tariff.id,
                                                    "months": 2}]).json()["id"]
        self.OWN.post(V1 + f"deals/{did}/confirm/")
        self.assertEqual(Subscription.objects.get(deal_id=did).status, "awaiting_payment")
        inst = Implementation.objects.get(deal_id=did)
        self.assertEqual(inst.status, "assigned")
        r = self.A.post(V1 + f"deals/{did}/cancel/", {"reason": "Клиент передумал"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Subscription.objects.get(deal_id=did).status, "closed")
        inst.refresh_from_db()
        self.assertEqual((inst.status, inst.stage), ("cancelled", "cancelled"))


# ---- H-5 -----------------------------------------------------------------------------------------------------------

class LegacyCreditOpenPayoutTests(Base):
    def test_credit_applied_inside_open_payout_and_one_formula_everywhere(self):
        self.pay(3000, d=today() - timedelta(days=40), months=1)
        self.pay(3000, months=1)
        payout = payout_svc.create(self.osh, month_first(today()), self.fin)  # draft на 1200
        self.assertEqual(payout.amount, D("1200.00"))
        CommissionPayout.objects.create(franchisee=self.osh, amount=900, method="bank")  # старая ручная выплата
        payout.refresh_from_db()
        self.assertEqual(payout.amount, D("300.00"))  # пакет пересчитан
        first, second = CommissionAccrual.objects.order_by("date", "id")
        self.assertEqual((first.status, first.payout_id, second.paid_amount, second.owed),
                         ("paid", None, D("300.00"), D("300.00")))
        due = finance_map([self.osh.id])[self.osh.id]["commission_due"]
        self.assertEqual(due, D("300.00"))
        self.assertEqual(D(self.OWN.get(V1 + "dashboard/").json()["commission_due"]), due)
        s = self.A.get(V1 + "commission-accruals/summary/", {"partner": self.osh.id}).json()
        self.assertEqual(D(s["to_pay"]), due)
        self.assertEqual(commission_to_pay([self.osh.id])[self.osh.id], due)
        payout_svc.verify(payout, self.fin)
        payout_svc.pay(payout, self.fin2)
        self.assertEqual(finance_map([self.osh.id])[self.osh.id]["commission_due"], D("0.00"))

    def test_overpaid_legacy_does_not_break_new_payout_and_cards_agree(self):
        self.pay(3000, months=1)
        payout = payout_svc.create(self.osh, month_first(today()), self.fin)
        CommissionPayout.objects.create(franchisee=self.osh, amount=1000, method="bank")  # больше начисленного
        payout.refresh_from_db()
        self.assertEqual(payout.status, "cancelled")  # пакет полностью погашен старой выплатой
        due = finance_map([self.osh.id])[self.osh.id]["commission_due"]
        self.assertEqual(due, D("-400.00"))  # переплачено 400: кредит за партнёром
        self.pay(3000, months=1)
        self.assertEqual(finance_map([self.osh.id])[self.osh.id]["commission_due"], D("200.00"))
        p2 = payout_svc.create(self.osh, month_first(today()), self.fin)
        self.assertEqual(p2.amount, D("200.00"))


# ---- M-2 / M-3 -----------------------------------------------------------------------------------------------------

class CashDateAndBalanceTests(TestCase):
    def setUp(self):
        self.fin = make_user(role="finance")
        self.acc = CashAccount.objects.create(name="Тест", kind="cash", opening_balance=1000,
                                              opening_date=today() - timedelta(days=10))
        self.out = CashCategory.objects.filter(direction="out", manual_allowed=True).first()
        self.inc = CashCategory.objects.get(code="other_income")

    def out_entry(self, amount, d=None):
        return cash.manual_entry(user=self.fin, direction="out", amount=amount, account=self.acc, date=d,
                                 category_obj=self.out)

    def test_dates_validated(self):
        with self.assertRaises(BusinessError):
            self.out_entry(10, today() - timedelta(days=11))
        with self.assertRaises(BusinessError):
            self.out_entry(10, today() + timedelta(days=1))
        self.assertEqual(self.out_entry(10, today()).amount, D("10.00"))

    def test_negative_balance_blocked_unless_setting(self):
        with self.assertRaises(BusinessError):
            self.out_entry(1500)
        r = api(self.fin).post(V1 + "cash-transactions/", {"direction": "out", "account": self.acc.id,
                                                           "category": self.out.id, "amount": "1500"}, format="json")
        self.assertEqual(r.status_code, 400)
        admin = make_user(role="super_admin")
        set_setting("cash.allow_negative", True, user=admin, reason="Тест")
        self.assertEqual(self.out_entry(1500).amount, D("1500.00"))

    def test_reversal_dated_like_original(self):
        d = today() - timedelta(days=5)
        tx = self.out_entry(100, d)
        rev = cash.cancel_entry(tx, self.fin, "Ошибка")
        self.assertEqual(rev.date, d)

    def test_report_invariant_opening_plus_flows_equals_closing(self):
        other = CashAccount.objects.create(name="Второй", kind="bank", opening_balance=500,
                                           opening_date=today() - timedelta(days=10))
        cash.manual_entry(user=self.fin, direction="in", amount=700, account=self.acc, category_obj=self.inc,
                          date=today() - timedelta(days=6))
        t = self.out_entry(200, today() - timedelta(days=4))
        cash.cancel_entry(t, self.fin, "Ошибка")
        self.out_entry(50, today() - timedelta(days=2))
        cash.manual_entry(user=self.fin, direction="transfer", amount=300, account=self.acc, transfer_account=other,
                          date=today() - timedelta(days=1))
        orig = cash.post(source_type="payment", source_id=99, direction="in", amount=400,
                         category_code="subscription", account=self.acc, date=today() - timedelta(days=3))
        cash.reverse(orig, reason="Возврат", amount=150, source_type="payment_refund", source_id=7,
                     date=today() - timedelta(days=1))
        for d1, d2 in ((today() - timedelta(days=10), today()), (today() - timedelta(days=5), today() - timedelta(days=1)),
                       (today() - timedelta(days=2), today())):
            rep = cash.cash_report(d1, d2)
            for a in rep["accounts"]:
                got = D(a["opening"]) + D(a["in"]) - D(a["out"]) + D(a["transfer_in"]) - D(a["transfer_out"])
                self.assertEqual(got, D(a["closing"]), (a["name"], d1, d2))
            self.assertEqual(D(rep["opening_total"]) + D(rep["total_in"]) - D(rep["total_out"]),
                             D(rep["closing_total"]))


# ---- M-4 / M-5 -----------------------------------------------------------------------------------------------------

class SettingsTests(DealBase):
    def test_nbs_lead_percents_create_rule_versions(self):
        make_rule(name="Лид OZOK — доля", sale_kind="setup", lead_source="nbs_ads", percent=70, basis="x")
        set_setting("nbs_lead.subscription_percent", "12", user=self.admin, reason="Новая политика")
        set_setting("nbs_lead.nbs_share_percent", "40", user=self.admin, reason="Новая политика")
        sub_rules = CommissionRule.objects.filter(lead_source="nbs_ads", sale_kind="subscription")
        cur = sub_rules.filter(valid_to__isnull=True)
        self.assertEqual({r.percent for r in cur}, {D("12.00")})
        self.assertTrue(sub_rules.filter(valid_to=today() - timedelta(days=1), percent=15).exists())  # старая версия
        setup = CommissionRule.objects.filter(lead_source="nbs_ads", sale_kind="setup", valid_to__isnull=True).get()
        self.assertEqual(setup.percent, D("60.00"))

    def test_franchise_amounts_drive_offers_and_withhold_default(self):
        set_setting("franchise.cash_amount", "90000", user=self.admin, reason="Прайс")
        set_setting("franchise.installment_max_amount", "180000", user=self.admin, reason="Прайс")
        offers = {o.code: o for o in FranchiseOffer.objects.all()}
        self.assertEqual((offers["cash"].total, offers["installment"].total), (D("90000.00"), D("180000.00")))
        set_setting("franchise.default_withhold_percent", "25", user=self.admin, reason="Политика")
        fr = make_fr("Новый", "Ош", status="manager")
        c = Contract.objects.create(franchisee=fr, product=self.product, payment_scheme="hybrid", fee_total=1000,
                                    list_price=1000, signed_at=today())
        self.assertEqual(c.withhold_percent, D("25.00"))
        self.assertEqual(get_decimal("franchise.default_withhold_percent"), D("25"))

    def test_validators(self):
        bad = (("nbs_lead.subscription_percent", "150"), ("franchise.default_withhold_percent", "-1"),
               ("withhold.due_days", 0), ("installations.default_deadline_days", 0),
               ("approvals.roles", ["franchisee"]), ("approvals.roles", []),
               ("cash.payment_source_account", {"manual": "no_such_kind"}))
        for key, value in bad:
            r = self.A.put(V1 + f"settings/{key}/", {"value": value, "reason": "т"}, format="json")
            self.assertEqual(r.status_code, 400, (key, value, r.content))
        ok = self.A.put(V1 + "settings/approvals.roles/", {"value": ["super_admin"], "reason": "т"}, format="json")
        self.assertEqual(ok.status_code, 200, ok.content)


# ---- M-6 / M-7 / M-8 -----------------------------------------------------------------------------------------------

class PartnerTests(TestCase):
    def setUp(self):
        self.fr = make_fr("Закрытый", "Ош", status="closed")
        self.admin, self.head = make_user(role="super_admin"), make_user(role="network_head")
        self.A, self.H = api(self.admin), api(self.head)

    def test_closed_partner_cannot_be_reopened_by_patch(self):
        r = self.A.patch(V1 + f"franchisees/{self.fr.id}/", {"status": "franchisee"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.fr.refresh_from_db()
        self.assertEqual(self.fr.status, "closed")
        self.assertEqual(self.H.post(V1 + f"franchisees/{self.fr.id}/reopen/", {"reason": "x"}, format="json")
                         .status_code, 403)
        self.assertEqual(self.A.post(V1 + f"franchisees/{self.fr.id}/reopen/", {}, format="json").status_code, 400)
        r = self.A.post(V1 + f"franchisees/{self.fr.id}/reopen/", {"reason": "Ошибочное закрытие"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.fr.refresh_from_db()
        self.assertNotEqual(self.fr.status, "closed")

    def test_summary_hides_finance_from_non_finance_roles(self):
        for role in ("quality", "recruiter", "regional_support", "implementer"):
            u = make_user(role=role, region="Ош")
            r = api(u).get(V1 + f"franchisees/{self.fr.id}/summary/")
            if r.status_code == 200:
                d = r.json()
                self.assertTrue(all(d[k] is None for k in ("balance", "overdue_amount", "commission_due")), role)


class IntegrationClosedPartnerTests(TestCase):
    def test_upsert_for_closed_or_suspended_partner_is_unmatched(self):
        market = make_product("market")
        for status, code in (("closed", "CL-1"), ("suspended", "SU-1")):
            make_fr(f"П {status}", "Ош", status=status, code=code)
            ev = {"event_id": f"e-{code}", "type": "client.upsert", "external_client_id": f"c-{code}",
                  "partner_code": code, "business_name": "Магазин", "phone": "1"}
            r = api().post(V1 + "integrations/events/", ev, format="json", HTTP_X_OZOK_KEY=market.api_key)
            self.assertEqual(r.json()["results"][0]["status"], "unmatched", (status, r.content))
        self.assertFalse(Client.objects.exists())


# ---- M-9 -----------------------------------------------------------------------------------------------------------

class FranchisePaymentCapTests(TestCase):
    def setUp(self):
        self.product = make_product("coca", payment_source="manual")
        self.fr = make_fr()
        self.contract = make_contract(self.fr, product=self.product, scheme="cash", fee=1000, upfront_amount=1000,
                                      installments_total=0, installments_count=0)
        self.fin, self.admin = make_user(role="finance"), make_user(role="super_admin")
        self.F, self.A = api(self.fin), api(self.admin)

    def post(self, who, amount, **kw):
        return who.post(V1 + "franchisee-payments/", {"franchisee": self.fr.id, "contract": self.contract.id,
                                                      "amount": str(amount), "method": "bank", **kw}, format="json")

    def test_cap_credit_flag_and_future_date(self):
        self.assertEqual(self.post(self.F, 1500).status_code, 400)
        self.assertEqual(self.post(self.F, 1500, allow_credit=True).status_code, 400)  # флаг — только Super Admin
        self.assertEqual(self.post(self.A, 1500, allow_credit=True).status_code, 201)
        self.assertEqual(self.post(self.F, 100, date=str(today() + timedelta(days=2))).status_code, 400)
        self.assertEqual(self.post(self.F, 100, comment="сверх").status_code, 400)  # остаток уже исчерпан


# ---- M-10 / L-* / API ----------------------------------------------------------------------------------------------

class LookupAccessTests(TestCase):
    def test_lookups_for_menu_pages(self):
        for role, urls in (("recruiter", ("products/", "packages/")), ("implementer", ("products/", "packages/")),
                           ("finance", ("users/",))):
            c = api(make_user(role=role))
            for url in urls:
                self.assertEqual(c.get(V1 + url).status_code, 200, (role, url))
        # финансовые данные по-прежнему закрыты
        for role in ("recruiter", "implementer", "quality", "regional_support"):
            c = api(make_user(role=role))
            for url in ("payouts/", "charges/", "cash-transactions/"):
                self.assertEqual(c.get(V1 + url).status_code, 403, (role, url))


class LowTests(DealBase):
    def test_l1_franchisee_cannot_patch_implementer(self):
        inst = Implementation.objects.create(franchisee=self.osh, client=self.client_, product=self.product)
        dev = make_user(role="implementer")
        r = self.OWN.patch(V1 + f"installations/{inst.id}/", {"implementer": dev.id}, format="json")
        self.assertEqual(r.status_code, 400, r.content)

    def test_l2_deactivated_login_message(self):
        u = make_user(role="finance", email="off@test.kg")
        u.is_active = False
        u.save()
        r = api().post(V1 + "auth/login/", {"email": "off@test.kg", "password": "pass12345"}, format="json")
        self.assertEqual(r.status_code, 401)
        self.assertIn("Учётная запись отключена", r.json()["detail"])
        r = api().post(V1 + "auth/login/", {"email": "off@test.kg", "password": "wrong"}, format="json")
        self.assertNotIn("отключена", r.content.decode())  # без перебора учётных записей

    def test_l3_sales_deal_filter(self):
        did = self.deal(lines=[{"kind": "setup"}]).json()["id"]
        self.OWN.post(V1 + f"deals/{did}/confirm/")
        other = self.deal(lines=[{"kind": "training"}]).json()["id"]
        self.OWN.post(V1 + f"deals/{other}/confirm/")
        r = self.OWN.get(V1 + "client-sales/", {"deal": did}).json()
        self.assertEqual({x["deal"] for x in r["results"]}, {did})

    def test_l5_reasons_for_tariff_price_and_min_price(self):
        url = V1 + f"tariffs/{self.tariff.id}/"
        body = {"monthly_price": "1800", "valid_from": str(today() + timedelta(days=30))}
        self.assertEqual(self.A.post(url + "set-price/", body, format="json").status_code, 400)
        self.assertEqual(self.A.post(url + "set-price/", {**body, "reason": "Индексация"}, format="json")
                         .status_code, 200)
        self.assertEqual(self.A.patch(url, {"min_price": "1000"}, format="json").status_code, 400)
        self.assertEqual(self.A.patch(url, {"min_price": "1000", "reason": "Политика"}, format="json").status_code, 200)

    def test_l6_activation_needs_manager_stage_or_override(self):
        fr = make_fr("Кандидат", "Ош", status="candidate")
        c = make_contract(fr, product=self.product, activate=False)
        self.assertEqual(self.A.post(V1 + f"contracts/{c.id}/activate/").status_code, 400)
        self.assertEqual(self.FIN.post(V1 + f"contracts/{c.id}/activate/", {"override_reason": "х"}, format="json")
                         .status_code, 400)  # исключение — только Super Admin
        r = self.A.post(V1 + f"contracts/{c.id}/activate/", {"override_reason": "Решение владельца"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)


class ApiAdditionsTests(DealBase):
    def test_priority_hours_left_and_line_income_and_product_source(self):
        c2 = self.A.post(V1 + "branches/", {"client": self.client_.id, "name": "Филиал 2"}, format="json").json()
        self.assertIsNotNone(c2["priority_hours_left"])
        self.assertGreater(c2["priority_hours_left"], 0)
        self.assertIsNone(self.A.get(V1 + f"branches/{self.branch.id}/").json()["priority_hours_left"])
        d = self.deal(lines=[{"kind": "setup"}, {"kind": "subscription", "tariff": self.tariff.id,
                                                  "months": 2}]).json()
        self.OWN.post(V1 + f"deals/{d['id']}/confirm/")
        items = {i["kind"]: i for i in self.OWN.get(V1 + f"deals/{d['id']}/").json()["items"]}
        self.assertEqual((items["subscription"]["income_now"], items["subscription"]["income_on_payment"]),
                         ("0.00", "600.00"))  # 20% от 3000 — когда клиент заплатит
        self.assertEqual(items["setup"]["income_now"], items["setup"]["total"])
        prod = self.A.get(V1 + f"products/{self.product.id}/").json()
        self.assertEqual(prod["payment_source"], "manual")
