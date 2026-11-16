"""Интеграция с продуктами (раздел 5Б): ключ, идемпотентность, нераспознанные, вознаграждение, возврат."""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from billing.models import CommissionPayout, FranchiseePayment
from billing.services import franchisee_finance
from clients.models import Client, ClientProductAccount, ClientSale
from core.models import add_months, today
from core.testing import api, make_contract, make_fr, make_product, make_user
from integrations.models import IntegrationEvent
from integrations.services import process_event

D = Decimal
EVENTS = "/api/v1/integrations/events/"


class IntegrationBase(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.opt = make_product("opt")
        self.fr = make_fr("OZOK Ош", "Ош", code="OSH-01")
        self.other = make_fr("OZOK Бишкек", "Бишкек", code="BSK-01")
        self.n = 0

    def post(self, data, product=None, key=None):
        product = product or self.market
        c = api()
        return c.post(EVENTS, data, format="json", HTTP_X_OZOK_KEY=key if key is not None else product.api_key)

    def eid(self):
        self.n += 1
        return f"e{self.n}"

    def upsert(self, ext="c-1", code="OSH-01", **kw):
        return {"event_id": self.eid(), "type": "client.upsert", "external_client_id": ext, "partner_code": code,
                "business_name": "Магазин «Береке»", "business_type": "shop", "phone": "0555 123 456",
                "city": "Ош", **kw}

    def payment(self, pid="p-1", ext="c-1", amount="10000", months=1, paid_until=None, **kw):
        return {"event_id": self.eid(), "type": "payment.succeeded", "external_payment_id": pid,
                "external_client_id": ext, "amount": amount, "months": months,
                "paid_until": (paid_until or add_months(today(), months)).isoformat(),
                "paid_at": timezone.now().isoformat(), **kw}

    def status(self, resp, i=0):
        return resp.json()["results"][i]["status"]


class AuthTests(IntegrationBase):
    def test_ping_and_key_checks(self):
        c = api()
        r = c.get("/api/v1/integrations/ping/", HTTP_X_OZOK_KEY=self.market.api_key)
        self.assertEqual((r.status_code, r.json()), (200, {"product": "market", "ok": True}))
        self.assertEqual(c.get("/api/v1/integrations/ping/").status_code, 401)
        self.assertEqual(c.get("/api/v1/integrations/ping/", HTTP_X_OZOK_KEY="wrong").status_code, 401)
        self.assertEqual(self.post(self.upsert(), key="wrong").status_code, 401)
        self.assertEqual(api().post(EVENTS, self.upsert(), format="json").status_code, 401)

    def test_disabled_integration_is_401(self):
        self.market.integration_enabled = False
        self.market.save()
        self.assertEqual(self.post(self.upsert()).status_code, 401)

    def test_key_of_other_product_creates_account_for_that_product(self):
        self.post(self.upsert(ext="o-1"), product=self.opt)
        self.assertTrue(ClientProductAccount.objects.filter(product=self.opt, external_id="o-1").exists())
        self.assertFalse(ClientProductAccount.objects.filter(product=self.market).exists())

    def test_batch_limit_and_response_shape(self):
        events = [self.upsert(ext=f"b-{i}") for i in range(100)]
        r = self.post(events)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["results"]), 100)
        self.assertEqual(set(r.json()["results"][0]), {"event_id", "status", "error"})
        self.assertEqual(self.post([self.upsert(ext=f"x-{i}") for i in range(101)]).status_code, 400)
        self.assertEqual(self.post([]).status_code, 400)

    def test_last_event_at_and_bad_events(self):
        r = self.post([{"event_id": "z1", "type": "nope"}, {"type": "client.upsert"}, "строка"])
        self.assertEqual([x["status"] for x in r.json()["results"]], ["error", "error", "error"])
        self.market.refresh_from_db()
        self.assertIsNotNone(self.market.last_event_at)

    def test_rotate_key_returns_new_key_and_invalidates_old(self):
        admin = api(make_user(role="super_admin"))
        old = self.market.api_key
        r = admin.post(f"/api/v1/products/{self.market.id}/rotate-key/")
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(r.json()["api_key"], old)
        self.assertEqual(api().get("/api/v1/integrations/ping/", HTTP_X_OZOK_KEY=old).status_code, 401)
        self.assertEqual(api().get("/api/v1/integrations/ping/", HTTP_X_OZOK_KEY=r.json()["api_key"]).status_code, 200)
        self.assertEqual(api(make_user(self.fr, "franchisee")).post(
            f"/api/v1/products/{self.market.id}/rotate-key/").status_code, 403)

    def test_test_event_endpoint_super_admin_only(self):
        admin = api(make_user(role="super_admin"))
        r = admin.post("/api/v1/integrations/test-event/", {"product": "market"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["results"][-1]["status"], "processed")
        self.assertEqual(ClientSale.objects.filter(source="integration").count(), 1)
        self.assertEqual(api(make_user(self.fr, "franchisee")).post(
            "/api/v1/integrations/test-event/", {"product": "market"}, format="json").status_code, 403)
        self.assertEqual(admin.post("/api/v1/integrations/test-event/", {"product": "zzz"}, format="json").status_code, 400)

    def test_events_24h_counters_for_admin(self):
        self.post([self.upsert(), self.upsert(ext="c-2", code="NOPE")])
        admin = api(make_user(role="super_admin"))
        row = next(p for p in admin.get("/api/v1/products/").json()["results"] if p["code"] == "market")
        self.assertEqual(row["events_24h"], {"processed": 1, "duplicate": 0, "unmatched": 1, "error": 0})


class ClientUpsertTests(IntegrationBase):
    def test_creates_client_and_account_for_partner(self):
        r = self.post(self.upsert(connected_at="2026-10-01"))
        self.assertEqual(self.status(r), "processed")
        acc = ClientProductAccount.objects.get(product=self.market, external_id="c-1")
        self.assertEqual((acc.client.franchisee_id, acc.client.business_name, acc.connected_at.isoformat()),
                         (self.fr.id, "Магазин «Береке»", "2026-10-01"))
        # повторный upsert с другим event_id обновляет, а не дублирует
        self.post(self.upsert(business_name="Новое имя", status="blocked"))
        acc.refresh_from_db()
        self.assertEqual((Client.objects.count(), acc.client.business_name, acc.status), (1, "Новое имя", "restricted"))  # blocked из продукта -> restricted

    def test_clients_api_returns_accounts(self):
        self.post(self.upsert())
        owner = api(make_user(self.fr, "franchisee"))
        row = owner.get("/api/v1/clients/").json()["results"][0]
        self.assertEqual(row["accounts"][0]["external_id"], "c-1")
        self.assertEqual(row["accounts"][0]["product_name"], self.market.name)
        self.assertIn("paid_until", row["accounts"][0])
        self.assertEqual(row["products"], ["market"])

    def test_unknown_partner_code_is_unmatched(self):
        r = self.post(self.upsert(code="XXX"))
        self.assertEqual(self.status(r), "unmatched")
        self.assertIn("XXX", r.json()["results"][0]["error"])
        self.assertEqual(Client.objects.count(), 0)


class PaymentTests(IntegrationBase):
    def setUp(self):
        super().setUp()
        self.post(self.upsert())

    def test_first_payment_is_subscription_then_renewal_with_commission(self):
        make_contract(self.fr, product=self.market, commission=30)
        until1 = add_months(today(), 1)
        r = self.post(self.payment("p-1", amount="10000", months=1, paid_until=until1))
        self.assertEqual(self.status(r), "processed")
        s1 = ClientSale.objects.get(external_payment_id="p-1")
        self.assertEqual((s1.kind, s1.source, s1.months, s1.amount, s1.commission_percent, s1.commission_amount),
                         ("subscription", "integration", 1, D("10000"), D("30"), D("3000.00")))
        until2 = add_months(until1, 3)
        self.post(self.payment("p-2", amount="30000", months=3, paid_until=until2))
        s2 = ClientSale.objects.get(external_payment_id="p-2")
        self.assertEqual((s2.kind, s2.commission_amount), ("renewal", D("9000.00")))
        acc = ClientProductAccount.objects.get()
        self.assertEqual((acc.paid_until, acc.monthly_fee), (until2, D("10000.00")))
        self.assertEqual(franchisee_finance(self.fr.id)["commission_due"], D("12000.00"))
        self.assertEqual(IntegrationEvent.objects.filter(status="processed").count(), 3)

    def test_payment_without_contract_saved_without_commission(self):
        self.post(self.payment("p-1"))
        s = ClientSale.objects.get()
        self.assertEqual(s.commission_amount, D("0"))
        self.assertIn("договор", IntegrationEvent.objects.filter(event_type="payment.succeeded").first().error)

    def test_idempotency_by_event_id_and_payment_id(self):
        make_contract(self.fr, product=self.market)
        ev = self.payment("p-1")
        self.assertEqual(self.status(self.post(ev)), "processed")
        self.assertEqual(self.status(self.post(ev)), "duplicate")                # тот же event_id
        self.assertEqual(self.status(self.post(self.payment("p-1"))), "duplicate")  # другой event_id, тот же платёж
        self.assertEqual(ClientSale.objects.count(), 1)
        self.assertEqual(franchisee_finance(self.fr.id)["commission_due"], D("2000.00"))
        self.assertEqual(IntegrationEvent.objects.filter(status="duplicate").count(), 2)
        # одинаковый external_payment_id в разных продуктах — это разные платежи
        self.post(self.upsert(ext="c-1"), product=self.opt)
        self.assertEqual(self.status(self.post(self.payment("p-1"), product=self.opt), 0), "processed")

    def test_batch_with_mixed_results(self):
        r = self.post([self.payment("p-1"), self.payment("p-2", ext="unknown"), self.payment("p-3", amount="abc"),
                       self.payment("p-4", months=0)])
        self.assertEqual([x["status"] for x in r.json()["results"]], ["processed", "unmatched", "error", "error"])
        self.assertEqual(ClientSale.objects.count(), 1)

    def test_unmatched_payment_then_resolve(self):
        make_contract(self.fr, product=self.market, commission=20)
        r = self.post(self.payment("p-9", ext="ghost-1", amount="5000"))
        self.assertEqual(self.status(r), "unmatched")
        event = IntegrationEvent.objects.get(status="unmatched")
        admin = api(make_user(role="super_admin"))
        # без клиента платёж не привязать
        self.assertEqual(admin.post(f"/api/v1/integration-events/{event.id}/resolve/",
                                    {"franchisee": self.fr.id}, format="json").status_code, 400)
        # клиент другого франчайзи не подходит
        foreign = Client.objects.create(franchisee=self.other, business_name="Чужой")
        self.assertEqual(admin.post(f"/api/v1/integration-events/{event.id}/resolve/",
                                    {"franchisee": self.fr.id, "client": foreign.id}, format="json").status_code, 400)
        mine = Client.objects.get(franchisee=self.fr)
        r = admin.post(f"/api/v1/integration-events/{event.id}/resolve/",
                       {"franchisee": self.fr.id, "client": mine.id}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["status"], r.json()["client"], r.json()["franchisee"]),
                         ("processed", mine.id, self.fr.id))
        sale = ClientSale.objects.get(external_payment_id="p-9")
        self.assertEqual((sale.client_id, sale.commission_amount), (mine.id, D("1000.00")))
        self.assertTrue(ClientProductAccount.objects.filter(external_id="ghost-1", client=mine).exists())
        # повторная привязка невозможна
        self.assertEqual(admin.post(f"/api/v1/integration-events/{event.id}/resolve/",
                                    {"franchisee": self.fr.id, "client": mine.id}, format="json").status_code, 400)

    def test_unmatched_upsert_resolved_to_franchisee_creates_client(self):
        self.post(self.upsert(ext="n-1", code="NOPE"))
        event = IntegrationEvent.objects.get(status="unmatched")
        r = api(make_user(role="network_head")).post(f"/api/v1/integration-events/{event.id}/resolve/",
                                                   {"franchisee": self.other.id}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(ClientProductAccount.objects.get(external_id="n-1").client.franchisee_id, self.other.id)

    def test_event_log_filters_hq_only(self):
        self.post(self.upsert(ext="n-1", code="NOPE"))
        admin = api(make_user(role="super_admin"))
        r = admin.get("/api/v1/integration-events/", {"status": "unmatched", "product": "market"})
        self.assertEqual(r.json()["count"], 1)
        self.assertEqual(admin.get("/api/v1/integration-events/", {"product": "opt"}).json()["count"], 0)
        self.assertEqual(admin.get("/api/v1/integration-events/", {"event_type": "client.upsert"}).json()["count"], 2)
        today_s = today().isoformat()
        self.assertEqual(admin.get("/api/v1/integration-events/", {"date_from": today_s}).json()["count"], 2)
        self.assertEqual(api(make_user(role="finance")).get("/api/v1/integration-events/").status_code, 200)

    def test_client_accounts_filters(self):
        make_contract(self.fr, product=self.market)
        self.post(self.payment("p-1", months=1, paid_until=today() + timedelta(days=5)))
        self.post(self.upsert(ext="c-2"))
        self.post(self.payment("p-2", ext="c-2", months=1, paid_until=today() - timedelta(days=3)))
        owner = api(make_user(self.fr, "franchisee"))
        soon = owner.get("/api/v1/client-accounts/", {"renewal_soon": "true"}).json()
        expired = owner.get("/api/v1/client-accounts/", {"expired": "true"}).json()
        self.assertEqual([a["external_id"] for a in soon["results"]], ["c-1"])
        self.assertEqual([a["external_id"] for a in expired["results"]], ["c-2"])
        self.assertTrue(expired["results"][0]["expired"])
        clients_soon = owner.get("/api/v1/clients/", {"renewal_soon": "true"}).json()
        self.assertEqual(clients_soon["count"], 1)
        self.assertEqual(owner.get("/api/v1/clients/", {"expired": "true"}).json()["count"], 1)


class PerSaleIntegrationTests(IntegrationBase):
    def test_offset_from_integrated_payments_capped(self):
        self.post(self.upsert())
        c = make_contract(self.fr, product=self.market, scheme="per_sale", fee=D("1000"), commission=20)
        self.post(self.payment("p-1", amount="10000"))   # доход 2000 -> удержано 600 (30%)
        self.post(self.payment("p-2", amount="10000"))   # доход 2000 -> удержано 400 (остаток франшизы)
        self.post(self.payment("p-3", amount="10000"))   # взнос погашен
        payments = FranchiseePayment.objects.filter(method="offset", contract=c)
        self.assertEqual(sum(p.amount for p in payments), D("1000.00"))
        from billing.models import CommissionAccrual
        self.assertEqual(sum(a.withheld_amount for a in CommissionAccrual.objects.all()), D("1000.00"))  # C6
        f = franchisee_finance(self.fr.id)
        self.assertEqual((f["balance"], f["commission_due"]), (D("0.00"), D("5000.00")))


class RefundTests(IntegrationBase):
    def setUp(self):
        super().setUp()
        self.post(self.upsert())
        self.contract = make_contract(self.fr, product=self.market, commission=20)

    def refund(self, pid="p-1", amount="4000"):
        return {"event_id": self.eid(), "type": "payment.refunded", "external_payment_id": pid, "amount": amount}

    def test_refund_creates_negative_adjustment(self):
        self.post(self.payment("p-1", amount="10000"))
        r = self.post(self.refund(amount="4000"))
        self.assertEqual(self.status(r), "processed", r.content)
        neg = ClientSale.objects.get(amount__lt=0)
        self.assertEqual((neg.kind, neg.amount, neg.commission_amount, neg.source),
                         ("renewal", D("-4000.00"), D("-800.00"), "integration"))
        self.assertEqual(franchisee_finance(self.fr.id)["commission_due"], D("1200.00"))
        owner = api(make_user(self.fr, "franchisee"))
        rows = owner.get("/api/v1/client-sales/").json()["results"]
        self.assertIn("-4000.00", [x["amount"] for x in rows])
        # повторная отправка того же события — дубликат
        again = {"event_id": "same-refund", "type": "payment.refunded", "external_payment_id": "p-1", "amount": "1000"}
        self.assertEqual(self.status(self.post(again)), "processed")
        self.assertEqual(self.status(self.post(again)), "duplicate")

    def test_refund_more_than_payment_is_error(self):
        self.post(self.payment("p-1", amount="1000"))
        self.assertEqual(self.status(self.post(self.refund(amount="2000"))), "error")

    def test_refund_of_unknown_payment_is_unmatched(self):
        self.assertEqual(self.status(self.post(self.refund(pid="nope"))), "unmatched")

    def test_refund_reverses_per_sale_offset(self):
        c = make_contract(make_fr("Ф2", "Нарын", code="NAR-01"), product=self.opt, scheme="per_sale",
                          fee=D("5000"), commission=20)
        self.post(self.upsert(ext="n-1", code="NAR-01"), product=self.opt)
        self.post(self.payment("q-1", ext="n-1", amount="10000"), product=self.opt)  # удержано 600
        fid = c.franchisee_id
        self.assertEqual(franchisee_finance(fid)["balance"], D("4400.00"))
        self.post(self.refund(pid="q-1", amount="10000"), product=self.opt)           # полный возврат
        f = franchisee_finance(fid)
        self.assertEqual((f["balance"], f["commission_due"]), (D("5000.00"), D("0.00")))

    def test_process_event_helper_is_idempotent_for_same_payload(self):
        ev = self.payment("p-1")
        self.assertEqual(process_event(self.market, ev).status, "processed")
        self.assertEqual(process_event(self.market, ev).status, "duplicate")




