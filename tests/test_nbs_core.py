"""P0–P1: настройки, роли, статусы партнёров, аудит, 2FA."""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from billing.models import Charge
from clients.services import create_client_sale
from core import totp
from core.models import AuditLog, add_months, month_first, today
from core.settings_registry import get_decimal, get_int
from core.testing import api, days_ago, make_client, make_contract, make_fr, make_product, make_rule, make_user
from leads.services import create_lead
from network.models import PartnerStatusHistory
from network.partners import tier_on

ROLES = ("super_admin", "network_head", "recruiter", "regional_support", "implementer", "finance", "quality")


def nbs(role, **kw):
    return make_user(role=role, **kw)


class Base(TestCase):
    def setUp(self):
        self.market = make_product("market")
        self.fr = make_fr("OZOK Ош", "Ош")
        self.fr.region = "Ош"
        self.fr.save()
        self.other = make_fr("OZOK Бишкек", "Бишкек")
        self.other.region = "Бишкек"
        self.other.save()
        self.admin_u = nbs("super_admin")
        self.A = api(self.admin_u)
        self.head = nbs("network_head")
        self.H = api(self.head)
        self.fin_u = nbs("finance")
        self.F = api(self.fin_u)
        self.fin2_u = nbs("finance")
        self.F2 = api(self.fin2_u)
        self.owner_u = make_user(self.fr, "franchisee")
        self.O = api(self.owner_u)


class SettingsTests(Base):
    def test_defaults_and_listing(self):
        self.assertEqual(get_decimal("recruiter.reward_amount"), Decimal("13125"))
        self.assertEqual(get_decimal("nbs_lead.subscription_percent"), Decimal("15"))
        self.assertEqual(get_decimal("nbs_lead.nbs_share_percent"), Decimal("30"))
        rows = self.O.get("/api/v1/settings/").json()
        self.assertEqual(next(r for r in rows if r["key"] == "franchise.cash_amount")["value"], "87500")

    def test_patch_permissions_reason_and_audit(self):
        url = "/api/v1/settings/recruiter.reward_amount/"
        self.assertEqual(self.O.patch(url, {"value": "1", "reason": "x"}, format="json").status_code, 403)
        self.assertEqual(self.H.patch(url, {"value": "1", "reason": "x"}, format="json").status_code, 403)
        self.assertEqual(self.A.patch(url, {"value": "20000"}, format="json").status_code, 400)  # причина
        r = self.F.patch(url, {"value": "20000", "reason": "Решение OZOK"}, format="json")  # финансовый ключ
        self.assertEqual((r.status_code, r.json()["value"]), (200, "20000"))
        self.assertEqual(self.F.patch("/api/v1/settings/leads.overdue_hours/", {"value": 5, "reason": "x"},
                                      format="json").status_code, 403)  # не финансовый
        self.assertEqual(self.A.patch("/api/v1/settings/leads.overdue_hours/", {"value": "abc", "reason": "x"},
                                      format="json").status_code, 400)
        log = AuditLog.objects.filter(entity="core.SystemSetting", action="update").first()
        self.assertEqual(log.changes["value"], ["13125", "20000"])
        self.assertEqual(log.reason, "Решение OZOK")

    def test_settings_changes_take_effect(self):
        self.A.patch("/api/v1/settings/recruiter.reward_amount/", {"value": "5000", "reason": "тест"}, format="json")
        rec = nbs("recruiter")
        p = make_fr("Кандидат", "Ош", status="candidate")
        p.recruited_by = rec
        p.save()
        self.assertEqual(p.recruiter_reward.target_amount, Decimal("5000.00"))
        self.A.patch("/api/v1/settings/nbs_lead.share_due_days/", {"value": 10, "reason": "тест"}, format="json")
        lead = create_lead(franchisee=self.fr, name="Л", phone="1", source="nbs_ads", interest="program")
        client = make_client(self.fr)
        client.lead = lead
        client.save()
        make_rule(name="Лид OZOK: установка 70%", sale_kind="setup", lead_source="nbs_ads", percent=70, basis="x")
        make_rule(name="Установка 100%", sale_kind="setup", percent=100)
        sale, _ = create_client_sale(client=client, product=self.market, kind="setup", amount=1000)
        ch = Charge.objects.get(client_sale=sale, kind="nbs_lead_share")
        self.assertEqual(ch.due_date, sale.date + timedelta(days=10))
        self.A.patch("/api/v1/settings/leads.overdue_hours/", {"value": 1, "reason": "тест"}, format="json")
        self.assertEqual(get_int("leads.overdue_hours"), 1)


class RoleMatrixTests(Base):
    def setUp(self):
        super().setUp()
        self.users = {r: nbs(r) for r in ROLES}
        self.users["regional_support"].region = "Ош"
        self.users["regional_support"].save()
        self.users["franchisee"] = self.owner_u
        self.users["franchisee_staff"] = make_user(self.fr, "franchisee_staff")
        self.users["manager"] = make_user(self.fr, "manager")

    def code(self, role, url):
        return api(self.users[role]).get(f"/api/v1/{url}").status_code

    def test_read_matrix(self):
        ALL = set(self.users)
        table = {
            "audit-log/": {"super_admin", "finance"},
            "settings/": ALL,
            "recruiter-rewards/": {"super_admin", "network_head", "recruiter", "finance"},
            "contracts/": {"super_admin", "network_head", "finance", "franchisee"},
            "charges/": {"super_admin", "network_head", "finance", "franchisee"},
            "franchisee-payments/": {"super_admin", "network_head", "finance", "franchisee"},
            "leads/": {"super_admin", "network_head", "regional_support", "finance", "franchisee",
                       "franchisee_staff", "manager"},
            "franchisees/": ALL - {"implementer"},
            "implementations/": {"super_admin", "network_head", "regional_support", "implementer", "finance",
                                 "quality", "franchisee", "franchisee_staff", "manager"},
            "dashboard/": ALL,  # P5+: простые дашборды ролей recruiter/regional_support/implementer/quality
            "partner-status-history/": {"super_admin", "network_head", "finance", "quality"},
            "change-requests/": {"super_admin", "network_head", "finance", "franchisee", "franchisee_staff",
                                 "manager"},
            "commission-rules/": ALL - {"recruiter", "implementer"},
            "users/": {"super_admin", "network_head", "finance", "franchisee", "franchisee_staff", "manager"},
        }
        for url, allowed in table.items():
            for role in self.users:
                self.assertEqual(self.code(role, url), 200 if role in allowed else 403, (role, url))

    def test_write_restrictions(self):
        def post(role, url, body):
            return api(self.users[role]).post(f"/api/v1/{url}", body, format="json").status_code
        for role in self.users:
            ok = role in ("super_admin", "network_head")
            self.assertEqual(post(role, "franchisees/", {"name": "N", "city": "C"}), 201 if ok else 403, role)
        pay = {"franchisee": self.fr.id, "amount": "5", "method": "cash"}
        for role in self.users:
            self.assertEqual(post(role, "franchisee-payments/", pay),
                             201 if role in ("super_admin", "finance") else 403, role)

    def test_recruiter_sees_only_own_partners(self):
        rec = self.users["recruiter"]
        mine = make_fr("Мой", "Ош", status="candidate")
        mine.recruited_by = rec
        mine.save()
        ids = {r["id"] for r in api(rec).get("/api/v1/franchisees/").json()["results"]}
        self.assertEqual(ids, {mine.id})
        self.assertEqual(api(rec).get(f"/api/v1/franchisees/{self.fr.id}/").status_code, 404)
        self.assertIsNone(api(rec).get(f"/api/v1/franchisees/{mine.id}/").json()["balance"])  # без финансов

    def test_regional_support_sees_only_own_region(self):
        c_osh, c_bsk = make_client(self.fr), make_client(self.other)
        ids = {r["id"] for r in api(self.users["regional_support"]).get("/api/v1/clients/").json()["results"]}
        self.assertEqual(ids, {c_osh.id})
        self.assertNotIn(c_bsk.id, ids)

    def test_implementer_sees_only_assigned_installations(self):
        from clients.models import Implementation
        impl = self.users["implementer"]
        mine = Implementation.objects.create(franchisee=self.fr, client=make_client(self.fr), product=self.market,
                                             responsible=impl)
        Implementation.objects.create(franchisee=self.fr, client=make_client(self.fr), product=self.market)
        ids = {r["id"] for r in api(impl).get("/api/v1/implementations/").json()["results"]}
        self.assertEqual(ids, {mine.id})

    def test_protected_fields_masked(self):
        self.fr.inn, self.fr.passport = "12345678901234", "AN1234567"
        self.fr.save()
        d = self.H.get(f"/api/v1/franchisees/{self.fr.id}/").json()
        self.assertEqual((d["inn"], d["passport"]), ("***", "***"))
        for c in (self.A, self.F):
            d = c.get(f"/api/v1/franchisees/{self.fr.id}/").json()
            self.assertEqual((d["inn"], d["passport"]), ("12345678901234", "AN1234567"))
        self.H.patch(f"/api/v1/franchisees/{self.fr.id}/", {"inn": "000"}, format="json")
        self.fr.refresh_from_db()
        self.assertEqual(self.fr.inn, "12345678901234")


class PartnerStatusTests(Base):
    def test_tier_effective_dates(self):
        p = make_fr("Growth", "Ош")
        body = {"partner": p.id, "field": "tier", "new": "growth", "effective_from": "2026-09-01"}
        self.assertEqual(self.H.post("/api/v1/partner-status-history/", {**body, "reason": "x"}, format="json")
                         .status_code, 400)  # задним числом — только super_admin
        self.assertEqual(self.A.post("/api/v1/partner-status-history/", body, format="json").status_code, 400)
        self.assertEqual(self.A.post("/api/v1/partner-status-history/", {**body, "reason": "Growth подтверждён"},
                                     format="json").status_code, 201)
        r = self.H.post("/api/v1/partner-status-history/", {"partner": p.id, "field": "tier", "new": "standard",
                                                           "reason": "Условия не выполнены"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        first_next = add_months(month_first(today()), 1)
        self.assertEqual(r.json()["effective_from"], first_next.isoformat())
        self.assertFalse(r.json()["applied"])
        p.refresh_from_db()
        self.assertEqual(p.tier, "growth")
        self.assertEqual(tier_on(p, date(2026, 8, 31)), "standard")
        self.assertEqual(tier_on(p, date(2026, 9, 1)), "growth")
        self.assertEqual(tier_on(p, today()), "growth")
        self.assertEqual(tier_on(p, first_next), "standard")

    def test_status_history_changes_commission(self):
        make_contract(self.fr, product=self.market, scheme="cash")
        make_rule(name="Growth 30%", product=self.market, sale_kind="subscription", partner_tier="growth",
                  percent=30)
        make_rule(name="Standard 20%", product=self.market, sale_kind="subscription", partner_tier="standard",
                  percent=20)
        self.A.post("/api/v1/partner-status-history/", {"partner": self.fr.id, "field": "tier", "new": "growth",
                                                         "effective_from": str(days_ago(30)),
                                                         "reason": "Growth подтверждён"}, format="json")
        self.A.post("/api/v1/partner-status-history/", {"partner": self.fr.id, "field": "tier", "new": "standard",
                                                         "effective_from": str(days_ago(10)),
                                                         "reason": "Условия не выполнены"}, format="json")
        client = make_client(self.fr)
        s1, _ = create_client_sale(client=client, product=self.market, kind="subscription", amount=3000,
                                   date=days_ago(20))
        s2, _ = create_client_sale(client=client, product=self.market, kind="renewal", amount=3000,
                                   date=days_ago(5))
        self.assertEqual((s1.partner_tier, s1.franchisee_income), ("growth", Decimal("900.00")))
        self.assertEqual((s2.partner_tier, s2.franchisee_income), ("standard", Decimal("600.00")))

    def test_no_subscription_commission_for_non_franchisee_statuses(self):
        make_contract(self.fr, product=self.market, scheme="cash")
        client = make_client(self.fr)
        for status in ("manager", "suspended", "closed", "candidate"):
            self.fr.status = status
            self.fr.save()
            sale, warning = create_client_sale(client=client, product=self.market, kind="renewal", amount=1000,
                                               external_payment_id=f"x-{status}", source="integration")
            self.assertEqual(sale.commission_amount, 0, status)
            # приостановленный (H2): начисление on_hold до решения OZOK; остальные — без начисления
            self.assertIn("on_hold" if status == "suspended" else "статус партнёра", warning)

    def test_status_change_via_patch_writes_history_and_audit(self):
        p = make_fr("Кандидат", "Ош", status="candidate")
        r = self.H.patch(f"/api/v1/franchisees/{p.id}/", {"status": "suspended", "status_reason": "Проверка"},
                         format="json")
        self.assertEqual((r.status_code, r.json()["status"]), (200, "suspended"))
        h = PartnerStatusHistory.objects.get(partner=p, field="status")
        self.assertEqual((h.old, h.new, h.reason), ("candidate", "suspended", "Проверка"))
        self.assertTrue(AuditLog.objects.filter(entity="network.Franchisee", entity_id=str(p.id),
                                                action="status").exists())


class AuditTests(Base):
    def test_money_changes_are_audited(self):
        r = self.F.post("/api/v1/charges/", {"franchisee": self.fr.id, "amount": "100", "kind": "other"},
                        format="json")
        self.assertEqual(r.status_code, 201)
        cid = str(r.json()["id"])
        log = AuditLog.objects.get(entity="billing.Charge", entity_id=cid, action="create")
        self.assertEqual((log.user_id, log.changes["amount"][1]), (self.fin_u.id, "100.00"))
        self.F.patch(f"/api/v1/charges/{cid}/", {"amount": "150"}, format="json")
        upd = AuditLog.objects.get(entity="billing.Charge", entity_id=cid, action="update")
        self.assertEqual(upd.changes["amount"], ["100.00", "150.00"])
        self.F.post(f"/api/v1/charges/{cid}/cancel/", {"reason": "Ошибка"}, format="json")
        self.assertTrue(AuditLog.objects.filter(entity="billing.Charge", action="cancel", reason="Ошибка").exists())

    def test_rule_role_and_login_audited_and_endpoint_filters(self):
        make_rule(name="r", percent=10)
        u = nbs("quality")
        self.A.patch(f"/api/v1/users/{u.id}/", {"role": "network_head"}, format="json")
        self.assertTrue(AuditLog.objects.filter(entity="accounts.User", entity_id=str(u.id)).exists())
        self.assertEqual(AuditLog.objects.filter(entity="network.CommissionRule", action="create").count(), 1)
        c = api()
        c.post("/api/v1/auth/login/", {"email": self.admin_u.email, "password": "pass12345"}, format="json")
        c.post("/api/v1/auth/login/", {"email": self.admin_u.email, "password": "bad"}, format="json")
        self.assertTrue(AuditLog.objects.filter(action="login").exists())
        self.assertTrue(AuditLog.objects.filter(action="login_failed").exists())
        r = self.A.get("/api/v1/audit-log/", {"entity": "accounts.User", "entity_id": u.id}).json()
        self.assertTrue(r["count"] >= 1)
        self.assertEqual(self.A.get("/api/v1/audit-log/", {"action": "login"}).json()["count"], 1)
        self.assertEqual(self.H.get("/api/v1/audit-log/").status_code, 403)

    def test_audit_log_is_immutable(self):
        entry = AuditLog.objects.create(action="export")
        entry.reason = "x"
        with self.assertRaises(RuntimeError):
            entry.save()
        with self.assertRaises(RuntimeError):
            entry.delete()


class TwoFactorTests(Base):
    def test_flow(self):
        c = api()

        def login():
            return c.post("/api/v1/auth/login/", {"email": self.fin_u.email, "password": "pass12345"}, format="json")
        self.assertIn("access", login().json())  # без 2FA — сразу токены
        self.assertEqual(self.O.post("/api/v1/auth/2fa/setup/").status_code, 403)  # роль без 2FA
        secret = self.F.post("/api/v1/auth/2fa/setup/").json()["secret"]
        self.assertEqual(self.F.post("/api/v1/auth/2fa/enable/", {"code": "000000"}, format="json").status_code, 400)
        now = timezone.now().timestamp()
        self.assertEqual(self.F.post("/api/v1/auth/2fa/enable/", {"code": totp.totp_now(secret, now)},
                                     format="json").status_code, 200)
        r = login().json()
        self.assertTrue(r["otp_required"])
        self.assertNotIn("access", r)
        bad = c.post("/api/v1/auth/login/otp/", {"otp_token": r["otp_token"], "code": "123456"}, format="json")
        self.assertEqual(bad.status_code, 401)
        code = totp.totp_now(secret, now + 30)  # код текущего шага уже использован при включении
        ok = c.post("/api/v1/auth/login/otp/", {"otp_token": r["otp_token"], "code": code}, format="json")
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertIn("access", ok.json())
        again = c.post("/api/v1/auth/login/otp/", {"otp_token": r["otp_token"], "code": code}, format="json")
        self.assertEqual(again.status_code, 401)  # повтор кода запрещён
        self.assertEqual(c.post("/api/v1/auth/login/otp/", {"otp_token": "junk", "code": code},
                                format="json").status_code, 401)
        self.F.post("/api/v1/auth/2fa/disable/", {"code": totp.totp_now(secret, now + 30)}, format="json")
        self.fin_u.refresh_from_db()
        self.assertFalse(self.fin_u.otp_enabled)

    def test_login_throttle(self):
        from accounts.views import LoginThrottle
        cache.clear()
        c = api()
        with mock.patch.object(LoginThrottle, "THROTTLE_RATES", {"login": "5/min"}):
            codes = [c.post("/api/v1/auth/login/", {"email": "x@y.kg", "password": "bad"}, format="json").status_code
                     for _ in range(7)]
        cache.clear()
        self.assertEqual(codes[:5], [401] * 5)
        self.assertEqual(codes[5:], [429, 429])
