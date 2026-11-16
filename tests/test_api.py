"""Сквозной сценарий 11.3, дашборды, отчёты, права ролей, формы ответов."""
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from billing.models import CommissionPayout, FranchiseePayment
from clients.models import ClientSale
from core.models import add_months, month_first, month_last, today
from core.testing import api, days_ago, make_client, make_contract, make_fr, make_package, make_product, make_user
from integrations.models import IntegrationEvent
from leads.models import Lead
from tasks.models import Task

D = Decimal
MONTH = today().strftime("%Y-%m")


class AuthTests(TestCase):
    def test_login_refresh_me(self):
        fr = make_fr()
        user = make_user(fr, "franchisee", email="osh@ozok.kg")
        user.set_password("demo12345")
        user.save()
        c = api()
        r = c.post("/api/v1/auth/login/", {"email": "osh@ozok.kg", "password": "demo12345"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(r.json()), {"access", "refresh", "user"})
        self.assertEqual(set(r.json()["user"]), {"id", "email", "first_name", "last_name", "full_name", "phone",
                                                 "role", "region", "partner", "is_hq", "franchisee", "otp_enabled",
                                                 "otp_available", "must_change_password",
                                                 "access_role", "permissions", "modules", "actions", "scope", "language", "theme"})
        self.assertEqual(r.json()["user"]["franchisee"], {"id": fr.id, "name": fr.name, "city": fr.city,
                                                          "status": "franchisee"})
        self.assertFalse(r.json()["user"]["is_hq"])
        self.assertEqual(c.post("/api/v1/auth/login/", {"email": "osh@ozok.kg", "password": "bad"},
                                format="json").status_code, 401)
        new = c.post("/api/v1/auth/refresh/", {"refresh": r.json()["refresh"]}, format="json")
        self.assertIn("access", new.json())
        c.credentials(HTTP_AUTHORIZATION="Bearer " + r.json()["access"])
        self.assertEqual(c.get("/api/v1/auth/me/").json()["email"], "osh@ozok.kg")
        hq = api(make_user(role="super_admin"))
        self.assertIsNone(hq.get("/api/v1/auth/me/").json()["franchisee"])
        self.assertTrue(hq.get("/api/v1/auth/me/").json()["is_hq"])
        self.assertEqual(api().get("/api/v1/dashboard/").status_code, 401)


class EndToEndScenarioTests(TestCase):
    """Раздел 11.3: заявка из Оша -> франчайзи -> демо -> клиент -> оплата из продукта -> вознаграждение ->
    платёж франчайзи -> баланс и дашборды обновились."""

    def setUp(self):
        cache.clear()
        self.market = make_product("market", price_cash=35000)
        self.osh = make_fr("OZOK Ош", "Ош", ["Ош"], code="OSH-01")
        make_fr("OZOK Бишкек", "Бишкек", ["Бишкек"], code="BSK-01")
        self.admin = api(make_user(role="super_admin"))
        self.buh = api(make_user(role="finance"))
        self.owner = api(make_user(self.osh, "franchisee"))

    def test_full_scenario(self):
        # договор: создаёт admin, активирует бухгалтер
        r = self.admin.post("/api/v1/contracts/", {"franchisee": self.osh.id, "product": self.market.id,
                                                   "payment_scheme": "cash"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        rule = self.admin.post("/api/v1/commission-rules/", {"name": "Ош: 30%", "franchisee": self.osh.id,
                                                             "sale_kind": "subscription", "percent": "30"},
                               format="json")
        self.assertEqual(rule.status_code, 201, rule.content)
        cid = r.json()["id"]
        self.assertEqual(self.buh.post(f"/api/v1/contracts/{cid}/activate/").json()["status"], "active")
        self.admin.post("/api/v1/sales-plans/", {"franchisee": self.osh.id, "month": MONTH, "target_clients": 1,
                                                 "target_revenue": "10000"}, format="json")
        before = self.owner.get("/api/v1/dashboard/").json()
        self.assertEqual((before["balance"], before["plan"]["fact_clients"], before["plan"]["fact_revenue"],
                          before["commission_month"]), ("35000.00", 0, "0.00", "0.00"))
        # 1. публичная заявка -> сразу у «OZOK Ош»
        r = api().post("/api/v1/leads/public/", {"name": "Азамат", "phone": "0555 000 111", "city": "Ош",
                                                 "interest": "program", "product": "market"}, format="json")
        self.assertEqual(r.status_code, 201)
        lead_id = r.json()["id"]
        lead = self.owner.get(f"/api/v1/leads/{lead_id}/").json()
        self.assertEqual((lead["status"], lead["franchisee"], lead["franchisee_name"]),
                         ("new", self.osh.id, "OZOK Ош"))
        # 2. взята -> демо -> успешно -> клиент
        for action, status in (("take", "in_progress"), ("demo", "demo")):
            self.assertEqual(self.owner.post(f"/api/v1/leads/{lead_id}/{action}/").json()["status"], status)
        won = self.owner.post(f"/api/v1/leads/{lead_id}/win/", {"business_name": "Магазин «Азамат»",
                                                                "business_type": "shop"}, format="json").json()
        self.assertEqual(won["status"], "paid")
        client_id = won["client_id"]
        # 3. продукт присылает оплату; клиент неизвестен -> нераспознано -> центр привязывает
        r = api().post("/api/v1/integrations/events/", [{
            "event_id": "ev-1", "type": "payment.succeeded", "external_payment_id": "pay-1",
            "external_client_id": "nur-555", "amount": "10000", "months": 1,
            "paid_until": add_months(today(), 1).isoformat(), "paid_at": timezone.now().isoformat()}],
            format="json", HTTP_X_OZOK_KEY=self.market.api_key)
        self.assertEqual(r.json()["results"][0]["status"], "unmatched")
        hq_dash = self.admin.get("/api/v1/dashboard/").json()
        self.assertGreaterEqual(hq_dash["unmatched_events"], 1)
        event = IntegrationEvent.objects.get(external_id="ev-1")
        r = self.admin.post(f"/api/v1/integration-events/{event.id}/resolve/",
                            {"franchisee": self.osh.id, "client": client_id}, format="json")
        self.assertEqual(r.json()["status"], "processed")
        # 4. продажа с вознаграждением 30%
        sales = self.owner.get("/api/v1/client-sales/").json()["results"]
        sale = next(s for s in sales if s["client"] == client_id)
        self.assertEqual((sale["kind"], sale["source"], sale["commission_percent"], sale["commission_amount"],
                          sale["product_name"], sale["product_code"]),
                         ("subscription", "integration", "30.00", "3000.00", self.market.name, "market"))
        # 5. платёж франчайзи центру -> баланс уменьшился
        r = self.buh.post("/api/v1/franchisee-payments/", {"franchisee": self.osh.id, "amount": "20000",
                                                           "method": "bank", "contract": cid}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        after = self.owner.get("/api/v1/dashboard/").json()
        self.assertEqual((after["balance"], after["commission_due"], after["commission_month"]),
                         ("15000.00", "3000.00", "3000.00"))
        # 6. факт плана и рейтинг на дашбордах обновились
        self.assertEqual((after["plan"]["fact_clients"], after["plan"]["fact_revenue"], after["plan"]["plan_percent"]),
                         (1, "10000.00", 1.0))
        self.assertIsNotNone(after["rating"])
        self.assertEqual(after["place"], 1)
        self.assertEqual(after["subscriptions"]["active"], 1)
        hq_after = self.admin.get("/api/v1/dashboard/").json()
        self.assertEqual(hq_after["network_revenue"], "10000.00")
        self.assertEqual(hq_after["payments_month"], "20000.00")
        self.assertEqual(hq_after["receivables"], "15000.00")
        self.assertEqual(next(r for r in hq_after["rating"] if r["franchisee_id"] == self.osh.id)["place"], 1)
        # выплата вознаграждения: франчайзи читает
        # commission-payouts/ — только история (D1): новая выплата идёт через payouts/, старая строка — через ORM
        self.assertEqual(self.buh.post("/api/v1/commission-payouts/", {"franchisee": self.osh.id, "amount": "1000",
                                                                       "method": "cash"}, format="json").status_code, 405)
        CommissionPayout.objects.create(franchisee=self.osh, amount=1000, method="cash")
        self.assertEqual(self.owner.get("/api/v1/dashboard/").json()["commission_due"], "2000.00")
        self.assertEqual(self.owner.get("/api/v1/commission-payouts/").json()["count"], 1)


class DashboardShapeTests(TestCase):
    HQ_KEYS = {"scope", "month", "franchisees", "leads", "new_clients", "network_revenue", "commission_month",
               "receivables", "overdue_receivables", "payments_month", "open_tickets", "sla_breached",
               "overdue_tasks", "trained_share", "subscriptions", "unmatched_events", "revenue_by_month",
               "sales_by_product", "rating", "attention", "network_income_month", "withheld_month",
               "payments_collected", "commissions_accrued", "commissions_withheld", "commissions_paid", "commissions_on_hold"}
    FR_KEYS = {"scope", "month", "plan", "rating", "place", "network_size", "leads", "clients_total", "renewal_soon",
               "expired", "balance", "overdue_amount", "commission_due", "commission_month", "sales_by_product",
               "subscriptions", "open_tickets", "sla_breached", "tasks", "trained_share", "revenue_by_month",
               "income_month", "withheld_month",
               "payments_collected", "commissions_accrued", "commissions_withheld", "commissions_paid", "commissions_on_hold"}

    def setUp(self):
        self.market = make_product("market")
        self.opt = make_product("opt")
        self.fr = make_fr("OZOK Ош", "Ош")
        self.fr2 = make_fr("OZOK Бишкек", "Бишкек")
        make_fr("Новый", "Нарын", status="trainee")
        make_fr("Кандидат", "Талас", status="candidate")
        self.owner = make_user(self.fr, "franchisee")
        make_user(self.fr2, "franchisee")
        make_contract(self.fr, product=self.market, signed=days_ago(30))  # просрочка 35000
        self.client_ = make_client(self.fr)
        ClientSale.objects.create(franchisee=self.fr, client=self.client_, product=self.market, kind="subscription",
                                  amount=1000, commission_percent=20, commission_amount=200)

    def test_hq_shape(self):
        d = api(make_user(role="network_head")).get("/api/v1/dashboard/").json()
        self.assertEqual(set(d), self.HQ_KEYS)
        self.assertEqual(d["scope"], "hq")
        self.assertEqual(d["month"], MONTH)
        self.assertEqual(d["franchisees"], {"candidate": 1, "trainee": 1, "manager": 0, "franchisee": 2, "suspended": 0, "closed": 0})
        self.assertEqual(set(d["leads"]), {"new", "in_progress", "demo", "proposal", "contract", "paid", "lost",
                                           "assigned", "won", "overdue", "unassigned"})
        self.assertEqual(d["overdue_receivables"], "35000.00")
        self.assertEqual(d["receivables"], "35000.00")
        self.assertEqual((d["network_revenue"], d["commission_month"]), ("1000.00", "200.00"))
        self.assertEqual(set(d["subscriptions"]), {"active", "renewal_soon", "expired", "by_status"})
        self.assertEqual(len(d["revenue_by_month"]), 6)
        self.assertEqual(d["revenue_by_month"][-1], {"month": MONTH, "revenue": "1000.00"})
        self.assertEqual({r["franchisee_id"] for r in d["rating"]}, {self.fr.id, self.fr2.id})
        self.assertEqual(set(d["rating"][0]), {"franchisee_id", "name", "city", "rating", "place", "plan_percent"})
        self.assertEqual([p["product"] for p in d["sales_by_product"]], ["market", "opt"])
        att = d["attention"]
        self.assertTrue(att and att[0]["type"] == "overdue_payment" and "35 000" in att[0]["text"])
        self.assertEqual(set(att[0]), {"type", "franchisee_id", "franchisee_name", "text"})
        self.assertLessEqual(len(att), 10)

    def test_franchisee_shape(self):
        d = api(self.owner).get("/api/v1/dashboard/").json()
        self.assertEqual(set(d), self.FR_KEYS)
        self.assertEqual((d["scope"], d["plan"], d["network_size"], d["clients_total"]), ("franchisee", None, 2, 1))
        self.assertEqual((d["balance"], d["overdue_amount"], d["commission_month"], d["commission_due"]),
                         ("35000.00", "35000.00", "200.00", "200.00"))
        self.assertEqual(set(d["leads"]), {"assigned", "in_progress", "demo", "overdue"})
        self.assertEqual(set(d["tasks"]), {"new", "in_progress", "overdue"})
        self.assertEqual(set(d["subscriptions"]), {"active", "renewal_soon", "expired", "by_status"})
        self.assertEqual(len(d["revenue_by_month"]), 6)

    def test_month_param_and_errors(self):
        admin = api(make_user(role="super_admin"))
        past = add_months(month_first(today()), -2).strftime("%Y-%m")
        self.assertEqual(admin.get("/api/v1/dashboard/", {"month": past}).json()["month"], past)
        self.assertEqual(admin.get("/api/v1/dashboard/", {"month": "bad"}).status_code, 400)

    def test_attention_counts_all_types(self):
        hq = make_user(role="super_admin")
        Task.objects.create(franchisee=self.fr, title="Т", due_date=days_ago(2), created_by=hq)
        lead = Lead.objects.create(name="x", phone="1", franchisee=self.fr, status="new",
                                   assigned_at=timezone.now() - timedelta(hours=30))
        from quality.services import create_ticket
        create_ticket(franchisee=self.fr, client=self.client_, subject="Т", priority="critical",
                      now=timezone.now() - timedelta(hours=3))
        d = api(hq).get("/api/v1/dashboard/").json()
        self.assertEqual({a["type"] for a in d["attention"]}, {"overdue_payment", "sla", "overdue_task",
                                                               "overdue_lead"})
        self.assertEqual((d["overdue_tasks"], d["sla_breached"], d["open_tickets"], d["leads"]["overdue"]),
                         (1, 1, 1, 1))
        fr = api(self.owner).get("/api/v1/dashboard/").json()
        self.assertEqual((fr["tasks"]["overdue"], fr["sla_breached"], fr["leads"]["overdue"]), (1, 1, 1))
        self.assertIsNotNone(lead)


class SalesReportTests(TestCase):
    def setUp(self):
        self.market = make_product("market", sphere="Магазины")
        self.opt = make_product("opt", sphere="Опт")
        self.fr = make_fr("A", "Ош")
        self.fr2 = make_fr("B", "Бишкек")
        rows = ((self.fr, self.market, "subscription", 10000, 20, 0), (self.fr, self.market, "setup", 5000, 0, 1500),
                (self.fr, self.opt, "renewal", 4000, 30, 0), (self.fr2, self.opt, "subscription", 8000, 20, 0))
        for fr, product, kind, amount, pct, withheld in rows:
            commission = D(amount) * pct / 100
            income = commission if kind in ("subscription", "renewal") else D(amount)
            ClientSale.objects.create(franchisee=fr, client=make_client(fr), product=product, kind=kind,
                                      amount=amount, commission_percent=pct, commission_amount=commission,
                                      franchisee_income=income, withheld_amount=withheld, list_amount=amount)
    def test_hq_report_shape_and_totals(self):
        r = api(make_user(role="finance")).get("/api/v1/reports/sales/")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(set(d), {"period", "filters", "totals", "by_product", "by_franchisee", "by_month"})
        self.assertEqual(d["period"], {"date_from": month_first(today()).isoformat(),
                                       "date_to": month_last(today()).isoformat()})
        self.assertEqual(d["filters"], {"franchisee": None, "product": None})
        self.assertEqual({k: v for k, v in d["totals"].items() if k != "by_kind"},
                         {"count": 4, "amount": "27000.00", "subscription_amount": "22000.00", "commission": "4800.00",
                          "income": "9800.00", "withheld": "1500.00", "net_income": "8300.00"})
        self.assertEqual([(k["kind"], k["count"], k["amount"], k["income"]) for k in d["totals"]["by_kind"]],
                         [("setup", 1, "5000.00", "5000.00"), ("training", 0, "0.00", "0.00"), ("equipment", 0, "0.00", "0.00"),
                          ("subscription", 3, "22000.00", "4800.00"), ("other", 0, "0.00", "0.00")])
        mp = {p["product"]: p for p in d["by_product"]}
        self.assertEqual(set(mp["market"]), {"product", "sphere", "name", "color", "icon", "count", "amount",
                                             "subscription_amount", "commission", "income", "withheld", "net_income"})
        self.assertEqual((mp["market"]["income"], mp["market"]["withheld"], mp["market"]["net_income"]),
                         ("7000.00", "1500.00", "5500.00"))
        self.assertEqual((mp["market"]["count"], mp["market"]["amount"], mp["market"]["subscription_amount"],
                          mp["market"]["commission"], mp["opt"]["amount"]),
                         (2, "15000.00", "10000.00", "2000.00", "12000.00"))
        self.assertEqual([(f["name"], f["amount"]) for f in d["by_franchisee"]], [("A", "19000.00"), ("B", "8000.00")])
        self.assertEqual(len(d["by_month"]), 6)
        self.assertEqual(d["by_month"][-1], {"month": MONTH, "amount": "27000.00", "commission": "4800.00",
                                              "income": "9800.00", "withheld": "1500.00", "net_income": "8300.00"})
        by_fr = {f["name"]: f for f in d["by_franchisee"]}
        self.assertEqual((by_fr["A"]["income"], by_fr["A"]["withheld"], by_fr["A"]["net_income"]),
                         ("8200.00", "1500.00", "6700.00"))

    def test_filters_and_dynamic_products(self):
        hq = api(make_user(role="super_admin"))
        d = hq.get("/api/v1/reports/sales/", {"franchisee": self.fr.id, "product": "opt"}).json()
        self.assertEqual((d["filters"], d["totals"]["amount"], d["totals"]["count"]),
                         ({"franchisee": self.fr.id, "product": "opt"}, "4000.00", 1))
        self.assertEqual({p["product"] for p in d["by_product"]}, {"market", "opt"})  # карточки без фильтра сферы
        make_product("salon", sphere="Красота", sort_order=9)
        d = hq.get("/api/v1/reports/sales/").json()
        self.assertEqual([p["product"] for p in d["by_product"]][-1], "salon")
        self.assertEqual(d["by_product"][-1]["count"], 0)
        self.opt.is_active = False
        self.opt.save()  # отключённая сфера с продажами остаётся в отчёте
        self.assertIn("opt", [p["product"] for p in hq.get("/api/v1/reports/sales/").json()["by_product"]])
        d = hq.get("/api/v1/reports/sales/", {"date_from": "2020-01-01", "date_to": "2020-01-31"}).json()
        self.assertEqual(d["totals"]["count"], 0)
        self.assertEqual(hq.get("/api/v1/reports/sales/", {"date_from": "bad"}).status_code, 400)

    def test_franchisee_sees_only_own(self):
        d = api(make_user(self.fr2, "franchisee")).get("/api/v1/reports/sales/", {"franchisee": self.fr.id}).json()
        self.assertEqual(d["totals"]["amount"], "8000.00")
        self.assertEqual(d["by_franchisee"], [])
        self.assertIsNone(d["filters"]["franchisee"])

    def test_client_sales_list_filters(self):
        hq = api(make_user(role="super_admin"))
        self.assertEqual(hq.get("/api/v1/client-sales/", {"product": "opt"}).json()["count"], 2)
        self.assertEqual(hq.get("/api/v1/client-sales/", {"product": self.opt.id}).json()["count"], 2)
        self.assertEqual(hq.get("/api/v1/client-sales/", {"kind": "setup"}).json()["count"], 1)
        self.assertEqual(hq.get("/api/v1/client-sales/", {"franchisee": self.fr2.id}).json()["count"], 1)
        self.assertEqual(hq.get("/api/v1/client-sales/", {"date_from": str(days_ago(-1))}).json()["count"], 0)
        row = hq.get("/api/v1/client-sales/").json()["results"][0]
        for key in ("source", "product_name", "product_code", "sphere", "commission_percent", "commission_amount",
                    "client_name", "franchisee_name"):
            self.assertIn(key, row)


class FranchiseeApiTests(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.pkg = make_package([self.market])
        self.fr = make_fr("OZOK Ош", "Ош")
        self.other = make_fr("OZOK Бишкек", "Бишкек")
        self.owner = api(make_user(self.fr, "franchisee"))
        self.admin = api(make_user(role="super_admin"))
        make_contract(self.fr, product=self.market)

    def test_franchisee_user_gets_own_row_with_extras(self):
        r = self.owner.get("/api/v1/franchisees/").json()
        self.assertEqual(r["count"], 1)
        row = r["results"][0]
        self.assertEqual(row["products"], ["market"])
        for key in ("balance", "overdue_amount", "commission_due", "commission_month", "sales_by_product", "rating",
                    "employees_count", "clients_count", "curator_name", "partner_code"):
            self.assertIn(key, row)
        self.assertEqual((row["balance"], row["employees_count"]), ("35000.00", 1))

    def test_summary_shape(self):
        d = self.admin.get(f"/api/v1/franchisees/{self.fr.id}/summary/").json()
        self.assertTrue({"franchisee", "balance", "overdue_amount", "commission_due", "commission_month",
                         "sales_by_product", "rating", "place", "plan", "contracts", "employees", "recent_leads",
                         "recent_sales", "open_tasks", "last_audit"} <= set(d))
        self.assertEqual((d["balance"], len(d["contracts"]), len(d["employees"])), ("35000.00", 1, 1))
        self.assertEqual(self.owner.get(f"/api/v1/franchisees/{self.fr.id}/summary/").status_code, 200)

    def test_crud_and_filters_by_center(self):
        curator = make_user(role="network_head")
        c = api(curator)
        r = c.post("/api/v1/franchisees/", {"name": "OZOK Талас", "city": "Талас", "partner_code": "TAL-01",
                                            "status": "candidate", "curator": curator.id}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.json()["cities"], r.json()["curator_name"]), (["Талас"], curator.full_name))
        self.assertEqual(c.post("/api/v1/franchisees/", {"name": "Дубль", "city": "X", "partner_code": "TAL-01"},
                                format="json").status_code, 400)
        self.assertEqual(c.get("/api/v1/franchisees/", {"curator": "me"}).json()["count"], 1)
        self.assertEqual(c.get("/api/v1/franchisees/", {"status": "franchisee"}).json()["count"], 2)
        self.assertEqual(c.get("/api/v1/franchisees/", {"search": "Ош"}).json()["count"], 1)
        self.assertEqual(self.owner.post("/api/v1/franchisees/", {"name": "x", "city": "y"}, format="json").status_code, 403)
        # с данными франчайзи удалить нельзя
        self.assertEqual(c.delete(f"/api/v1/franchisees/{self.fr.id}/").status_code, 403)  # удаляет только Super Admin
        self.assertEqual(self.admin.delete(f"/api/v1/franchisees/{self.fr.id}/").status_code, 400)

    def test_pagination_shape(self):
        for i in range(3):
            make_fr(f"Ф{i}", "Ош")
        r = self.admin.get("/api/v1/franchisees/", {"page_size": 2}).json()
        self.assertEqual(set(r), {"count", "next", "previous", "results"})
        self.assertEqual((r["count"], len(r["results"])), (5, 2))
        self.assertIsNotNone(r["next"])
        self.assertEqual(len(self.admin.get("/api/v1/franchisees/", {"page_size": 1000}).json()["results"]), 5)


class PermissionMatrixTests(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.fr = make_fr()
        self.users = {role: make_user(self.fr if role.startswith("franchisee") else None, role)
                      for role in ("super_admin", "network_head", "finance", "franchisee", "franchisee_staff")}
        self.client_ = make_client(self.fr)

    def code(self, role, method, url, body=None):
        return getattr(api(self.users[role]), method)(f"/api/v1/{url}", body or {}, format="json").status_code

    def test_read_access(self):
        table = {
            "contracts/": {"super_admin": 200, "network_head": 200, "finance": 200, "franchisee": 200, "franchisee_staff": 403},
            "charges/": {"super_admin": 200, "network_head": 200, "finance": 200, "franchisee": 200, "franchisee_staff": 403},
            "franchisee-payments/": {"super_admin": 200, "finance": 200, "franchisee": 200, "franchisee_staff": 403},
            "commission-payouts/": {"super_admin": 200, "finance": 200, "franchisee": 200, "franchisee_staff": 403},
            "users/": {"super_admin": 200, "network_head": 200, "finance": 200, "franchisee": 200, "franchisee_staff": 200},
            "integration-events/": {"super_admin": 200, "network_head": 200, "finance": 200, "franchisee": 403,
                                    "franchisee_staff": 403},
            "reports/rating/": {"super_admin": 200, "finance": 200, "franchisee": 403},
            "reports/sales/": {"super_admin": 200, "finance": 200, "franchisee": 200, "franchisee_staff": 200},
            "dashboard/": {role: 200 for role in self.users},
            "products/": {role: 200 for role in self.users},
            "packages/": {role: 200 for role in self.users},
            "courses/": {role: 200 for role in self.users},
            "client-accounts/": {role: 200 for role in self.users},
        }
        for url, roles in table.items():
            for role, expected in roles.items():
                self.assertEqual(self.code(role, "get", url), expected, (role, url))

    def test_write_access(self):
        fr_id = self.fr.id
        cases = [
            # (url, body, {роль: ожидаемый "разрешено?"})
            ("products/", {"code": "n1", "name": "N"}, {"super_admin": True, "network_head": False, "finance": False,
                                                        "franchisee": False, "franchisee_staff": False}),
            ("franchisees/", {"name": "N", "city": "C"}, {"super_admin": True, "network_head": True, "finance": False,
                                                          "franchisee": False, "franchisee_staff": False}),
            ("contracts/", {"franchisee": fr_id, "product": self.market.id},
             {"super_admin": True, "network_head": False, "finance": True, "franchisee": False, "franchisee_staff": False}),
            ("charges/", {"franchisee": fr_id, "amount": "5", "kind": "other"},
             {"super_admin": True, "network_head": False, "finance": True, "franchisee": False, "franchisee_staff": False}),
            ("franchisee-payments/", {"franchisee": fr_id, "amount": "5", "method": "cash"},
             {"super_admin": True, "network_head": False, "finance": True, "franchisee": False, "franchisee_staff": False}),
            ("clients/", {"franchisee": fr_id, "business_name": "N"},
             {"super_admin": True, "network_head": False, "finance": False, "franchisee": True, "franchisee_staff": True}),
            ("leads/", {"name": "N", "phone": "1", "franchisee": fr_id},
             {"super_admin": True, "network_head": True, "finance": False, "franchisee": True, "franchisee_staff": True}),
            ("tasks/", {"franchisee": fr_id, "title": "T"},
             {"super_admin": True, "network_head": True, "finance": False, "franchisee": False, "franchisee_staff": False}),
            ("audits/", {"franchisee": fr_id, "items": [{"criterion": "a", "ok": True}]},
             {"super_admin": True, "network_head": True, "finance": False, "franchisee": False, "franchisee_staff": False}),
            ("sales-plans/", {"franchisee": fr_id, "month": "2026-01"},
             {"super_admin": True, "network_head": True, "finance": False, "franchisee": False, "franchisee_staff": False}),
            ("courses/", {"title": "K"}, {"super_admin": True, "network_head": True, "finance": False,
                                          "franchisee": False, "franchisee_staff": False}),
            ("tickets/", {"client": self.client_.id, "subject": "S"},
             {"super_admin": True, "network_head": True, "finance": False, "franchisee": True, "franchisee_staff": True}),
            ("client-sales/", {"client": self.client_.id, "product": self.market.id, "kind": "setup", "amount": "5"},
             {"super_admin": True, "network_head": False, "finance": False, "franchisee": True, "franchisee_staff": True}),
        ]
        for url, body, roles in cases:
            for role, allowed in roles.items():
                status = self.code(role, "post", url, body)
                if allowed:
                    self.assertEqual(status, 201, (role, url, body))
                else:
                    self.assertEqual(status, 403, (role, url))
                if allowed:  # убираем созданное, чтобы роли не упирались друг в друга (дубли плана, договор)
                    from network.models import Contract
                    from planning.models import SalesPlan
                    Contract.objects.filter(status="draft").delete()
                    SalesPlan.objects.all().delete()

    def test_manager_can_only_read_users_owner_writes(self):
        body = {"email": "new@t.kg", "password": "pass12345", "role": "franchisee_staff"}
        self.assertEqual(self.code("franchisee_staff", "post", "users/", body), 403)
        self.assertEqual(self.code("network_head", "post", "users/", body), 403)
        self.assertEqual(self.code("franchisee", "post", "users/", body), 201)

    def test_user_admin_rules(self):
        admin = api(self.users["super_admin"])
        r = admin.post("/api/v1/users/", {"email": "Cur@T.kg", "password": "pass12345", "role": "network_head"},
                       format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["email"], "cur@t.kg")
        self.assertNotIn("password", r.json())
        self.assertEqual(admin.post("/api/v1/users/", {"email": "cur@t.kg", "password": "pass12345",
                                                       "role": "network_head"}, format="json").status_code, 400)
        self.assertEqual(admin.post("/api/v1/users/", {"email": "x@t.kg", "password": "pass12345",
                                                       "role": "franchisee"}, format="json").status_code, 400)  # нужен франчайзи
        self.assertEqual(admin.post("/api/v1/users/", {"email": "y@t.kg", "password": "short", "role": "network_head"},
                                    format="json").status_code, 400)
        me = self.users["super_admin"]
        self.assertEqual(admin.delete(f"/api/v1/users/{me.id}/").status_code, 400)
        self.assertEqual(admin.patch(f"/api/v1/users/{me.id}/", {"is_active": False}, format="json").status_code, 400)

    def test_commission_payout_offset_cannot_be_entered_manually(self):
        r = api(self.users["finance"]).post("/api/v1/commission-payouts/", {
            "franchisee": self.fr.id, "amount": "5", "method": "offset"}, format="json")
        self.assertEqual(r.status_code, 405)  # D1: ручных выплат больше нет
        r = api(self.users["finance"]).post("/api/v1/franchisee-payments/", {
            "franchisee": self.fr.id, "amount": "5", "method": "offset"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(CommissionPayout.objects.count() + FranchiseePayment.objects.count(), 0)

    def test_implementation_stage_action(self):
        owner = api(self.users["franchisee"])
        r = owner.post("/api/v1/implementations/", {"client": self.client_.id, "product": self.market.id,
                                                    "planned_launch": str(days_ago(3))}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(r.json()["overdue"])
        iid = r.json()["id"]
        r = owner.post(f"/api/v1/implementations/{iid}/stage/", {"stage": "launched"}, format="json")
        self.assertEqual((r.json()["stage"], r.json()["launched_at"], r.json()["overdue"]),
                         ("launched", today().isoformat(), False))
        self.assertEqual(owner.post(f"/api/v1/implementations/{iid}/stage/", {"stage": "launched"},
                                    format="json").status_code, 400)
        self.assertEqual(owner.post(f"/api/v1/implementations/{iid}/stage/", {"stage": "bogus"},
                                    format="json").status_code, 400)
        self.assertEqual(api(self.users["network_head"]).post(f"/api/v1/implementations/{iid}/stage/",
                                                            {"stage": "setup"}, format="json").status_code, 403)




