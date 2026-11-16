"""P5: филиалы, подписки по филиалам, второй филиал (C8, C9, H4, H10), уход партнёра (H3)."""
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from clients import branches as bsvc
from clients import subscriptions as ssvc
from clients.models import Branch, BranchAssignmentHistory, Client, ClientProductAccount, Subscription
from core.models import today
from core.testing import api, make_client, make_contract, make_fr, make_product, make_user
from leads.models import Lead
from network.commission import create_tariff
from network.models import Franchisee
from quality.models import Ticket

V1 = "/api/v1/"


class Base(TestCase):
    def setUp(self):
        self.product = make_product("coca")
        self.osh, self.bsk = make_fr("OZOK Ош", "Ош"), make_fr("OZOK Бишкек", "Бишкек")
        self.admin = make_user(role="super_admin")
        self.owner, self.bowner = make_user(self.osh), make_user(self.bsk)
        self.A, self.OWN, self.BSK = api(self.admin), api(self.owner), api(self.bowner)
        make_contract(self.osh, product=self.product)
        make_contract(self.bsk, product=self.product)
        self.tariff = create_tariff(product=self.product, code="start", name="Старт", monthly_price=1500)


class MainBranchTests(Base):
    def test_c5_main_branch_partners_equal_client_partner(self):
        client = make_client(self.osh)
        sub = Subscription.objects.create(client=client, product=self.product, external_id="x1")
        b = sub.branch
        self.assertTrue(b.is_main)
        self.assertEqual((b.seller_partner_id, b.implementer_partner_id, b.responsible_partner_id),
                         (self.osh.id, self.osh.id, self.osh.id))
        self.assertEqual(client.owner_partner_id, self.osh.id)
        self.assertIs(ClientProductAccount, Subscription)

    def test_first_branch_defaults_and_second_branch_priority(self):
        client = make_client(self.osh)
        r1 = self.A.post(V1 + "branches/", {"client": client.id, "name": "Главный", "elqr_account": "Q1"},
                         format="json")
        self.assertEqual(r1.status_code, 201, r1.content)
        b1 = r1.json()
        self.assertTrue(b1["is_main"])
        self.assertEqual((b1["implementer_partner"], b1["responsible_partner"], b1["seller_partner"]),
                         (self.osh.id,) * 3)
        r2 = self.A.post(V1 + "branches/", {"client": client.id, "name": "Филиал 2", "elqr_account": "Q2"},
                         format="json")
        b2 = r2.json()
        self.assertEqual(b2["priority_partner"], self.osh.id)
        self.assertIsNone(b2["implementer_partner"])
        deadline = timezone.datetime.fromisoformat(b2["priority_deadline"].replace("Z", "+00:00"))
        self.assertAlmostEqual((deadline - timezone.now()).total_seconds(), 3 * 86400, delta=120)
        # дубль лицевого счёта
        dup = self.A.post(V1 + "branches/", {"client": client.id, "name": "X", "elqr_account": "Q2"}, format="json")
        self.assertEqual(dup.status_code, 400)
        # партнёр сам создаёт второй филиал — закрепляется сразу
        r3 = self.OWN.post(V1 + "branches/", {"client": client.id, "name": "Филиал 3"}, format="json")
        self.assertEqual(r3.json()["implementer_partner"], self.osh.id)
        # OZOK не может назначить чужого партнёра при создании
        bad = self.A.post(V1 + "branches/", {"client": client.id, "name": "Чужой",
                                             "implementer_partner": self.bsk.id}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_accept_and_reassign_with_history_and_isolation(self):
        client = make_client(self.osh)
        self.A.post(V1 + "branches/", {"client": client.id, "name": "Главный"}, format="json")
        b2 = self.A.post(V1 + "branches/", {"client": client.id, "name": "Филиал 2"}, format="json").json()
        self.assertEqual(self.BSK.post(V1 + f"branches/{b2['id']}/accept/").status_code, 404)  # скрыт от чужого
        # reassign: основание обязательно
        self.assertEqual(self.A.post(V1 + f"branches/{b2['id']}/reassign/", {"partner": self.bsk.id},
                                     format="json").status_code, 400)
        self.assertEqual(self.A.post(V1 + f"branches/{b2['id']}/reassign/", {
            "partner": self.bsk.id, "reason_code": "bogus", "reason": "x"}, format="json").status_code, 400)
        r = self.A.post(V1 + f"branches/{b2['id']}/reassign/", {
            "partner": self.bsk.id, "reason_code": "no_response", "reason": "Нет ответа 3 дня",
            "effective_from": str(today() - timedelta(days=10))}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        hist = BranchAssignmentHistory.objects.get(branch_id=b2["id"])
        self.assertEqual((hist.from_partner_id, hist.to_partner_id, hist.reason_code, hist.from_priority),
                         (self.osh.id, self.bsk.id, "no_response", True))  # было за приоритетным партнёром (osh)
        # C9: osh не видит филиал 2 и его подписки; bsk видит клиента, но только свой филиал
        ids = [x["id"] for x in self.OWN.get(V1 + "branches/").json()["results"]]
        self.assertNotIn(b2["id"], ids)
        c = self.BSK.get(V1 + f"clients/{client.id}/").json()
        self.assertEqual([x["id"] for x in c["branches"]], [b2["id"]])
        self.assertEqual(self.BSK.get(V1 + "clients/").json()["count"], 1)

    def test_accept_by_priority_partner(self):
        client = make_client(self.osh)
        self.A.post(V1 + "branches/", {"client": client.id, "name": "Главный"}, format="json")
        b2 = self.A.post(V1 + "branches/", {"client": client.id, "name": "Филиал 2"}, format="json").json()
        r = self.OWN.post(V1 + f"branches/{b2['id']}/accept/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["implementer_partner"], self.osh.id)
        self.assertEqual(self.OWN.post(V1 + f"branches/{b2['id']}/accept/").status_code, 400)

    def test_payee_resolved_as_of_date_c8(self):
        client = make_client(self.osh)
        b1 = bsvc.ensure_main_branch(client)
        self.assertEqual(bsvc.payee_on(b1, today())[0].id, self.osh.id)
        bsvc.reassign(b1, self.bsk, reason_code="other", reason="Передача", user=self.admin,
                      effective_from=today() - timedelta(days=5))
        b1 = Branch.objects.get(pk=b1.pk)
        self.assertEqual(bsvc.payee_on(b1, today() - timedelta(days=6))[0].id, self.osh.id)
        self.assertEqual(bsvc.payee_on(b1, today() - timedelta(days=5))[0].id, self.bsk.id)
        self.assertEqual(bsvc.payee_on(b1, today())[0].id, self.bsk.id)

    def test_h10_implementer_not_responsible_gets_no_commission(self):
        client = make_client(self.osh)
        b = bsvc.ensure_main_branch(client)
        b.responsible_partner = self.bsk
        b.save()
        partner, why = bsvc.payee_on(Branch.objects.get(pk=b.pk), today())
        self.assertIsNone(partner)
        self.assertIn("H10", why)

    def test_h4_lead_only_for_branches_created_from_lead(self):
        lead = Lead.objects.create(name="Лид", phone="1", source="nbs_ads")
        client = make_client(self.osh)
        client.lead = lead
        client.save()
        main = bsvc.ensure_main_branch(client)
        self.assertEqual(main.effective_lead, lead)
        b2 = Branch.objects.create(client=client, name="2")
        self.assertIsNone(b2.effective_lead)

    def test_contacts_and_client_fields(self):
        client = make_client(self.osh)
        r = self.OWN.patch(V1 + f"clients/{client.id}/", {"legal_name": "ОсОО Нур", "inn": "123", "region": "Ош"},
                           format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["legal_name"], "ОсОО Нур")
        c = self.OWN.post(V1 + "client-contacts/", {"client": client.id, "full_name": "Айбек", "is_primary": True},
                          format="json")
        self.assertEqual(c.status_code, 201, c.content)
        # владелец не меняет лид клиента, OZOK меняет
        lead = Lead.objects.create(name="L", phone="1", source="nbs_ads")
        self.assertEqual(self.OWN.patch(V1 + f"clients/{client.id}/", {"lead": lead.id}, format="json").status_code, 400)
        self.assertEqual(self.A.patch(V1 + f"clients/{client.id}/", {"lead": lead.id}, format="json").status_code, 200)


class SubscriptionStatusTests(Base):
    def make(self, paid_until, **kw):
        client = make_client(self.osh)
        return Subscription.objects.create(client=client, product=self.product, external_id="s1",
                                           paid_until=paid_until, **kw)

    def test_status_by_paid_until(self):
        t = today()
        cases = [(t + timedelta(days=1), "active"), (t, "active"), (t - timedelta(days=3), "grace"),
                 (t - timedelta(days=10), "overdue"), (t - timedelta(days=60), "restricted"), (None, "awaiting_payment")]
        for i, (paid, expected) in enumerate(cases):
            sub = Subscription.objects.create(client=make_client(self.osh), product=self.product,
                                              external_id=f"e{i}", paid_until=paid)
            self.assertEqual(ssvc.status_for(sub), expected, paid)

    def test_tariff_overrides_product_days(self):
        self.product.grace_days, self.product.restrict_after_days = 2, 20
        self.product.save()
        sub = self.make(today() - timedelta(days=3))
        self.assertEqual(ssvc.status_for(sub), "overdue")
        self.tariff.grace_days = 10
        self.tariff.save()
        sub.tariff = self.tariff
        self.assertEqual(ssvc.status_for(sub), "grace")

    def test_command_updates_statuses_and_keeps_closed(self):
        a = self.make(today() - timedelta(days=60))
        b = Subscription.objects.create(client=a.client, product=self.product, external_id="s2",
                                        paid_until=today() - timedelta(days=60), status="closed",
                                        closed_at=today())
        out = StringIO()
        call_command("update_subscription_statuses", stdout=out)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.status, b.status), ("restricted", "closed"))
        self.assertIn("1", out.getvalue())

    def test_close_action_and_periods_endpoint_shape(self):
        sub = self.make(today() + timedelta(days=5))
        r = self.A.get(V1 + f"subscriptions/{sub.id}/periods/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("periods", r.json())
        self.assertEqual(self.A.post(V1 + f"subscriptions/{sub.id}/close/", {}, format="json").status_code, 400)
        self.assertEqual(self.A.post(V1 + f"subscriptions/{sub.id}/close/", {"reason": "Ушёл"},
                                     format="json").status_code, 200)

    def test_client_accounts_alias_still_works(self):
        sub = self.make(today())
        r = self.OWN.get(V1 + "client-accounts/")
        self.assertEqual(r.json()["count"], 1)
        self.assertEqual(r.json()["results"][0]["id"], sub.id)


class ClosurePartnerTests(Base):
    def test_close_partner_h3(self):
        client = make_client(self.osh)
        b = bsvc.ensure_main_branch(client)
        ticket = Ticket.objects.create(franchisee=self.osh, client=client, subject="Помогите")
        lead = Lead.objects.create(name="Л", phone="2", franchisee=self.osh, status="in_progress")
        from rest_framework_simplejwt.tokens import RefreshToken
        refresh = RefreshToken.for_user(self.owner)
        closed_at = today() - timedelta(days=2)
        bad = self.A.post(V1 + f"franchisees/{self.osh.id}/close/", {"reason": "x"}, format="json")
        self.assertEqual(bad.status_code, 400)
        r = self.A.post(V1 + f"franchisees/{self.osh.id}/close/", {
            "closed_at": str(closed_at), "reason": "Расторжение", "transfer_to_partner": self.bsk.id}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["transfer"]["branches"], 1)
        self.osh.refresh_from_db()
        self.assertEqual((self.osh.status, self.osh.closed_at), ("closed", closed_at))
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_active)
        login = api().post(V1 + "auth/login/", {"email": self.owner.email, "password": "pass12345"}, format="json")
        self.assertIn(login.status_code, (400, 401))
        ref = api().post(V1 + "auth/refresh/", {"refresh": str(refresh)}, format="json")
        self.assertEqual(ref.status_code, 401)
        from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
        self.assertEqual(BlacklistedToken.objects.count(), 1)
        client.refresh_from_db(), ticket.refresh_from_db(), lead.refresh_from_db(), b.refresh_from_db()
        self.assertEqual((client.franchisee_id, ticket.franchisee_id, lead.franchisee_id, b.responsible_partner_id),
                         (self.bsk.id,) * 4)
        # платежи до closed_at — партнёру, после — новому ответственному (C8/H3)
        self.assertEqual(bsvc.payee_on(b, closed_at - timedelta(days=1))[0].id, self.osh.id)
        self.assertEqual(bsvc.payee_on(b, closed_at)[0].id, self.bsk.id)
        # договор закрыт, отчёт «Передачи клиентов»
        self.assertFalse(self.osh.contracts.exclude(status="closed").exists())
        rep = self.A.get(V1 + "branch-assignments/", {"reason_code": "partner_closed"}).json()
        self.assertEqual(rep["count"], 1)
        self.assertEqual(self.A.post(V1 + f"franchisees/{self.osh.id}/close/", {
            "reason": "ещё раз", "transfer_to_partner": self.bsk.id}, format="json").status_code, 400)

    def test_close_to_nbs_user(self):
        client = make_client(self.osh)
        b = bsvc.ensure_main_branch(client)
        support = make_user(role="regional_support")
        r = self.A.post(V1 + f"franchisees/{self.osh.id}/close/", {
            "reason": "Уход", "transfer_to_user": support.id}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        b.refresh_from_db()
        self.assertEqual((b.responsible_partner_id, b.support_user_id), (None, support.id))
        self.assertIsNone(bsvc.payee_on(b, today())[0])

    def test_only_nbs_head_can_close(self):
        r = self.OWN.post(V1 + f"franchisees/{self.osh.id}/close/", {"reason": "x"}, format="json")
        self.assertEqual(r.status_code, 403)


class ApplyDueChangesCommandTests(Base):
    def test_command_applies_scheduled_tier_change(self):
        from network.models import PartnerStatusHistory
        self.bsk.tier = "growth"
        self.bsk.save()
        PartnerStatusHistory.objects.create(partner=self.bsk, field="tier", old="growth", new="standard",
                                            effective_from=today(), applied=False)
        out = StringIO()
        call_command("apply_due_changes", stdout=out)
        self.bsk.refresh_from_db()
        self.assertEqual(self.bsk.tier, "standard")
        self.assertIn("1", out.getvalue())
