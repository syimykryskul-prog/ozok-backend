"""Договоры, цены, начисления, вознаграждение, зачёт per_sale (разделы 5, 5А, 5В)."""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase

from billing.models import Charge, CommissionPayout, FranchiseePayment
from billing.services import franchisee_finance
from clients.models import ClientSale
from clients.services import create_client_sale, set_commission_percent
from core.exceptions import BusinessError
from core.models import add_months, today
from core.testing import api, days_ago, make_client, make_contract, make_fr, make_package, make_product, make_user
from leads.models import Lead
from network.models import Contract
from network.services import activate_contract, price_quote

D = Decimal


class ContractPriceTests(TestCase):
    def setUp(self):
        self.market = make_product("market", price_cash=35000, price_per_sale=50000)
        self.opt = make_product("opt", price_cash=26000, price_per_sale=37000, default_commission_percent=30)
        self.pkg = make_package([self.market, self.opt], price_cash=87000, price_prepay=89000, price_per_sale=127000)
        self.fr = make_fr()
        self.admin = make_user(role="super_admin")
        self.api = api(self.admin)

    def create(self, **body):
        body.setdefault("franchisee", self.fr.id)
        return self.api.post("/api/v1/contracts/", body, format="json")

    def test_price_defaults_by_scheme(self):
        r = self.create(product=self.market.id, payment_scheme="cash")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.json()["fee_total"], r.json()["list_price"]), ("35000.00", "35000.00"))
        r = self.create(product=self.opt.id, payment_scheme="per_sale")
        self.assertEqual(r.json()["fee_total"], "37000.00")
        r = self.create(package=self.pkg.id, payment_scheme="prepay")
        self.assertEqual((r.json()["fee_total"], r.json()["prepay_percent"]), ("89000.00", "40.00"))
        self.assertEqual(sorted(r.json()["product_codes"]), ["market", "opt"])
        self.assertEqual(r.json()["status"], "draft")

    def test_fee_override_keeps_list_price(self):
        r = self.create(package=self.pkg.id, payment_scheme="cash", fee_total="80000.00")
        self.assertEqual((r.json()["fee_total"], r.json()["list_price"]), ("80000.00", "87000.00"))

    def test_exactly_one_of_product_or_package(self):
        self.assertEqual(self.create(payment_scheme="cash").status_code, 400)
        self.assertEqual(self.create(product=self.market.id, package=self.pkg.id).status_code, 400)

    def test_prepay_unavailable_for_single_product(self):
        self.assertEqual(self.create(product=self.market.id, payment_scheme="prepay").status_code, 400)

    def test_price_quote_endpoint(self):
        r = self.api.get("/api/v1/contracts/price-quote/", {"package": self.pkg.id, "scheme": "prepay"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["list_price"], "89000.00")
        self.assertEqual(r.json()["prepay_percent"], "40.00")
        self.assertIn("installments_hint", r.json())
        r = self.api.get("/api/v1/contracts/price-quote/", {"product": "market", "scheme": "per_sale"})
        self.assertEqual(r.json()["list_price"], "50000.00")
        self.assertEqual(self.api.get("/api/v1/contracts/price-quote/", {"product": self.market.id,
                                                                         "scheme": "prepay"}).status_code, 400)

    def test_withhold_percent_range_and_remainder_requires_it(self):
        self.assertEqual(self.create(product=self.market.id, withhold_percent="101").status_code, 400)
        self.assertEqual(self.create(product=self.market.id, withhold_percent="-1").status_code, 400)
        # остаток франшизы погашается удержаниями: при 0% удерживать нечем
        r = self.create(product=self.market.id, payment_scheme="per_sale", withhold_percent="0")
        self.assertEqual(r.status_code, 400)
        self.assertIn("удержани", str(r.json()))
        # у договора «всё сразу» остатка нет — 0% допустим
        self.assertEqual(self.create(product=self.market.id, payment_scheme="cash", withhold_percent="0").status_code, 201)
        self.assertEqual(self.create(product=self.opt.id, payment_scheme="per_sale").json()["withhold_percent"], "30.00")
    def test_one_active_contract_per_product_including_via_package(self):
        make_contract(self.fr, product=self.market)
        # новый договор на пакет с той же сферой: 400 при создании
        r = self.create(package=self.pkg.id, payment_scheme="cash")
        self.assertEqual(r.status_code, 400)
        self.assertIn("сферу", str(r.json()))
        # и при активации ранее созданного черновика
        other = make_fr("Второй", "Бишкек")
        draft = make_contract(other, package=self.pkg, activate=False)
        activate_contract(draft)
        draft2 = make_contract(other, product=self.opt, activate=False)
        with self.assertRaises(BusinessError):
            activate_contract(draft2)
        # у другого франчайзи та же сфера — можно
        self.assertEqual(api(self.admin).post(f"/api/v1/contracts/{make_contract(make_fr('Третий', 'Нарын'), product=self.opt, activate=False).id}/activate/").status_code, 200)

    def test_can_buy_new_sphere_later(self):
        make_contract(self.fr, product=self.market)
        self.assertEqual(self.create(product=self.opt.id, payment_scheme="cash").status_code, 201)

    def test_edit_only_draft(self):
        c = make_contract(self.fr, product=self.market)
        r = self.api.patch(f"/api/v1/contracts/{c.id}/", {"fee_total": "1.00"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.api.patch(f"/api/v1/contracts/{c.id}/", {"note": "ок"}, format="json").status_code, 200)

    def test_contract_number_auto(self):
        a = make_contract(self.fr, product=self.market, activate=False)
        b = make_contract(self.fr, product=self.opt, activate=False)
        self.assertEqual(a.number, f"D-{today().year}-001")
        self.assertEqual(b.number, f"D-{today().year}-002")


class ActivationTests(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.fr = make_fr()

    def test_cash_single_fee_due_on_signing(self):
        c = make_contract(self.fr, product=self.market, scheme="cash", signed=days_ago(5))
        ch = list(c.charges.all())
        self.assertEqual(len(ch), 1)
        self.assertEqual((ch[0].kind, ch[0].amount, ch[0].due_date), ("fee", D("35000.00"), days_ago(5)))

    def test_prepay_exact_sums_with_rounding(self):
        pkg = make_package([self.market])
        signed = days_ago(1)
        c = make_contract(self.fr, package=pkg, scheme="prepay", fee=D("10000.00"), signed=signed,
                          prepay_percent=D("33.33"), installments=3)
        charges = list(c.charges.order_by("due_date", "id"))
        self.assertEqual([x.kind for x in charges], ["fee", "installment", "installment", "installment"])
        self.assertEqual([x.amount for x in charges], [D("3333.00"), D("2222.33"), D("2222.33"), D("2222.34")])
        self.assertEqual(sum(x.amount for x in charges), D("10000.00"))
        self.assertEqual([x.due_date for x in charges[1:]], [add_months(signed, i) for i in (1, 2, 3)])

    def test_prepay_default_40_percent(self):
        pkg = make_package([self.market])
        c = make_contract(self.fr, package=pkg, scheme="prepay", fee=D("89000"))
        amounts = sorted(x.amount for x in c.charges.all())
        self.assertEqual(sum(amounts), D("89000.00"))
        self.assertEqual(c.charges.get(kind="fee").amount, D("35600.00"))

    def test_per_sale_single_fee_without_due_date(self):
        c = make_contract(self.fr, product=self.market, scheme="per_sale", signed=days_ago(200))
        ch = c.charges.get()
        self.assertEqual((ch.kind, ch.amount, ch.due_date), ("fee", D("50000.00"), None))
        self.assertEqual(franchisee_finance(self.fr.id)["overdue"], 0)  # без срока — не просрочка
        self.assertEqual(franchisee_finance(self.fr.id)["balance"], D("50000.00"))

    def test_activate_only_draft_and_actions_via_api(self):
        c = make_contract(self.fr, product=self.market, activate=False)
        acc = api(make_user(role="finance"))
        self.assertEqual(acc.post(f"/api/v1/contracts/{c.id}/activate/").status_code, 200)
        r = acc.post(f"/api/v1/contracts/{c.id}/activate/")
        self.assertEqual(r.status_code, 400)
        self.assertIn("detail", r.json())
        self.assertEqual(acc.post(f"/api/v1/contracts/{c.id}/close/").json()["status"], "closed")


class BalanceTests(TestCase):
    def test_balance_and_overdue(self):
        fr = make_fr()
        market = make_product("market")
        c = make_contract(fr, product=market, scheme="cash", signed=days_ago(10))  # 35000 просрочено
        Charge.objects.create(franchisee=fr, contract=c, kind="other", amount=1000, due_date=days_ago(-5))
        f = franchisee_finance(fr.id)
        self.assertEqual((f["balance"], f["overdue"]), (D("36000.00"), D("35000.00")))
        FranchiseePayment.objects.create(franchisee=fr, amount=D("10000"), method="bank")
        f = franchisee_finance(fr.id)
        self.assertEqual((f["balance"], f["overdue"]), (D("26000.00"), D("25000.00")))
        FranchiseePayment.objects.create(franchisee=fr, amount=D("30000"), method="bank")
        f = franchisee_finance(fr.id)
        self.assertEqual((f["balance"], f["overdue"]), (D("-4000.00"), D("0.00")))


class CommissionTests(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.opt = make_product("opt")
        self.fr = make_fr()
        self.client_ = make_client(self.fr)

    def sale(self, kind="subscription", amount="10000", product=None):
        return create_client_sale(client=self.client_, product=product or self.market, kind=kind, amount=amount)

    def test_commission_20_and_30_percent(self):
        make_contract(self.fr, product=self.market, commission=30)
        s, w = self.sale(amount="3333.33")
        self.assertIsNone(w)
        self.assertEqual((s.commission_percent, s.commission_amount), (D("30"), D("1000.00")))  # 999.999 -> half-up
        s, _ = self.sale("renewal", "12345.67")
        self.assertEqual(s.commission_amount, D("3703.70"))

    def test_only_subscription_and_renewal_earn_commission(self):
        make_contract(self.fr, product=self.market, commission=20)
        for kind in ("setup", "other"):
            s, w = self.sale(kind)
            self.assertEqual((s.commission_amount, s.franchisee_income, w), (D("0"), D("10000.00"), None))
        self.assertEqual(Charge.objects.filter(kind__in=["deduction", "royalty"]).count(), 0)

    def test_no_contract_means_zero_and_warning(self):
        make_contract(self.fr, product=self.market)
        s, w = self.sale(product=self.opt)
        self.assertEqual(s.commission_amount, D("0"))
        self.assertIn("договор", w)

    def test_commission_due_is_earned_minus_payouts(self):
        make_contract(self.fr, product=self.market, commission=20)
        self.sale(amount="10000")
        self.sale("renewal", "5000")
        self.assertEqual(franchisee_finance(self.fr.id)["commission_due"], D("3000.00"))
        CommissionPayout.objects.create(franchisee=self.fr, amount=1000, method="bank")
        self.assertEqual(franchisee_finance(self.fr.id)["commission_due"], D("2000.00"))

    def test_recalc_commission_percent_by_accountant_via_api(self):
        make_contract(self.fr, product=self.market, commission=20)
        s, _ = self.sale(amount="10000")
        acc = api(make_user(role="finance"))
        r = acc.patch(f"/api/v1/client-sales/{s.id}/", {"commission_percent": "30", "amount": "1"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["commission_percent"], r.json()["commission_amount"], r.json()["amount"]),
                         ("30.00", "3000.00", "10000.00"))
        owner = api(make_user(self.fr, "franchisee"))
        self.assertEqual(owner.patch(f"/api/v1/client-sales/{s.id}/", {"commission_percent": "50"},
                                     format="json").status_code, 200)
        s.refresh_from_db()
        self.assertEqual(s.commission_percent, D("30"))  # франчайзи ставку менять не может

    def test_franchisee_cannot_enter_subscription_manually_but_license_ok(self):
        make_contract(self.fr, product=self.market)
        owner = api(make_user(self.fr, "franchisee"))
        body = {"client": self.client_.id, "product": self.market.id, "amount": "5000"}
        self.assertEqual(owner.post("/api/v1/client-sales/", {**body, "kind": "subscription"},
                                    format="json").status_code, 400)
        self.assertEqual(owner.post("/api/v1/client-sales/", {**body, "kind": "renewal"},
                                    format="json").status_code, 400)
        r = owner.post("/api/v1/client-sales/", {**body, "kind": "setup"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual((r.json()["commission_amount"], r.json()["source"], r.json()["warning"]),
                         ("0.00", "manual", None))
        admin = api(make_user(role="super_admin"))
        r = admin.post("/api/v1/client-sales/", {**body, "kind": "subscription"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["commission_amount"], "1000.00")
        self.assertIn("product_code", r.json())
        self.assertIn("sphere", r.json())

    def test_sale_warning_only_in_create_response(self):
        owner = api(make_user(self.fr, "franchisee"))
        r = owner.post("/api/v1/client-sales/", {"client": self.client_.id, "product": self.market.id,
                                                 "kind": "setup", "amount": "100"}, format="json")
        self.assertIn("warning", r.json())
        self.assertNotIn("warning", owner.get(f"/api/v1/client-sales/{r.json()['id']}/").json())


class PerSaleWithholdingTests(TestCase):
    """Удержание из дохода франчайзи в счёт остатка франшизы (5Г): 30% дохода, не больше остатка."""

    def setUp(self):
        self.market = make_product("market")
        self.fr = make_fr()
        self.client_ = make_client(self.fr)
        self.contract = make_contract(self.fr, product=self.market, scheme="per_sale", fee=D("5000"))

    def sale(self, amount, kind="subscription"):
        return create_client_sale(client=self.client_, product=self.market, kind=kind, amount=amount)[0]

    def offsets(self):
        from billing.models import CommissionAccrual  # C6: удержание по абонплате хранится в accrual.withheld_amount
        return (sum(a.withheld_amount for a in CommissionAccrual.objects.all()),
                sum(p.amount for p in FranchiseePayment.objects.filter(method="offset")))

    def test_subscription_withholds_share_of_commission(self):
        s = self.sale("20000")  # доход 20% = 4000, удержано 30% = 1200
        self.assertEqual((s.franchisee_income, s.withheld_amount), (D("4000.00"), D("1200.00")))
        self.assertEqual(self.offsets(), (D("1200.00"), D("1200.00")))
        self.assertEqual(Charge.objects.filter(kind="withhold").count(), 0)  # для абонплаты долг не создаётся
        f = franchisee_finance(self.fr.id)
        self.assertEqual((f["balance"], f["commission_due"]), (D("3800.00"), D("2800.00")))

    def test_setup_and_equipment_withhold_and_create_charge(self):
        s = self.sale("10000", "setup")  # доход 100% = 10000, удержано 3000
        self.assertEqual((s.franchisee_income, s.withheld_amount), (D("10000.00"), D("3000.00")))
        ch = s.withhold_charges.get()
        self.assertEqual((ch.kind, ch.amount, ch.due_date, ch.contract_id),
                         ("withhold", D("3000.00"), s.date + timedelta(days=30), self.contract.id))
        self.assertEqual(FranchiseePayment.objects.get(client_sale=s).method, "offset")
        self.assertEqual(CommissionPayout.objects.count(), 0)
        # долг по франшизе уменьшился на 3000, но появился долг «удержание с продаж» на те же 3000
        self.assertEqual(franchisee_finance(self.fr.id)["balance"], D("5000.00"))
        e = self.sale("1000", "equipment")
        self.assertEqual((e.withheld_amount, e.withhold_charges.count()), (D("300.00"), 1))
        o = self.sale("500", "other")
        self.assertEqual(o.withheld_amount, D("150.00"))

    def test_withholding_capped_at_remaining_fee(self):
        for _ in range(3):
            self.sale("10000", "setup")  # 3000 + 2000 (остаток) + 0
        withheld = [s.withheld_amount for s in ClientSale.objects.order_by("id")]
        self.assertEqual(withheld, [D("3000.00"), D("2000.00"), D("0.00")])
        self.assertEqual(sum(p.amount for p in FranchiseePayment.objects.filter(method="offset")), D("5000.00"))
        self.sale("20000")  # франшиза погашена — удержаний больше нет
        self.assertEqual(ClientSale.objects.latest("id").withheld_amount, D("0.00"))

    def test_contract_api_progress(self):
        self.sale("20000")  # удержано 1200
        r = api(make_user(role="super_admin")).get(f"/api/v1/contracts/{self.contract.id}/").json()
        self.assertEqual((r["paid_off_amount"], r["remaining_amount"], r["fee_total"]), ("1200.00", "3800.00", "5000.00"))
        self.assertEqual(r["payoff"]["withholding"], {"paid": "1200.00", "total": "5000.00"})
        self.assertEqual(r["payoff"]["upfront"], {"paid": "0.00", "total": "0.00"})
        owner = api(make_user(self.fr, "franchisee"))
        self.assertEqual(owner.get(f"/api/v1/contracts/{self.contract.id}/").json()["remaining_amount"], "3800.00")

    def test_recalc_percent_rebuilds_withholding(self):
        s = self.sale("10000")  # доход 2000, удержано 600
        set_commission_percent(s, 30)  # доход 3000, удержано 900
        self.assertEqual(self.offsets(), (D("900.00"), D("900.00")))
        self.assertEqual(s.offset_payments.count(), 1)

    def test_deleting_sale_removes_withholding(self):
        s = self.sale("10000", "setup")
        s.delete()
        self.assertEqual(self.offsets(), (0, 0))
        self.assertEqual(Charge.objects.filter(kind="withhold").count(), 0)

    def test_no_withholding_when_nothing_to_withhold(self):
        other = make_product("opt")
        make_contract(self.fr, product=other, scheme="cash")  # остатка нет
        create_client_sale(client=self.client_, product=other, kind="subscription", amount="1000")
        create_client_sale(client=self.client_, product=other, kind="setup", amount="1000")
        self.assertEqual(FranchiseePayment.objects.filter(method="offset").count(), 0)

    def test_withhold_percent_zero_means_no_withholding(self):
        Contract.objects.filter(pk=self.contract.pk).update(withhold_percent=0)
        self.assertEqual(self.sale("10000", "setup").withheld_amount, D("0"))

class CatalogTests(TestCase):
    def setUp(self):
        self.admin = api(make_user(role="super_admin"))
        self.fr = make_fr()
        self.owner = make_user(self.fr, "franchisee")
        self.market = make_product("market")
        self.opt = make_product("opt")
        self.pkg = make_package([self.market, self.opt])

    def test_product_crud_admin_only_and_code_is_free_slug(self):
        r = self.admin.post("/api/v1/products/", {"code": "salon-pro", "name": "Salon Pro", "sphere": "Красота",
                                                  "icon": "scissors", "price_cash": "10000",
                                                  "price_per_sale": "15000"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(r.json()["color"].startswith("#"))
        self.assertTrue(r.json()["api_key"].startswith("ozk_"))
        self.assertEqual(self.admin.post("/api/v1/products/", {"code": "x", "name": "x", "icon": "nope"},
                                         format="json").status_code, 400)
        owner = api(self.owner)
        self.assertEqual(owner.get("/api/v1/products/").status_code, 200)
        self.assertEqual(owner.post("/api/v1/products/", {"code": "y", "name": "y"}, format="json").status_code, 403)
        self.assertNotIn("api_key", owner.get("/api/v1/products/").json()["results"][0])
        pid = r.json()["id"]
        self.assertEqual(self.admin.patch(f"/api/v1/products/{pid}/", {"code": "other"}, format="json").status_code, 400)
        self.assertEqual(self.admin.delete(f"/api/v1/products/{pid}/").status_code, 204)

    def test_used_product_cannot_be_deleted_only_deactivated(self):
        make_contract(self.fr, product=self.market)
        r = self.admin.delete(f"/api/v1/products/{self.market.id}/")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.admin.patch(f"/api/v1/products/{self.market.id}/", {"is_active": False},
                                          format="json").status_code, 200)
        self.assertEqual(self.admin.post("/api/v1/contracts/", {"franchisee": self.fr.id, "product": self.market.id},
                                         format="json").status_code, 400)

    def test_package_crud_and_connected_flag(self):
        self.assertEqual(self.admin.post("/api/v1/packages/", {"name": "Новый", "products": [self.market.id],
                                                               "price_cash": "1", "price_prepay": "1",
                                                               "price_per_sale": "1"}, format="json").status_code, 201)
        make_contract(self.fr, product=self.market)
        rows = {p["code"]: p for p in api(self.owner).get("/api/v1/products/").json()["results"]}
        self.assertTrue(rows["market"]["connected"])
        self.assertFalse(rows["opt"]["connected"])

    def test_request_connection_creates_lead_for_self(self):
        owner = api(self.owner)
        r = owner.post(f"/api/v1/products/{self.opt.id}/request/")
        self.assertEqual(r.status_code, 201, r.content)
        lead = Lead.objects.get(pk=r.json()["lead_id"])
        self.assertEqual((lead.interest, lead.product, lead.franchisee_id, lead.source),
                         ("franchise", "opt", self.fr.id, "other"))
        r = owner.post(f"/api/v1/packages/{self.pkg.id}/request/")
        self.assertEqual(Lead.objects.get(pk=r.json()["lead_id"]).product, f"package:{self.pkg.id}")
        make_contract(self.fr, product=self.market)
        self.assertEqual(owner.post(f"/api/v1/products/{self.market.id}/request/").status_code, 400)  # уже подключена
        self.assertEqual(self.admin.post(f"/api/v1/products/{self.opt.id}/request/").status_code, 403)
        self.opt.is_active = False
        self.opt.save()
        self.assertEqual(owner.post(f"/api/v1/products/{self.opt.id}/request/").status_code, 400)

    def test_price_quote_function(self):
        q = price_quote(package=self.pkg, scheme="prepay")
        self.assertEqual(q["list_price"], D("89000.00"))





