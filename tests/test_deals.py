"""P0+: продажа одной формой (Deal), продажи OZOK, доходы/расходы партнёра, аналитика."""
from decimal import Decimal

from billing.models import Charge, LedgerCategory
from clients.models import ClientSale, Deal
from core.models import ChangeRequest
from core.testing import api, make_client, make_contract, make_fr, make_product, make_rule, make_user
from leads.services import create_lead
from network.commission import create_tariff
from network.models import Equipment

from .test_nbs_core import Base, nbs

D = "/api/v1/deals/"


class DealBase(Base):
    def setUp(self):
        super().setUp()
        self.market.setup_price = Decimal("10000")
        self.market.training_price = Decimal("2000")
        self.market.save()
        self.tariff = create_tariff(product=self.market, code="start", name="Старт", monthly_price=1500)
        self.eq = Equipment.objects.create(name="POS", price=5000, min_price=4000)
        make_rule(name="Подписка 20%", sale_kind="subscription", percent=20)
        make_rule(name="Установка 100%", sale_kind="setup", percent=100)
        make_rule(name="Оборудование 100%", sale_kind="equipment", percent=100)
        make_contract(self.fr, product=self.market, scheme="cash")
        self.client_ = make_client(self.fr)

    def body(self, **kw):
        data = {"client": self.client_.id, "product": self.market.id, "lines": [
            {"kind": "setup", "discount_percent": "10"},
            {"kind": "equipment", "equipment": self.eq.id, "quantity": 2},
            {"kind": "subscription", "tariff": self.tariff.id, "months": 12}]}
        data.update(kw)
        return data


class DealPreviewAndConfirmTests(DealBase):
    def test_preview_per_line_numbers(self):
        r = self.O.post(D + "preview/", self.body(), format="json")
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        setup, eq, sub = d["lines"]
        self.assertEqual((setup["list_amount"], setup["line_discount"], setup["total"], setup["partner_income"]),
                         ("10000.00", "1000.00", "9000.00", "9000.00"))
        self.assertEqual((eq["list_amount"], eq["total"], eq["partner_income"]), ("10000.00", "10000.00", "10000.00"))
        self.assertEqual((sub["list_amount"], sub["total"], sub["partner_income"], sub["pays_commission_on_payment"]),
                         ("18000.00", "18000.00", "3600.00", True))
        self.assertEqual((sub["nbs_income"]), "14400.00")
        t = d["totals"]
        self.assertEqual((t["list_total"], t["discount_total"], t["total"], t["partner_income"]),
                         ("38000.00", "1000.00", "37000.00", "22600.00"))
        self.assertFalse(d["needs_approval"])
        self.assertEqual(Deal.objects.count(), 0)  # предпросмотр ничего не сохраняет

    def test_confirm_creates_sales_for_setup_and_equipment_only(self):
        r = self.O.post(D, self.body(), format="json")
        self.assertEqual(r.status_code, 201, r.content)
        deal = r.json()
        self.assertEqual((deal["status"], deal["total"], deal["partner"]), ("draft", "37000.00", self.fr.id))
        self.assertEqual(ClientSale.objects.count(), 0)
        r = self.O.post(f"{D}{deal['id']}/confirm/")
        self.assertEqual((r.status_code, r.json()["status"]), (200, "confirmed"))
        sales = ClientSale.objects.filter(deal_id=deal["id"]).order_by("id")
        self.assertEqual([(s.kind, s.amount, s.franchisee_income) for s in sales],
                         [("setup", Decimal("9000.00"), Decimal("9000.00")),
                          ("equipment", Decimal("10000.00"), Decimal("10000.00"))])
        self.assertEqual(ClientSale.objects.filter(kind__in=("subscription", "renewal")).count(), 0)
        sub_line = [l for l in r.json()["items"] if l["kind"] == "subscription"][0]
        self.assertEqual((sub_line["subscription_status"], sub_line["sale"]), ("awaiting_payment", None))
        self.assertEqual(r.json()["partner_income_total"], "19000.00")  # комиссия с абонплаты — при оплате
        self.assertEqual(self.O.post(f"{D}{deal['id']}/confirm/").status_code, 400)

    def test_nbs_lead_share_in_preview_and_confirm(self):
        make_rule(name="Лид OZOK: установка 70%", sale_kind="setup", lead_source="nbs_ads", percent=70, basis="b")
        make_rule(name="Лид OZOK: абонплата 15%", sale_kind="subscription", lead_source="nbs_ads", percent=15, basis="b")
        lead = create_lead(franchisee=self.fr, name="Л", phone="1", source="nbs_ads", interest="program")
        body = self.body(lead=lead.id)
        body["lines"][0].pop("discount_percent")
        p = self.O.post(D + "preview/", body, format="json").json()
        setup, _, sub = p["lines"]
        self.assertEqual((setup["partner_income"], setup["nbs_share"], setup["partner_net"]),
                         ("10000.00", "3000.00", "7000.00"))
        self.assertEqual(sub["partner_income"], "2700.00")  # 15% вместо 20%
        deal = self.O.post(D, body, format="json").json()
        self.O.post(f"{D}{deal['id']}/confirm/")
        self.assertEqual(Charge.objects.filter(kind="nbs_lead_share").count(), 1)

    def test_deal_discount_is_spread_across_lines(self):
        self.market.max_discount_percent = Decimal("50")
        self.market.save()
        body = self.body(deal_discount_amount="1000")
        body["lines"] = body["lines"][:2]
        body["lines"][0].pop("discount_percent")
        d = self.O.post(D, body, format="json").json()
        shares = [l["deal_discount_share"] for l in d["items"]]
        self.assertEqual(shares, ["500.00", "500.00"])
        self.assertEqual((d["list_total"], d["discount_total"], d["total"]), ("20000.00", "1000.00", "19000.00"))
        self.assertEqual(self.O.post(D, self.body(deal_discount_percent="5", deal_discount_amount="10"),
                                     format="json").status_code, 400)  # процент или сумма

    def test_manager_gets_no_subscription_commission_in_preview(self):
        mgr = make_fr("Менеджер", "Ош", status="manager")
        client = make_client(mgr)
        M = api(make_user(mgr, "manager"))
        p = M.post(D + "preview/", self.body(client=client.id), format="json").json()
        self.assertEqual(p["lines"][2]["partner_income"], "0.00")
        self.assertEqual(p["lines"][0]["partner_income"], "9000.00")

    def test_validation(self):
        bad = self.body()
        bad["lines"] = []
        self.assertEqual(self.O.post(D, bad, format="json").status_code, 400)
        bad["lines"] = [{"kind": "subscription", "tariff": self.tariff.id}]  # нет месяцев
        self.assertEqual(self.O.post(D, bad, format="json").status_code, 400)
        bad["lines"] = [{"kind": "equipment"}]
        self.assertEqual(self.O.post(D, bad, format="json").status_code, 400)
        foreign = make_client(self.other)
        self.assertEqual(self.O.post(D, self.body(client=foreign.id), format="json").status_code, 400)
        new = self.body()
        new.pop("client")
        new["new_client"] = {"business_name": "Новый клиент", "phone": "0555"}
        r = self.O.post(D, new, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(r.json()["client"])

    def test_draft_edit_only(self):
        d = self.O.post(D, self.body(), format="json").json()
        r = self.O.patch(f"{D}{d['id']}/", {"lines": [{"kind": "setup"}]}, format="json")
        self.assertEqual((r.status_code, r.json()["total"]), (200, "10000.00"))
        self.O.post(f"{D}{d['id']}/confirm/")
        self.assertEqual(self.O.patch(f"{D}{d['id']}/", {"comment": "x"}, format="json").status_code, 400)
        self.assertEqual(self.O.delete(f"{D}{d['id']}/").status_code, 405)


class DealApprovalTests(DealBase):
    def test_discount_above_max_goes_to_approval(self):
        body = self.body()
        body["lines"][0]["discount_percent"] = "30"
        d = self.O.post(D, body, format="json").json()
        self.assertIn("скидка", d["items"][0]["violation"])
        r = self.O.post(f"{D}{d['id']}/confirm/")
        self.assertEqual(r.json()["status"], "pending_approval")
        self.assertEqual(ClientSale.objects.count(), 0)
        cr = ChangeRequest.objects.get(entity="clients.Deal", entity_id=d["id"])
        self.assertEqual((cr.status, cr.field), ("pending", "approval"))
        self.assertEqual(self.O.post(f"{D}{d['id']}/confirm/").status_code, 400)
        self.assertEqual(self.O.post(f"/api/v1/change-requests/{cr.id}/approve/").status_code, 403)
        r = self.H.post(f"/api/v1/change-requests/{cr.id}/approve/", {"comment": "ок"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        deal = Deal.objects.get(pk=d["id"])
        self.assertEqual((deal.status, deal.approved_by_id), ("confirmed", self.head.id))
        self.assertEqual(ClientSale.objects.filter(deal=deal).count(), 2)

    def test_reject_returns_to_draft(self):
        body = self.body()
        body["lines"][0]["discount_percent"] = "30"
        d = self.O.post(D, body, format="json").json()
        self.O.post(f"{D}{d['id']}/confirm/")
        cr = ChangeRequest.objects.get(entity="clients.Deal", entity_id=d["id"])
        self.H.post(f"/api/v1/change-requests/{cr.id}/reject/", {"comment": "Слишком большая"}, format="json")
        deal = Deal.objects.get(pk=d["id"])
        self.assertEqual(deal.status, "draft")
        self.assertIn("Слишком большая", deal.approval_note)

    def test_min_price_for_tariff_and_equipment(self):
        self.tariff.min_price = Decimal("1400")
        self.tariff.save()
        body = self.body()
        body["lines"] = [{"kind": "subscription", "tariff": self.tariff.id, "months": 10, "discount_percent": "9"},
                         {"kind": "equipment", "equipment": self.eq.id, "quantity": 1, "discount_percent": "9"}]
        # 1500*0.91 = 1365 < 1400; 5000*0.91 = 4550 >= 4000 — только тариф нарушает минимум
        d = self.O.post(D + "preview/", body, format="json").json()
        self.assertTrue(d["needs_approval"])
        self.assertIn("ниже минимальной", d["lines"][0]["violation"])
        self.assertEqual(d["lines"][1]["violation"], "")

    def test_cancel_confirmed_deal_cancels_sales_nbs_only(self):
        d = self.O.post(D, self.body(), format="json").json()
        self.O.post(f"{D}{d['id']}/confirm/")
        self.assertEqual(self.O.post(f"{D}{d['id']}/cancel/", {"reason": "Ошибка"}, format="json").status_code, 400)
        self.assertEqual(self.A.post(f"{D}{d['id']}/cancel/", {}, format="json").status_code, 400)
        r = self.A.post(f"{D}{d['id']}/cancel/", {"reason": "Клиент отказался"}, format="json")
        self.assertEqual((r.status_code, r.json()["status"]), (200, "cancelled"))
        self.assertEqual(ClientSale.objects.filter(deal_id=d["id"]).count(), 0)
        self.assertEqual(ClientSale.all_objects.filter(deal_id=d["id"]).count(), 2)
        draft = self.O.post(D, self.body(), format="json").json()
        self.assertEqual(self.O.post(f"{D}{draft['id']}/cancel/", {"reason": "Передумал"}, format="json")
                         .status_code, 200)


class NbsDirectSalesTests(DealBase):
    def test_nbs_direct_sale_has_no_partner_no_commission(self):
        body = {"product": self.market.id, "client_name": "Кафе Достук", "lines": [
            {"kind": "setup"}, {"kind": "subscription", "tariff": self.tariff.id, "months": 3}]}
        r = self.A.post(D, body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        d = r.json()
        self.assertEqual((d["partner"], d["partner_name"], d["is_nbs_direct"]), (None, "OZOK (прямые продажи)", True))
        self.assertEqual((d["total"], d["partner_income_total"], d["nbs_income_total"]),
                         ("14500.00", "0.00", "14500.00"))
        r = self.A.post(f"{D}{d['id']}/confirm/")
        self.assertEqual(r.json()["status"], "confirmed")
        self.assertEqual(ClientSale.objects.count(), 0)  # комиссий и продаж партнёра нет
        self.assertEqual(self.A.get(D, {"nbs_direct": "true"}).json()["count"], 1)
        self.assertEqual(self.O.get(D).json()["count"], 0)  # партнёр прямых продаж OZOK не видит
        self.assertEqual(self.O.get(f"{D}{d['id']}/").status_code, 404)

    def test_nbs_direct_requires_client_name_and_nbs_role(self):
        body = {"product": self.market.id, "lines": [{"kind": "setup"}]}
        self.assertEqual(self.A.post(D, body, format="json").status_code, 400)
        body["client_name"] = "X"
        self.assertEqual(api(nbs("quality")).post(D, body, format="json").status_code, 403)
        self.assertEqual(self.H.post(D, body, format="json").status_code, 201)
        # партнёр не может создать продажу «от OZOK»: партнёр подставляется сервером
        mine = self.O.post(D, {**body, "partner": None, "client": self.client_.id}, format="json").json()
        self.assertEqual(mine["partner"], self.fr.id)

    def test_partner_isolation(self):
        d = self.O.post(D, self.body(), format="json").json()
        O2 = api(make_user(self.other, "franchisee"))
        self.assertEqual(O2.get(D).json()["count"], 0)
        self.assertEqual(O2.get(f"{D}{d['id']}/").status_code, 404)
        self.assertEqual(self.A.get(D).json()["count"], 1)


class LedgerTests(DealBase):
    def test_categories_seeded(self):
        names = set(LedgerCategory.objects.filter(type="expense").values_list("name", flat=True))
        self.assertEqual(names, {"Аренда", "Зарплата", "Реклама/маркетинг", "Транспорт", "Закупка оборудования",
                                 "Связь и интернет", "Налоги", "Прочее"})
        self.assertTrue(LedgerCategory.objects.filter(type="income", name="Прочий доход").exists())
        self.assertEqual(self.O.get("/api/v1/ledger-categories/").json()["count"], 9)
        self.assertEqual(self.O.post("/api/v1/ledger-categories/", {"type": "expense", "name": "Х"},
                                     format="json").status_code, 403)
        self.assertEqual(self.A.post("/api/v1/ledger-categories/", {"type": "expense", "name": "Охрана"},
                                     format="json").status_code, 201)

    def cat(self, name):
        return LedgerCategory.objects.get(name=name).id

    def test_entries_cancel_only_and_report(self):
        e = self.O.post("/api/v1/ledger/", {"type": "expense", "category": self.cat("Аренда"), "amount": "5000",
                                            "description": "Офис"}, format="json")
        self.assertEqual(e.status_code, 201, e.content)
        self.O.post("/api/v1/ledger/", {"type": "expense", "category": self.cat("Реклама/маркетинг"),
                                        "amount": "2000"}, format="json")
        self.O.post("/api/v1/ledger/", {"type": "income", "category": self.cat("Прочий доход"), "amount": "1000"},
                    format="json")
        self.assertEqual(self.O.post("/api/v1/ledger/", {"type": "income", "category": self.cat("Аренда"),
                                                         "amount": "1"}, format="json").status_code, 400)
        deal = self.O.post(D, self.body(), format="json").json()
        self.O.post(f"{D}{deal['id']}/confirm/")  # доходы от продаж — автоматически: 9000 + 10000
        r = self.O.get("/api/v1/reports/ledger/").json()
        self.assertEqual(r["sales_income"]["total"], "19000.00")
        self.assertEqual((r["other_income"]["total"], r["expenses"]["total"], r["expenses"]["marketing"]),
                         ("1000.00", "7000.00", "2000.00"))
        self.assertEqual(r["profit"], "13000.00")  # 19000 + 1000 − 7000
        self.assertEqual(r["by_month"][-1]["profit"], "13000.00")
        # отмена вместо удаления: запись исчезает из отчёта, но остаётся в базе
        self.assertEqual(self.O.delete(f"/api/v1/ledger/{e.json()['id']}/").status_code, 405)
        self.assertEqual(self.O.post(f"/api/v1/ledger/{e.json()['id']}/cancel/", {}, format="json").status_code, 400)
        self.assertEqual(self.O.post(f"/api/v1/ledger/{e.json()['id']}/cancel/", {"reason": "Ошибка"},
                                     format="json").status_code, 200)
        r = self.O.get("/api/v1/reports/ledger/").json()
        self.assertEqual((r["expenses"]["total"], r["profit"]), ("2000.00", "18000.00"))
        from billing.models import PartnerLedgerEntry
        self.assertEqual(PartnerLedgerEntry.all_objects.count(), 3)

    def test_nbs_share_reduces_profit_and_isolation(self):
        make_rule(name="Лид OZOK: установка 70%", sale_kind="setup", lead_source="nbs_ads", percent=70, basis="b")
        lead = create_lead(franchisee=self.fr, name="Л", phone="1", source="nbs_ads", interest="program")
        body = {"client": self.client_.id, "product": self.market.id, "lead": lead.id, "lines": [{"kind": "setup"}]}
        deal = self.O.post(D, body, format="json").json()
        self.O.post(f"{D}{deal['id']}/confirm/")
        r = self.O.get("/api/v1/reports/ledger/").json()
        self.assertEqual((r["deductions"]["nbs_share"], r["profit"]), ("3000.00", "7000.00"))
        # изоляция: чужой партнёр видит только своё; OZOK — любого и сеть целиком
        O2 = api(make_user(self.other, "franchisee"))
        self.assertEqual(O2.get("/api/v1/reports/ledger/", {"partner": self.fr.id}).json()["profit"], "0.00")
        self.assertEqual(self.A.get("/api/v1/reports/ledger/", {"partner": self.fr.id}).json()["profit"], "7000.00")
        self.assertEqual(self.F.get("/api/v1/reports/ledger/").json()["profit"], "7000.00")
        self.assertEqual(self.H.get("/api/v1/reports/ledger/", {"date_from": "bad"}).status_code, 400)
        e = self.O.post("/api/v1/ledger/", {"type": "expense", "category": self.cat("Налоги"), "amount": "10"},
                        format="json").json()
        self.assertEqual(O2.get(f"/api/v1/ledger/{e['id']}/").status_code, 404)
        self.assertEqual(self.A.get("/api/v1/ledger/").json()["count"], 1)
        self.assertEqual(api(make_user(self.fr, "franchisee_staff")).get("/api/v1/reports/ledger/").status_code, 403)


class AnalyticsTests(DealBase):
    def setUp(self):
        super().setUp()
        self.product2 = make_product("coca")
        # партнёр 2 тоже франчайзи с договором и продажами
        make_contract(self.other, product=self.market, scheme="cash")
        self.client2 = make_client(self.other)
        for fr, client, amount in ((self.fr, self.client_, 9000), (self.other, self.client2, 4000)):
            from clients.services import create_client_sale
            create_client_sale(client=client, product=self.market, kind="setup", amount=amount)
        create_lead(franchisee=self.fr, name="Л1", phone="1", product="market", interest="program")

    def test_partner_analytics_by_sphere_and_isolation(self):
        d = self.O.get("/api/v1/reports/partner-analytics/").json()
        self.assertEqual(d["partner"]["id"], self.fr.id)
        by_code = {s["code"]: s for s in d["spheres"]}
        self.assertEqual((by_code["market"]["sales"]["count"], by_code["market"]["sales"]["amount"]), (1, "9000.00"))
        self.assertEqual(by_code["coca"]["sales"]["count"], 0)
        self.assertEqual(by_code["market"]["leads"]["total"], 1)
        self.assertEqual(d["totals"]["sales_amount"], "9000.00")
        self.assertEqual(d["network_average"]["partners"], 2)
        self.assertEqual(d["network_average"]["sales_amount"], "6500.00")  # (9000 + 4000) / 2
        # чужого партнёра не получить, даже указав id
        mine = self.O.get("/api/v1/reports/partner-analytics/", {"partner": self.other.id}).json()
        self.assertEqual(mine["partner"]["id"], self.fr.id)
        other = self.A.get("/api/v1/reports/partner-analytics/", {"partner": self.other.id}).json()
        self.assertEqual(other["totals"]["sales_amount"], "4000.00")
        self.assertEqual(self.A.get("/api/v1/reports/partner-analytics/").status_code, 400)
        self.assertEqual(api(nbs("recruiter")).get("/api/v1/reports/partner-analytics/",
                                                   {"partner": self.fr.id}).status_code, 403)

    def test_sphere_analytics_rank_nbs_row_and_partner_view(self):
        self.A.post(D, {"product": self.market.id, "client_name": "Прямой", "lines": [{"kind": "setup"}]},
                    format="json")
        deal = Deal.objects.get()
        self.A.post(f"{D}{deal.id}/confirm/")
        r = self.H.get("/api/v1/reports/sphere-analytics/", {"product": "market"}).json()
        self.assertEqual([(x["name"], x["rank"], x["revenue"]) for x in r["rows"]],
                         [(self.fr.name, 1, "9000.00"), (self.other.name, 2, "4000.00")])
        self.assertEqual((r["nbs_direct"]["name"], r["nbs_direct"]["revenue"], r["nbs_direct"]["sales_count"]),
                         ("OZOK (прямые продажи)", "10000.00", 1))
        self.assertEqual(r["totals"]["revenue"], "23000.00")
        self.assertEqual(self.H.get("/api/v1/reports/sphere-analytics/", {"product": self.market.id, "tier": "growth"})
                         .json()["participants"], 0)
        # партнёр видит только себя, своё место и число участников
        p = self.O.get("/api/v1/reports/sphere-analytics/", {"product": "market"}).json()
        self.assertEqual((p["participants"], p["me"]["rank"], len(p["rows"]), p["me"]["name"]), (2, 1, 1, self.fr.name))
        self.assertNotIn("nbs_direct", p)
        O2 = api(make_user(self.other, "franchisee")).get("/api/v1/reports/sphere-analytics/", {"product": "market"}).json()
        self.assertEqual((O2["me"]["rank"], O2["me"]["revenue"]), (2, "4000.00"))
        self.assertEqual(self.O.get("/api/v1/reports/sphere-analytics/").status_code, 400)
        self.assertEqual(self.O.get("/api/v1/reports/sphere-analytics/", {"product": "nope"}).status_code, 400)
        self.assertEqual(self.F.get("/api/v1/reports/sphere-analytics/", {"product": "market"}).status_code, 200)
