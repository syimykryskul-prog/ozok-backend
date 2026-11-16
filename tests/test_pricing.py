"""Раздел 5Г/5Д: правила вознаграждения, тарифы, составная оплата франшизы, оборудование, удержание, отчёты."""
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from billing.models import Charge, CommissionPayout, FranchiseePayment
from billing.services import franchisee_finance
from clients.models import ClientProductAccount, ClientSale
from clients.services import create_client_sale, recalculate_sale, refund_sale
from core.models import add_months, today
from core.testing import api, days_ago, make_client, make_contract, make_fr, make_product, make_rule, make_user
from integrations.models import IntegrationEvent
from network.commission import compute_income, price_on, select_rule
from network.models import CommissionRule, Contract, Equipment, Tariff, TariffPrice
from network.commission import create_tariff

D = Decimal
EVENTS = "/api/v1/integrations/events/"


def pick(fr, product, kind="subscription", tariff=None, on=None):
    return select_rule(franchisee_id=fr.id, product_id=product.id, tariff_id=tariff.id if tariff else None,
                       kind=kind, on=on or today())


class RuleSelectionTests(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.opt = make_product("opt")
        self.fr = make_fr("A", "Ош")
        self.other = make_fr("B", "Бишкек")
        self.tariff = create_tariff(product=self.market, code="biz", name="Бизнес", monthly_price=3000)
        CommissionRule.objects.filter(valid_from=date(2020, 1, 1)).update(is_active=False)  # свои правила в тесте

    def test_default_rules_from_migration_exist(self):
        self.assertEqual(CommissionRule.objects.filter(valid_from=date(2020, 1, 1)).count(), 5)

    def test_most_specific_rule_wins(self):
        base = make_rule(name="Абонплата", sale_kind="subscription", percent=20)
        self.assertEqual(pick(self.fr, self.market), base)
        by_product = make_rule(name="Продукт", product=self.market, sale_kind="subscription", percent=25)  # вес 2
        self.assertEqual(pick(self.fr, self.market), by_product)
        self.assertEqual(pick(self.fr, self.opt), base)  # другая сфера
        by_tariff = make_rule(name="Тариф", tariff=self.tariff, product=self.market, sale_kind="subscription",
                              percent=27)  # вес 4: 2 + 1 + 1
        self.assertEqual(pick(self.fr, self.market, tariff=self.tariff), by_tariff)
        self.assertEqual(pick(self.fr, self.market), by_product)  # без тарифа правило тарифа не подходит
        by_fr = make_rule(name="Франчайзи", franchisee=self.fr, percent=35)  # вес 4: ровно как у тарифа
        # при равном весе — позднее valid_from, затем больший id: у обоих 2020-01-01 -> больший id
        self.assertEqual(pick(self.fr, self.market, tariff=self.tariff), by_fr)
        by_fr_kind = make_rule(name="Франчайзи+вид", franchisee=self.fr, sale_kind="subscription", percent=31)  # 5
        self.assertEqual(pick(self.fr, self.market, tariff=self.tariff), by_fr_kind)
        self.assertEqual(pick(self.other, self.market, tariff=self.tariff), by_tariff)  # правило не для B
        self.assertEqual(pick(self.fr, self.market, kind="setup"), by_fr)  # вид setup: правило без вида (вес 4)

    def test_renewal_uses_subscription_rules_and_other_kinds_do_not(self):
        sub = make_rule(name="Абонплата", sale_kind="subscription", percent=20)
        self.assertEqual(pick(self.fr, self.market, kind="renewal"), sub)
        self.assertIsNone(pick(self.fr, self.market, kind="setup"))
        setup = make_rule(name="Установка", sale_kind="setup", percent=100)
        self.assertEqual(pick(self.fr, self.market, kind="setup"), setup)
        self.assertIsNone(pick(self.fr, self.market, kind="equipment"))

    def test_tie_later_valid_from_wins_then_id(self):
        old = make_rule(name="Старое", sale_kind="subscription", percent=20, valid_from=date(2025, 1, 1))
        new = make_rule(name="Новое", sale_kind="subscription", percent=22, valid_from=date(2026, 1, 1))
        self.assertEqual(pick(self.fr, self.market), new)
        self.assertEqual(pick(self.fr, self.market, on=date(2025, 6, 1)), old)  # на дату до нового правила
        same = make_rule(name="Тот же день", sale_kind="subscription", percent=23, valid_from=date(2026, 1, 1))
        self.assertEqual(pick(self.fr, self.market), same)  # равный valid_from -> больший id

    def test_validity_window_and_active_flag(self):
        r = make_rule(name="Акция", sale_kind="subscription", percent=30, valid_from=date(2026, 3, 1),
                      valid_to=date(2026, 3, 31))
        self.assertIsNone(pick(self.fr, self.market, on=date(2026, 2, 28)))
        self.assertEqual(pick(self.fr, self.market, on=date(2026, 3, 31)), r)
        self.assertIsNone(pick(self.fr, self.market, on=date(2026, 4, 1)))
        r.is_active = False
        r.save()
        self.assertIsNone(pick(self.fr, self.market, on=date(2026, 3, 15)))

    def test_percent_and_fixed_per_sale(self):
        rule = make_rule(name="10%+200", percent=10, fixed_amount=200)
        self.assertEqual(compute_income(rule, amount=D("3000"), months=3)["income"], D("500.00"))

    def test_fixed_per_month(self):
        rule = make_rule(name="25%+200/мес", percent=25, fixed_amount=200, fixed_per="month")
        calc = compute_income(rule, amount=D("9000"), months=3)
        self.assertEqual((calc["percent"], calc["fixed"], calc["income"]), (D("25.00"), D("600.00"), D("2850.00")))
        only_fixed = make_rule(name="Фикс", fixed_amount=300)
        self.assertEqual(compute_income(only_fixed, amount=D("9000"))["income"], D("300.00"))

    def test_base_paid_vs_list(self):
        paid = make_rule(name="От оплаченного", percent=20, base="paid")
        lst = make_rule(name="От прайса", percent=20, base="list")
        self.assertEqual(compute_income(paid, amount=D("4000"), list_amount=D("5000"))["income"], D("800.00"))
        calc = compute_income(lst, amount=D("4000"), list_amount=D("5000"))
        self.assertEqual((calc["base"], calc["income"]), (D("5000.00"), D("1000.00")))  # скидка за счёт центра
        self.assertEqual(compute_income(lst, amount=D("4000"), list_amount=None)["income"], D("800.00"))
        self.assertEqual(compute_income(None, amount=D("4000"))["income"], D("0.00"))


class SaleSnapshotTests(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.fr = make_fr()
        self.client_ = make_client(self.fr)
        make_contract(self.fr, product=self.market)
        self.rule = make_rule(name="Ош: 25% + 100/мес", franchisee=self.fr, sale_kind="subscription", percent=25,
                              fixed_amount=100, fixed_per="month", valid_from=date(2026, 1, 1))
        self.admin = api(make_user(role="super_admin"))
        self.buh = api(make_user(role="finance"))

    def sale(self, **kw):
        kw.setdefault("amount", "3000")
        kw.setdefault("months", 3)
        return create_client_sale(client=self.client_, product=self.market, kind="subscription", **kw)[0]

    def test_snapshot_fields(self):
        s = self.sale()
        self.assertEqual((s.commission_rule_id, s.commission_percent, s.commission_fixed, s.commission_base_amount,
                          s.franchisee_income, s.commission_amount, s.income_overridden),
                         (self.rule.id, D("25.00"), D("300.00"), D("3000.00"), D("1050.00"), D("1050.00"), False))

    def test_rule_edit_does_not_change_past_sales_until_recalculate(self):
        s = self.sale()
        r = self.admin.patch(f"/api/v1/commission-rules/{self.rule.id}/", {"percent": "50", "reason": "Пересмотр"},
                             format="json")
        self.assertEqual(r.status_code, 400)  # H-3: применённое правило задним числом не меняется
        r = self.admin.post(f"/api/v1/commission-rules/{self.rule.id}/new-version/", {
            "percent": "50", "reason": "Пересмотр", "valid_from": str(today() + timedelta(days=1))}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.valid_to, today())
        s.refresh_from_db()
        self.assertEqual((s.commission_percent, s.franchisee_income), (D("25.00"), D("1050.00")))
        new = self.sale()  # новая версия действует с завтрашнего дня: сегодняшняя продажа — по старой ставке
        self.assertEqual(new.franchisee_income, D("1050.00"))
        later = create_client_sale(client=self.client_, product=self.market, kind="subscription", amount="3000",
                                   months=3, date=today() + timedelta(days=1))[0]
        self.assertEqual(later.franchisee_income, D("1800.00"))
        r = self.buh.post(f"/api/v1/client-sales/{s.id}/recalculate/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["commission_percent"], r.json()["franchisee_income"], r.json()["commission_rule"]),
                         ("25.00", "1050.00", self.rule.id))  # новая версия начнёт действовать только с завтра
        self.assertEqual(franchisee_finance(self.fr.id)["commission_due"], D("3900.00"))

    def test_recalculate_permissions_and_guards(self):
        s = self.sale()
        owner = api(make_user(self.fr, "franchisee"))
        self.assertEqual(owner.post(f"/api/v1/client-sales/{s.id}/recalculate/").status_code, 403)
        refund_sale(s, 500, external_payment_id="x:refund:1")
        self.assertEqual(self.admin.post(f"/api/v1/client-sales/{s.id}/recalculate/").status_code, 400)
        refund = ClientSale.objects.get(refund_of=s)
        self.assertEqual(self.admin.post(f"/api/v1/client-sales/{refund.id}/recalculate/").status_code, 400)

    def test_manual_income_override_flag_and_withholding_uses_final_income(self):
        Contract.objects.update(payment_scheme="per_sale", fee_total=D("5000"), upfront_amount=0)
        s = self.sale()  # доход 1050, удержано 315
        self.assertEqual(s.withheld_amount, D("315.00"))
        r = self.buh.patch(f"/api/v1/client-sales/{s.id}/", {"franchisee_income": "2000"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["franchisee_income"], r.json()["income_overridden"], r.json()["withheld_amount"],
                          r.json()["commission_amount"]), ("2000.00", True, "600.00", "2000.00"))
        self.assertEqual(FranchiseePayment.objects.get(client_sale_id=s.id).amount, D("600.00"))
        # пересчёт по правилам снимает ручной доход
        r = self.buh.post(f"/api/v1/client-sales/{s.id}/recalculate/").json()
        self.assertEqual((r["franchisee_income"], r["income_overridden"], r["withheld_amount"]),
                         ("1050.00", False, "315.00"))

    def test_preview_endpoint(self):
        owner = api(make_user(self.fr, "franchisee"))
        r = owner.get("/api/v1/commission-rules/preview/", {"product": "market", "sale_kind": "subscription",
                                                            "amount": "3000", "months": 3, "franchisee": 9999})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["rule"]["id"], r.json()["percent"], r.json()["fixed"], r.json()["income"]),
                         (self.rule.id, "25.00", "300.00", "1050.00"))  # franchisee у франчайзи — всегда свой
        r = self.admin.get("/api/v1/commission-rules/preview/", {"franchisee": make_fr("Другой", "Талас").id,
                                                                 "product": "market", "sale_kind": "setup",
                                                                 "amount": "1000"})
        self.assertEqual(r.json()["income"], "1000.00")  # по умолчанию 100% с установки
        self.assertEqual(self.admin.get("/api/v1/commission-rules/preview/", {"amount": "x"}).status_code, 400)

    def test_rules_resource_visibility_and_permissions(self):
        other_rule = make_rule(name="Чужое", franchisee=make_fr("Б", "Бишкек"), percent=30)
        owner = api(make_user(self.fr, "franchisee"))
        ids = {r["id"] for r in owner.get("/api/v1/commission-rules/").json()["results"]}
        self.assertIn(self.rule.id, ids)
        self.assertNotIn(other_rule.id, ids)
        self.assertEqual(owner.post("/api/v1/commission-rules/", {"name": "x", "percent": "50"}, format="json").status_code, 403)
        body = {"name": "Новое", "percent": "30", "sale_kind": "setup"}
        self.assertEqual(self.buh.post("/api/v1/commission-rules/", body, format="json").status_code, 403)  # M11: правила — Super Admin
        self.assertEqual(self.admin.post("/api/v1/commission-rules/", body, format="json").status_code, 201)
        self.assertEqual(api(make_user(role="network_head")).post("/api/v1/commission-rules/", body,
                                                                format="json").status_code, 403)
        self.assertEqual(self.admin.post("/api/v1/commission-rules/", {"name": "пусто"}, format="json").status_code, 400)
        active = self.admin.get("/api/v1/commission-rules/", {"active_on": "2025-06-01", "franchisee": self.fr.id})
        self.assertNotIn(self.rule.id, {r["id"] for r in active.json()["results"]})  # правило с 2026-01-01


class TariffTests(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.admin = api(make_user(role="super_admin"))

    def test_price_on_date_and_history(self):
        t = create_tariff(product=self.market, code="start", name="Старт", monthly_price=2000,
                          valid_from=date(2026, 1, 1))
        self.admin.post(f"/api/v1/tariffs/{t.id}/set-price/", {"reason": "Тест", "monthly_price": "2500", "valid_from": "2026-06-01"},
                        format="json")
        self.admin.post(f"/api/v1/tariffs/{t.id}/set-price/", {"reason": "Тест", "monthly_price": "3000", "valid_from": "2026-12-01"},
                        format="json")
        self.assertEqual([price_on(t, date(2026, 5, 31)), price_on(t, date(2026, 6, 1)),
                          price_on(t, date(2026, 11, 30)), price_on(t, date(2027, 1, 1))],
                         [D("2000.00"), D("2500.00"), D("2500.00"), D("3000.00")])
        self.assertIsNone(price_on(t, date(2025, 12, 31)))
        rows = list(TariffPrice.objects.filter(tariff=t).order_by("valid_from").values_list("valid_from", "valid_to"))
        self.assertEqual(rows, [(date(2026, 1, 1), date(2026, 5, 31)), (date(2026, 6, 1), date(2026, 11, 30)),
                                (date(2026, 12, 1), None)])  # история не переписывается
        # нельзя вставить цену «в прошлое» поверх истории
        r = self.admin.post(f"/api/v1/tariffs/{t.id}/set-price/", {"reason": "Тест", "monthly_price": "1", "valid_from": "2026-03-01"},
                            format="json")
        self.assertEqual(r.status_code, 400)
        detail = self.admin.get(f"/api/v1/tariffs/{t.id}/").json()
        self.assertEqual(len(detail["prices"]), 3)
        self.assertEqual(detail["current_price"], "2500.00" if today() < date(2026, 12, 1) else "3000.00")

    def test_tariff_crud_and_product_price_from(self):
        r = self.admin.post("/api/v1/tariffs/", {"product": self.market.id, "code": "start", "name": "Старт",
                                                 "monthly_price": "2000"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.json()["current_price"], len(r.json()["prices"])), ("2000.00", 1))
        self.admin.post("/api/v1/tariffs/", {"product": self.market.id, "code": "biz", "name": "Бизнес",
                                             "monthly_price": "1500"}, format="json")
        self.assertEqual(self.admin.post("/api/v1/tariffs/", {"product": self.market.id, "code": "biz", "name": "Дубль",
                                                              "monthly_price": "1"}, format="json").status_code, 400)
        row = next(p for p in self.admin.get("/api/v1/products/").json()["results"] if p["code"] == "market")
        self.assertEqual(row["subscription_price"], "1500.00")  # «от N сом»
        self.assertEqual(len(self.admin.get("/api/v1/tariffs/", {"product": "market"}).json()["results"]), 2)
        self.assertEqual(self.admin.get("/api/v1/tariffs/", {"is_active": "false"}).json()["count"], 0)
        owner = api(make_user(make_fr(), "franchisee"))
        self.assertEqual(owner.get("/api/v1/tariffs/").status_code, 200)
        self.assertEqual(owner.post("/api/v1/tariffs/", {"product": self.market.id, "code": "x", "name": "x",
                                                         "monthly_price": "1"}, format="json").status_code, 403)
        self.assertEqual(owner.post(f"/api/v1/tariffs/{r.json()['id']}/set-price/",
                                    {"monthly_price": "1", "valid_from": "2030-01-01"}, format="json").status_code, 403)

    def test_sale_gets_list_amount_from_price_on_sale_date(self):
        t = create_tariff(product=self.market, code="start", name="Старт", monthly_price=2000,
                          valid_from=date(2026, 1, 1))
        from network.commission import set_price
        set_price(t, 2500, date(2026, 6, 1))
        fr = make_fr()
        client = make_client(fr)
        make_contract(fr, product=self.market)
        old, _ = create_client_sale(client=client, product=self.market, kind="subscription", amount=5400, months=3,
                                    tariff=t, date=date(2026, 3, 10))
        new, _ = create_client_sale(client=client, product=self.market, kind="renewal", amount=7500, months=3,
                                    tariff=t, date=date(2026, 7, 10))
        self.assertEqual((old.list_amount, old.discount_amount), (D("6000.00"), D("600.00")))
        self.assertEqual((new.list_amount, new.discount_amount), (D("7500.00"), D("0.00")))


class IntegrationTariffTests(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.fr = make_fr(code="OSH-01")
        self.tariff = create_tariff(product=self.market, code="biz", name="Бизнес", monthly_price=3000,
                                    valid_from=date(2026, 1, 1))
        make_contract(self.fr, product=self.market)
        self.n = 0
        self.post(self.ev("client.upsert", external_client_id="c1", partner_code="OSH-01", business_name="М"))

    def ev(self, kind, **kw):
        self.n += 1
        return {"event_id": f"t{self.n}", "type": kind, **kw}

    def post(self, data):
        return api().post(EVENTS, data, format="json", HTTP_X_OZOK_KEY=self.market.api_key)

    def pay(self, pid, **kw):
        body = dict(external_payment_id=pid, external_client_id="c1", amount="8100", months=3,
                    paid_until=add_months(today(), 3).isoformat(), **kw)
        return self.post(self.ev("payment.succeeded", **body)).json()["results"][0]

    def test_known_tariff_discount_and_account(self):
        self.assertEqual(self.pay("p1", tariff_code="biz")["status"], "processed")
        s = ClientSale.objects.get(external_payment_id="p1")
        self.assertEqual((s.tariff_id, s.list_amount, s.discount_amount), (self.tariff.id, D("9000.00"), D("900.00")))
        acc = ClientProductAccount.objects.get()
        self.assertEqual((acc.tariff_id, acc.discount_percent), (self.tariff.id, D("10.00")))

    def test_explicit_list_and_discount_amounts(self):
        self.pay("p1", tariff_code="biz", list_amount="9500", discount_amount="1400")
        s = ClientSale.objects.get(external_payment_id="p1")
        self.assertEqual((s.list_amount, s.discount_amount), (D("9500.00"), D("1400.00")))
        self.pay("p2", discount_amount="100")  # без тарифа: прайс = оплачено + скидка
        self.assertEqual(ClientSale.objects.get(external_payment_id="p2").list_amount, D("8200.00"))

    def test_unknown_tariff_code_is_processed_with_warning(self):
        r = self.pay("p1", tariff_code="nope")
        self.assertEqual(r["status"], "processed")
        self.assertIn("nope", r["error"] or "")
        s = ClientSale.objects.get(external_payment_id="p1")
        self.assertIsNone(s.tariff_id)
        self.assertEqual(s.list_amount, D("8100.00"))
        ev = IntegrationEvent.objects.get(external_id__startswith="t", event_type="payment.succeeded")
        self.assertEqual((ev.status, "nope" in ev.error), ("processed", True))

    def test_tariff_rule_applies_to_integrated_sale(self):
        make_rule(name="Бизнес 25% + 200/мес", tariff=self.tariff, product=self.market, sale_kind="subscription",
                  percent=25, fixed_amount=200, fixed_per="month")
        self.pay("p1", tariff_code="biz")
        s = ClientSale.objects.get(external_payment_id="p1")
        self.assertEqual((s.commission_percent, s.commission_fixed, s.franchisee_income),
                         (D("25.00"), D("600.00"), D("2625.00")))


class CompositeContractTests(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.fr = make_fr()
        self.admin = api(make_user(role="super_admin"))
        self.signed = date(2026, 1, 10)

    def hybrid(self, **kw):
        return make_contract(self.fr, product=self.market, scheme="hybrid", fee=D("100000"), signed=self.signed,
                             upfront_amount=D("20000"), upfront_due=date(2026, 1, 20), installments_total=D("30000"),
                             installments_count=3, installment_step_months=2, **kw)

    def test_charges_from_parts(self):
        c = self.hybrid()
        rows = list(c.charges.order_by("id"))
        self.assertEqual([(r.kind, r.amount, r.due_date) for r in rows], [
            ("fee", D("20000.00"), date(2026, 1, 20)),
            ("installment", D("10000.00"), date(2026, 3, 10)),
            ("installment", D("10000.00"), date(2026, 5, 10)),
            ("installment", D("10000.00"), date(2026, 7, 10)),
            ("fee", D("50000.00"), None)])
        self.assertEqual(sum(r.amount for r in rows), D("100000.00"))
        self.assertEqual(c.withhold_target, D("50000.00"))

    def test_installment_rounding_goes_to_last_payment(self):
        c = make_contract(self.fr, product=self.market, scheme="hybrid", fee=D("10000"), signed=self.signed,
                          installments_total=D("10000"), installments_count=3, upfront_amount=0)
        self.assertEqual([r.amount for r in c.charges.filter(kind="installment").order_by("due_date")],
                         [D("3333.33"), D("3333.33"), D("3333.34")])

    def test_preset_fills_parts_via_api_and_parts_can_be_edited(self):
        r = self.admin.post("/api/v1/contracts/", {"franchisee": self.fr.id, "product": self.market.id,
                                                   "payment_scheme": "cash"}, format="json")
        self.assertEqual((r.json()["upfront_amount"], r.json()["installments_total"], r.json()["withhold_target"]),
                         ("35000.00", "0.00", "0.00"))
        r = self.admin.post("/api/v1/contracts/", {"franchisee": make_fr("Б", "Бишкек").id, "product": self.market.id,
                                                   "payment_scheme": "per_sale", "discount_note": "Партнёрская акция"},
                            format="json")
        self.assertEqual((r.json()["upfront_amount"], r.json()["withhold_target"], r.json()["fee_total"],
                          r.json()["discount_note"]), ("0.00", "50000.00", "50000.00", "Партнёрская акция"))
        r = self.admin.post("/api/v1/contracts/", {
            "franchisee": make_fr("В", "Нарын").id, "product": self.market.id, "payment_scheme": "hybrid",
            "fee_total": "40000", "upfront_amount": "10000", "installments_total": "10000", "installments_count": 2,
            "withhold_percent": "20"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.json()["withhold_target"], r.json()["payment_scheme"]), ("20000.00", "hybrid"))

    def test_validation_of_parts(self):
        body = {"franchisee": self.fr.id, "product": self.market.id, "payment_scheme": "hybrid", "fee_total": "40000"}
        for extra in ({"upfront_amount": "30000", "installments_total": "20000", "installments_count": 2},
                      {"installments_total": "10000"},  # рассрочка без количества платежей
                      {"upfront_amount": "10000", "withhold_percent": "0"},  # остаток есть, удерживать нечем
                      {"upfront_amount": "-1"}):
            self.assertEqual(self.admin.post("/api/v1/contracts/", {**body, **extra}, format="json").status_code,
                             400, extra)
        ok = {"upfront_amount": "40000", "withhold_percent": "0"}  # остатка нет — удержание 0 допустимо
        self.assertEqual(self.admin.post("/api/v1/contracts/", {**body, **ok}, format="json").status_code, 201)

    def test_payoff_reported_per_part(self):
        c = self.hybrid()
        FranchiseePayment.objects.create(franchisee=self.fr, contract=c, amount=D("25000"), method="bank")
        client = make_client(self.fr)
        create_client_sale(client=client, product=self.market, kind="setup", amount=D("10000"),
                           date=today())  # доход 10000, удержано 3000
        r = self.admin.get(f"/api/v1/contracts/{c.id}/").json()
        self.assertEqual(r["payoff"], {"upfront": {"paid": "20000.00", "total": "20000.00"},
                                       "installments": {"paid": "5000.00", "total": "30000.00"},
                                       "withholding": {"paid": "3000.00", "total": "50000.00"}})
        self.assertEqual((r["paid_off_amount"], r["remaining_amount"]), ("28000.00", "72000.00"))

    def test_cap_equals_remainder_not_fee_total(self):
        make_contract(self.fr, product=self.market, scheme="hybrid", fee=D("10000"), signed=self.signed,
                      upfront_amount=D("9500"))
        s, _ = create_client_sale(client=make_client(self.fr), product=self.market, kind="setup", amount=D("10000"))
        self.assertEqual(s.withheld_amount, D("500.00"))
        s2, _ = create_client_sale(client=make_client(self.fr), product=self.market, kind="setup", amount=D("10000"))
        self.assertEqual(s2.withheld_amount, D("0.00"))
        self.assertEqual(sum(p.amount for p in FranchiseePayment.objects.filter(method="offset")), D("500.00"))

    def test_manual_overpayment_reduces_remainder_to_withhold(self):
        c = make_contract(self.fr, product=self.market, scheme="per_sale", fee=D("5000"))
        FranchiseePayment.objects.create(franchisee=self.fr, contract=c, amount=D("4800"), method="bank")
        s, _ = create_client_sale(client=make_client(self.fr), product=self.market, kind="setup", amount=D("10000"))
        self.assertEqual(s.withheld_amount, D("200.00"))  # осталось 200 из 5000

    def test_active_contract_parts_frozen_but_note_editable(self):
        c = self.hybrid()
        r = self.admin.patch(f"/api/v1/contracts/{c.id}/", {"upfront_amount": "1"}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.admin.patch(f"/api/v1/contracts/{c.id}/", {"withhold_percent": "40", "discount_note": "x"},
                             format="json")
        self.assertEqual((r.status_code, r.json()["withhold_percent"]), (200, "40.00"))


class EquipmentAndKindsTests(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.fr = make_fr()
        self.client_ = make_client(self.fr)
        self.pos = Equipment.objects.create(name="POS-терминал", price=D("12000"), product=self.market)
        self.owner = api(make_user(self.fr, "franchisee"))
        self.admin = api(make_user(role="super_admin"))

    def post(self, who=None, **kw):
        body = {"client": self.client_.id, "product": self.market.id, **kw}
        return (who or self.owner).post("/api/v1/client-sales/", body, format="json")

    def test_equipment_amount_defaults_to_price_times_quantity(self):
        r = self.post(kind="equipment", equipment=self.pos.id, quantity="3")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.json()["amount"], r.json()["franchisee_income"], r.json()["equipment_name"],
                          r.json()["quantity"]), ("36000.00", "36000.00", "POS-терминал", "3.000"))
        r = self.post(kind="equipment", equipment=self.pos.id)
        self.assertEqual(r.json()["amount"], "12000.00")
        r = self.post(kind="equipment", equipment=self.pos.id, quantity="2", amount="20000")  # цену можно изменить
        self.assertEqual(r.json()["amount"], "20000.00")
        self.assertEqual(self.post(kind="equipment").status_code, 400)  # без позиции и суммы

    def test_equipment_validations(self):
        self.assertEqual(self.post(kind="setup", equipment=self.pos.id, amount="10").status_code, 400)
        self.pos.is_active = False
        self.pos.save()
        self.assertEqual(self.post(kind="equipment", equipment=self.pos.id).status_code, 400)

    def test_franchisee_manual_kinds(self):
        for kind in ("setup", "equipment", "other"):
            self.assertEqual(self.post(kind=kind, amount="1000").status_code, 201, kind)
        for kind in ("subscription", "renewal", "license", "service"):
            self.assertEqual(self.post(kind=kind, amount="1000").status_code, 400, kind)
        self.assertEqual(self.post(self.admin, kind="subscription", amount="1000").status_code, 201)

    def test_equipment_catalog_permissions(self):
        body = {"name": "Сканер", "price": "3500", "unit": "шт"}
        self.assertEqual(self.admin.post("/api/v1/equipment/", body, format="json").status_code, 201)
        self.assertEqual(self.owner.post("/api/v1/equipment/", body, format="json").status_code, 403)
        self.assertEqual(self.owner.get("/api/v1/equipment/").json()["count"], 2)
        self.assertEqual(self.admin.get("/api/v1/equipment/", {"product": "market"}).json()["count"], 1)
        self.post(kind="equipment", equipment=self.pos.id)
        self.assertEqual(self.admin.delete(f"/api/v1/equipment/{self.pos.id}/").status_code, 400)

    def test_default_income_100_percent_for_setup_and_other(self):
        r = self.post(kind="setup", amount="8000").json()
        self.assertEqual((r["franchisee_income"], r["commission_percent"], r["commission_amount"], r["withheld_amount"],
                          r["net_income"]), ("8000.00", "100.00", "0.00", "0.00", "8000.00"))

    def test_migration_defaults_kind_enum_has_no_license(self):
        self.assertEqual(self.post(kind="license", amount="1").status_code, 400)
        self.assertNotIn("license", ClientSale.Kind.values)


class WithholdingRefundTests(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.fr = make_fr()
        self.client_ = make_client(self.fr)
        self.contract = make_contract(self.fr, product=self.market, scheme="per_sale", fee=D("20000"))

    def test_setup_refund_reverses_withholding_and_charge_proportionally(self):
        s, _ = create_client_sale(client=self.client_, product=self.market, kind="setup", amount=D("10000"))
        self.assertEqual(s.withheld_amount, D("3000.00"))
        r1 = refund_sale(s, D("4000"), external_payment_id="r1")
        self.assertEqual((r1.franchisee_income, r1.withheld_amount), (D("-4000.00"), D("-1200.00")))
        r2 = refund_sale(s, D("6000"), external_payment_id="r2")
        self.assertEqual((r2.franchisee_income, r2.withheld_amount), (D("-6000.00"), D("-1800.00")))
        self.assertEqual(sum(c.amount for c in Charge.objects.filter(kind="withhold")), D("0.00"))
        self.assertEqual(sum(p.amount for p in FranchiseePayment.objects.filter(method="offset")), D("0.00"))
        self.assertEqual(franchisee_finance(self.fr.id)["balance"], D("20000.00"))
        # возврат учтён в отчёте: итог по продаже и возвратам — ноль
        d = api(make_user(role="super_admin")).get("/api/v1/reports/sales/").json()["totals"]
        self.assertEqual((d["amount"], d["income"], d["withheld"], d["net_income"]),
                         ("0.00", "0.00", "0.00", "0.00"))

    def test_subscription_refund_reverses_commission_payout(self):
        s, _ = create_client_sale(client=self.client_, product=self.market, kind="subscription", amount=D("10000"),
                                  source="integration", external_payment_id="p1")
        self.assertEqual((s.franchisee_income, s.withheld_amount), (D("2000.00"), D("600.00")))
        refund_sale(s, D("10000"), external_payment_id="p1:refund")
        from billing.models import CommissionAccrual  # C6: исходное начисление + сторно (отрицательное)
        accs = list(CommissionAccrual.objects.all())
        self.assertEqual(len(accs), 2)
        self.assertEqual((sum(a.amount for a in accs), sum(a.withheld_amount for a in accs)),
                         (D("0.00"), D("0.00")))
        self.assertEqual(Charge.objects.filter(kind="withhold").count(), 0)
        f = franchisee_finance(self.fr.id)
        self.assertEqual((f["balance"], f["commission_due"]), (D("20000.00"), D("0.00")))


class IncomeReportsAndDashboardTests(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.opt = make_product("opt")
        self.fr = make_fr("A", "Ош")
        self.fr2 = make_fr("B", "Бишкек")
        make_contract(self.fr, product=self.market, scheme="per_sale", fee=D("50000"))
        make_contract(self.fr2, product=self.opt, scheme="cash")
        self.c1, self.c2 = make_client(self.fr), make_client(self.fr2)
        create_client_sale(client=self.c1, product=self.market, kind="setup", amount=D("10000"))  # 10000 / удерж. 3000
        create_client_sale(client=self.c1, product=self.market, kind="equipment", amount=D("5000"))  # 5000 / 1500
        create_client_sale(client=self.c1, product=self.market, kind="subscription", amount=D("10000"))  # 2000 / 600
        create_client_sale(client=self.c2, product=self.opt, kind="renewal", amount=D("4000"))  # 800 / 0
        create_client_sale(client=self.c2, product=self.opt, kind="other", amount=D("1000"))  # 1000 / 0

    def test_report_totals_by_kind_and_breakdowns(self):
        d = api(make_user(role="finance")).get("/api/v1/reports/sales/").json()
        t = d["totals"]
        self.assertEqual((t["count"], t["amount"], t["income"], t["withheld"], t["net_income"]),
                         (5, "30000.00", "18800.00", "5100.00", "13700.00"))
        self.assertEqual({k["kind"]: (k["count"], k["amount"], k["income"]) for k in t["by_kind"]},
                         {"setup": (1, "10000.00", "10000.00"), "training": (0, "0.00", "0.00"), "equipment": (1, "5000.00", "5000.00"),
                          "subscription": (2, "14000.00", "2800.00"), "other": (1, "1000.00", "1000.00")})
        self.assertEqual([k["label"] for k in t["by_kind"]],
                         ["Установка и обучение", "Обучение", "Оборудование", "Абонплата", "Прочее"])
        by_fr = {f["name"]: f for f in d["by_franchisee"]}
        self.assertEqual((by_fr["A"]["income"], by_fr["A"]["withheld"], by_fr["A"]["net_income"]),
                         ("17000.00", "5100.00", "11900.00"))
        self.assertEqual((by_fr["B"]["income"], by_fr["B"]["net_income"]), ("1800.00", "1800.00"))
        by_product = {p["product"]: p for p in d["by_product"]}
        self.assertEqual((by_product["market"]["income"], by_product["market"]["withheld"]), ("17000.00", "5100.00"))
        self.assertEqual(d["by_month"][-1]["net_income"], "13700.00")
        own = api(make_user(self.fr2, "franchisee")).get("/api/v1/reports/sales/").json()["totals"]
        self.assertEqual((own["income"], own["withheld"], own["net_income"]), ("1800.00", "0.00", "1800.00"))
        filtered = api(make_user(role="super_admin")).get("/api/v1/reports/sales/", {"product": "opt"}).json()["totals"]
        self.assertEqual((filtered["income"], filtered["count"]), ("1800.00", 2))

    def test_dashboards_income_fields(self):
        hq = api(make_user(role="super_admin")).get("/api/v1/dashboard/").json()
        self.assertEqual((hq["network_income_month"], hq["withheld_month"]), ("18800.00", "5100.00"))
        a = api(make_user(self.fr, "franchisee")).get("/api/v1/dashboard/").json()
        self.assertEqual((a["income_month"], a["withheld_month"]), ("17000.00", "5100.00"))
        b = api(make_user(self.fr2, "franchisee")).get("/api/v1/dashboard/").json()
        self.assertEqual((b["income_month"], b["withheld_month"]), ("1800.00", "0.00"))

    def test_sales_list_exposes_income_columns(self):
        rows = api(make_user(self.fr, "franchisee")).get("/api/v1/client-sales/").json()["results"]
        row = next(r for r in rows if r["kind"] == "setup")
        for key in ("franchisee_income", "withheld_amount", "net_income", "list_amount", "discount_amount",
                    "commission_rule_name", "tariff_name"):
            self.assertIn(key, row)
        self.assertEqual((row["franchisee_income"], row["withheld_amount"], row["net_income"]),
                         ("10000.00", "3000.00", "7000.00"))
        self.assertEqual(api(make_user(self.fr, "franchisee")).get("/api/v1/client-sales/", {"kind": "equipment"})
                         .json()["count"], 1)


