"""Изоляция данных между франчайзи (раздел 11, п. 2): на каждый ресурс."""
from django.test import TestCase

from billing.models import CommissionPayout, FranchiseePayment
from clients.models import ClientProductAccount, Implementation
from clients.services import create_client_sale
from core.models import month_first, today
from core.testing import api, make_client, make_contract, make_fr, make_product, make_user
from leads.services import create_lead
from planning.models import SalesPlan
from quality.models import Audit
from quality.services import create_ticket
from tasks.models import Task
from training.models import Course
from training.services import enroll

RESOURCES = {  # URL -> ключ в build()
    "leads": "lead", "clients": "client", "client-accounts": "account", "implementations": "impl",
    "client-sales": "sale", "charges": "charge", "franchisee-payments": "payment",
    "commission-payouts": "payout", "contracts": "contract", "enrollments": "enrollment", "tickets": "ticket",
    "audits": "audit", "sales-plans": "plan", "tasks": "task", "users": "employee",
}


class IsolationTests(TestCase):
    def setUp(self):
        self.product = make_product("market")
        self.course = Course.objects.create(title="Стандарты")
        self.hq = make_user(role="super_admin")
        self.a = make_fr("OZOK Ош", "Ош")
        self.b = make_fr("OZOK Бишкек", "Бишкек")
        self.owner_a = make_user(self.a, "franchisee")
        self.manager_a = make_user(self.a, "franchisee_staff")
        self.owner_b = make_user(self.b, "franchisee")
        self.A = self.build(self.a, self.owner_a)
        self.B = self.build(self.b, self.owner_b)
        self.api_a = api(self.owner_a)

    def build(self, fr, owner):
        client = make_client(fr)
        contract = make_contract(fr, product=self.product, scheme="cash")
        sale, _ = create_client_sale(client=client, product=self.product, kind="setup", amount=1000)
        return {
            "client": client, "contract": contract, "sale": sale, "employee": owner,
            "charge": fr.charges.first(),
            "lead": create_lead(franchisee=fr, name="Лид", phone="0555", city=fr.city, interest="program"),
            "account": ClientProductAccount.objects.create(client=client, product=self.product,
                                                           external_id=f"x-{fr.id}"),
            "impl": Implementation.objects.create(franchisee=fr, client=client, product=self.product),
            "payment": FranchiseePayment.objects.create(franchisee=fr, amount=10, method="bank"),
            "payout": CommissionPayout.objects.create(franchisee=fr, amount=10, method="bank"),
            "enrollment": enroll(self.course, owner),
            "ticket": create_ticket(franchisee=fr, client=client, subject="Тема"),
            "audit": Audit.objects.create(franchisee=fr, items=[], score=0),
            "plan": SalesPlan.objects.create(franchisee=fr, month=month_first(today())),
            "task": Task.objects.create(franchisee=fr, title="Задача", created_by=self.hq),
        }

    def ids(self, url, client=None, **params):
        r = (client or self.api_a).get(f"/api/v1/{url}/", params)
        self.assertEqual(r.status_code, 200, url)
        return {row["id"] for row in r.json()["results"]}

    def test_lists_contain_only_own_objects(self):
        for url, key in RESOURCES.items():
            ids = self.ids(url)
            self.assertIn(self.A[key].id, ids, url)
            self.assertNotIn(self.B[key].id, ids, url)

    def test_franchisee_param_ignored_for_franchisee_user(self):
        for url, key in RESOURCES.items():
            ids = self.ids(url, franchisee=self.b.id)
            self.assertNotIn(self.B[key].id, ids, url)
            self.assertIn(self.A[key].id, ids, url)

    def test_hq_sees_all_and_can_filter_by_franchisee(self):
        hq = api(self.hq)
        for url, key in RESOURCES.items():
            if url in ("users",):
                continue
            all_ids = self.ids(url, hq)
            self.assertTrue({self.A[key].id, self.B[key].id} <= all_ids, url)
            only_b = self.ids(url, hq, franchisee=self.b.id)
            self.assertIn(self.B[key].id, only_b, url)
            self.assertNotIn(self.A[key].id, only_b, url)

    def test_foreign_object_detail_is_404(self):
        for url, key in RESOURCES.items():
            r = self.api_a.get(f"/api/v1/{url}/{self.B[key].id}/")
            self.assertEqual(r.status_code, 404, url)
            self.assertIn(self.api_a.patch(f"/api/v1/{url}/{self.B[key].id}/", {}, format="json").status_code,
                          (403, 404), url)
            self.assertIn(self.api_a.delete(f"/api/v1/{url}/{self.B[key].id}/").status_code, (403, 404, 405), url)

    def test_franchisee_resource_shows_only_own_row(self):
        ids = self.ids("franchisees")
        self.assertEqual(ids, {self.a.id})
        self.assertEqual(self.api_a.get(f"/api/v1/franchisees/{self.b.id}/").status_code, 404)
        self.assertEqual(self.api_a.get(f"/api/v1/franchisees/{self.b.id}/summary/").status_code, 404)

    def test_cannot_create_with_foreign_fks(self):
        b_client, b_user = self.B["client"].id, self.owner_b.id
        cases = [
            ("client-sales", {"client": b_client, "product": self.product.id, "kind": "setup", "amount": "100"}),
            ("implementations", {"client": b_client, "product": self.product.id}),
            ("tickets", {"client": b_client, "subject": "x"}),
            ("enrollments", {"course": self.course.id, "user": b_user}),
            ("leads", {"name": "x", "phone": "1", "franchisee": self.b.id}),
            ("implementations", {"client": self.A["client"].id, "product": self.product.id, "responsible": b_user}),
            ("tickets", {"client": self.A["client"].id, "subject": "x", "assignee": b_user}),
        ]
        for url, body in cases:
            r = self.api_a.post(f"/api/v1/{url}/", body, format="json")
            self.assertEqual(r.status_code, 400, (url, r.content))

    def test_franchisee_is_set_server_side(self):
        r = self.api_a.post("/api/v1/clients/", {"business_name": "Новый", "franchisee": self.b.id}, format="json")
        # чужой франчайзи в payload отклоняется, а без него — подставляется свой
        self.assertEqual(r.status_code, 400)
        r = self.api_a.post("/api/v1/clients/", {"business_name": "Новый"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["franchisee"], self.a.id)
        r = self.api_a.post("/api/v1/leads/", {"name": "Иван", "phone": "0555 1", "source": "inbound_call"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["franchisee"], self.a.id)

    def test_hq_must_pass_franchisee_explicitly(self):
        hq = api(self.hq)
        self.assertEqual(hq.post("/api/v1/clients/", {"business_name": "Х"}, format="json").status_code, 400)
        r = hq.post("/api/v1/clients/", {"business_name": "Х", "franchisee": self.b.id}, format="json")
        self.assertEqual(r.status_code, 201)

    def test_hq_cross_franchisee_fk_rejected(self):
        hq = api(self.hq)
        r = hq.post("/api/v1/franchisee-payments/", {"franchisee": self.a.id, "amount": "10", "method": "cash",
                                                      "contract": self.B["contract"].id}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_manager_sees_only_own_enrollments(self):
        enroll(self.course, self.manager_a)
        ids = self.ids("enrollments", api(self.manager_a))
        self.assertEqual(len(ids), 1)

    def test_franchisee_users_cannot_see_integration_log_or_rating_report(self):
        self.assertEqual(self.api_a.get("/api/v1/integration-events/").status_code, 403)
        self.assertEqual(self.api_a.get("/api/v1/reports/rating/").status_code, 403)

    def test_users_endpoint_scoped(self):
        ids = self.ids("users")
        self.assertIn(self.owner_a.id, ids)
        self.assertNotIn(self.owner_b.id, ids)
        self.assertNotIn(self.hq.id, ids)
        r = self.api_a.post("/api/v1/users/", {"email": "n@t.kg", "password": "pass12345", "role": "super_admin"},
                            format="json")
        self.assertEqual(r.status_code, 400)
        r = self.api_a.post("/api/v1/users/", {"email": "n@t.kg", "password": "pass12345", "role": "franchisee_staff",
                                               "franchisee": self.b.id}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.api_a.post("/api/v1/users/", {"email": "n@t.kg", "password": "pass12345", "role": "franchisee_staff"},
                            format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["franchisee"], self.a.id)

