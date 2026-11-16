"""P0+ п.8: роли доступа из БД, индивидуальные отличия, защитные правила, приглашение и смена пароля."""
from django.test import TestCase

from accounts.models import AccessRole, User
from billing.models import Charge
from core import access
from core import permissions as P
from core.exceptions import BusinessError
from core.models import AuditLog
from core.testing import api, make_client, make_fr, make_user
from network.models import Franchisee

V1 = "/api/v1/"


class Base(TestCase):
    def setUp(self):
        self.admin = make_user(role="super_admin")
        self.A = api(self.admin)
        self.fr = make_fr()
        self.owner = make_user(self.fr)
        self.OWN = api(self.owner)

    def put(self, user, overrides):
        user.permission_overrides = overrides
        user.save()
        return api(user)


class SeedEquivalenceTests(Base):
    def test_seeded_roles_match_hardcoded_matrix_exactly(self):
        for role in P.ALL_ROLES:
            u = make_user(self.fr if role in P.PARTNER_ROLES else None, role)
            eff = access.compute(u)
            for res in access.ALL_RESOURCES:
                expected = P.role_access(role, res)
                want = {None: 0, P.R: 1, P.RW: 2}[expected]
                if role == "super_admin":
                    want = 2
                self.assertEqual(eff.level(res), want, (role, res))
            for (res, act), roles in P.ACTION_ROLES.items():
                self.assertEqual(eff.can(res, act), role in roles, (role, res, act))

    def test_system_roles_exist_and_users_linked(self):
        self.assertEqual(AccessRole.objects.filter(is_system=True).count(), len(P.ALL_ROLES))
        self.assertEqual(self.admin.access_role.code, "super_admin")
        self.assertEqual(AccessRole.objects.get(code="recruiter").scope, "own_partners")

    def test_me_returns_effective_permissions_actions_modules(self):
        fin = make_user(role="finance")
        d = api(fin).get(V1 + "auth/me/").json()
        self.assertEqual(d["permissions"]["payouts"], "write")
        self.assertEqual(d["permissions"].get("users", "none"), "read")  # имена сотрудников для страницы кандидатов
        self.assertIn("payouts.pay", d["actions"])
        self.assertEqual(d["modules"]["cash"], "write")
        self.assertEqual((d["access_role"]["code"], d["scope"], d["must_change_password"]), ("finance", "all", False))
        sa = self.A.get(V1 + "auth/me/").json()
        self.assertEqual(sa["permissions"]["users"], "write")


class OverridesTests(Base):
    def test_override_grants_read_access_through_api(self):
        rec = make_user(role="recruiter")
        self.assertEqual(api(rec).get(V1 + "clients/").status_code, 403)
        c = self.put(rec, {"permissions": {"clients": "read"}})
        self.assertEqual(c.get(V1 + "clients/").status_code, 200)
        self.assertEqual(c.post(V1 + "clients/", {"business_name": "x"}, format="json").status_code, 403)
        c = self.put(rec, {"permissions": {"clients": "write"}})
        self.assertEqual(c.post(V1 + "clients/", {"business_name": "x", "franchisee": self.fr.id},
                                format="json").status_code, 201)

    def test_override_denies_access(self):
        staff = make_user(self.fr, "franchisee_staff")
        self.assertEqual(api(staff).get(V1 + "leads/").status_code, 200)
        c = self.put(staff, {"permissions": {"leads": "none"}})
        self.assertEqual(c.get(V1 + "leads/").status_code, 403)
        c = self.put(staff, {"permissions": {"leads": "read"}})
        self.assertEqual(c.get(V1 + "leads/").status_code, 200)
        self.assertEqual(c.post(V1 + "leads/", {"name": "x", "phone": "1"}, format="json").status_code, 403)

    def test_actions_add_and_remove(self):
        head = make_user(role="network_head")
        ch = Charge.objects.create(franchisee=self.fr, kind="other", amount=10)
        self.assertEqual(api(head).post(V1 + f"charges/{ch.id}/cancel/", {"reason": "x"}, format="json")
                         .status_code, 403)
        c = self.put(head, {"permissions": {"charges": "write"}, "actions_add": ["cancel_financial"]})
        self.assertEqual(c.post(V1 + f"charges/{ch.id}/cancel/", {"reason": "x"}, format="json").status_code, 200)
        fin = make_user(role="finance")
        fin2 = make_user(role="finance")
        from billing.models import Payout
        from core.models import today
        po = Payout.objects.create(partner=self.fr, period=today().replace(day=1), amount=10, status="verified",
                                   verified_by=fin2)
        c = self.put(fin, {"actions_remove": ["payouts.pay"]})
        self.assertEqual(c.post(V1 + f"payouts/{po.id}/pay/").status_code, 403)
        c = self.put(fin, {})
        self.assertEqual(c.post(V1 + f"payouts/{po.id}/pay/").status_code, 200)

    def test_scope_override_region_vs_all(self):
        osh, bsk = make_fr("Ош", "Ош"), make_fr("Бишкек", "Бишкек")
        osh.region, bsk.region = "Ош", "Бишкек"
        osh.save(), bsk.save()
        make_client(osh), make_client(bsk)
        reg = make_user(role="regional_support", region="Ош")
        self.assertEqual(api(reg).get(V1 + "clients/").json()["count"], 1)
        c = self.put(reg, {"scope": "all"})
        self.assertEqual(c.get(V1 + "clients/").json()["count"], 2)
        rec = make_user(role="recruiter")
        bsk.recruited_by = rec
        bsk.save()
        self.assertEqual(api(rec).get(V1 + "franchisees/").json()["count"], 1)
        self.assertEqual(self.put(rec, {"scope": "all"}).get(V1 + "franchisees/").json()["count"], 3)

    def test_super_admin_overrides_ignored(self):
        self.admin.permission_overrides = {"permissions": {"clients": "none"}}
        self.admin.save()
        self.assertEqual(api(self.admin).get(V1 + "clients/").status_code, 200)

    def test_invalid_overrides_rejected_by_api(self):
        u = make_user(role="finance")
        r = self.A.patch(V1 + f"users/{u.id}/", {"permission_overrides": {"permissions": {"nope": "write"}}},
                         format="json")
        self.assertEqual(r.status_code, 400)
        r = self.A.patch(V1 + f"users/{u.id}/", {"permission_overrides": {"permissions": {"clients": "read"},
                                                                          "scope": "own_region"}}, format="json")
        self.assertEqual(r.status_code, 200, r.content)


class RolesApiTests(Base):
    def test_custom_role_flow_duplicate_and_assignment(self):
        r = self.A.post(V1 + f"access-roles/{AccessRole.objects.get(code='finance').id}/duplicate/",
                        {"name": "Бухгалтер (чтение)"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        role = r.json()
        self.assertFalse(role["is_system"])
        perms = {k: ("read" if v == "write" else v) for k, v in role["permissions"].items()}
        r = self.A.patch(V1 + f"access-roles/{role['id']}/", {"permissions": perms, "actions": []}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        inv = self.A.post(V1 + "users/invite/", {"email": "buh@nbs.kg", "first_name": "Б", "access_role": role["id"]},
                          format="json")
        self.assertEqual(inv.status_code, 201, inv.content)
        u = User.objects.get(email="buh@nbs.kg")
        self.assertEqual((u.role, u.access_role_id), ("finance", role["id"]))
        c = api(u)
        u.must_change_password = False
        u.save()
        self.assertEqual(c.get(V1 + "cash-transactions/").status_code, 200)
        self.assertEqual(c.post(V1 + "cash-transactions/", {"direction": "in", "amount": "1"}, format="json")
                         .status_code, 403)
        self.assertEqual(self.A.delete(V1 + f"access-roles/{role['id']}/").status_code, 400)  # назначена
        u.delete()
        self.assertEqual(self.A.delete(V1 + f"access-roles/{role['id']}/").status_code, 204)

    def test_system_role_editable_but_not_deletable_and_super_admin_locked(self):
        rid = AccessRole.objects.get(code="quality").id
        self.assertEqual(self.A.delete(V1 + f"access-roles/{rid}/").status_code, 400)
        r = self.A.patch(V1 + f"access-roles/{rid}/", {"description": "Контроль"}, format="json")
        self.assertEqual(r.status_code, 200)
        sid = AccessRole.objects.get(code="super_admin").id
        self.assertEqual(self.A.patch(V1 + f"access-roles/{sid}/", {"permissions": {}}, format="json").status_code, 400)

    def test_role_validation_and_permissions(self):
        bad = self.A.post(V1 + "access-roles/", {"name": "x", "base_role": "finance", "permissions": {"zzz": "write"},
                                                 "actions": [], "scope": "all"}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(api(make_user(role="finance")).post(V1 + "access-roles/", {"name": "x"}, format="json")
                         .status_code, 403)
        self.assertEqual(self.OWN.get(V1 + "access-roles/").status_code, 200)
        codes = {r["base_role"] for r in self.OWN.get(V1 + "access-roles/").json()["results"]}
        self.assertTrue(codes <= {"franchisee_staff", "manager"})

    def test_role_changes_are_audited(self):
        rid = AccessRole.objects.get(code="quality").id
        self.A.patch(V1 + f"access-roles/{rid}/", {"scope": "own_region"}, format="json")
        self.assertTrue(AuditLog.objects.filter(entity="accounts.AccessRole", entity_id=str(rid),
                                                action="update").exists())
        u = make_user(role="finance")
        self.A.patch(V1 + f"users/{u.id}/", {"permission_overrides": {"permissions": {"clients": "read"}}},
                     format="json")
        log = AuditLog.objects.filter(entity="accounts.User", entity_id=str(u.id), action="update").first()
        self.assertIn("permission_overrides", log.changes)

    def test_effective_permissions_endpoint(self):
        u = make_user(role="finance")
        u.permission_overrides = {"permissions": {"clients": "write"}, "actions_remove": ["payouts.pay"]}
        u.save()
        d = self.A.get(V1 + f"users/{u.id}/effective-permissions/").json()
        self.assertEqual(d["permissions"]["clients"], "write")
        self.assertNotIn("payouts.pay", d["actions"])
        self.assertEqual(d["role_permissions"].get("clients"), "read")
        self.assertEqual(self.OWN.get(V1 + f"users/{u.id}/effective-permissions/").status_code, 404)


class GuardTests(Base):
    def test_last_super_admin_cannot_be_disabled_or_demoted(self):
        with self.assertRaises(BusinessError):
            self.admin.is_active = False
            self.admin.save()
        self.admin.refresh_from_db()
        with self.assertRaises(BusinessError):
            self.admin.role = "finance"
            self.admin.save()
        self.admin.refresh_from_db()
        second = make_user(role="super_admin")
        r = self.A.post(V1 + f"users/{second.id}/deactivate/", {"reason": "Увольнение"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        second.refresh_from_db()
        self.assertFalse(second.is_active)
        # второй отключён -> первый снова единственный: понизить через API нельзя (даже другим админом не выйдет)
        second.is_active = True
        second.save()
        r = api(second).patch(V1 + f"users/{self.admin.id}/", {"role": "finance"}, format="json")
        self.assertEqual(r.status_code, 200)  # второй админ может понизить первого: он остаётся
        r = self.A.post(V1 + f"users/{second.id}/deactivate/", {}, format="json")
        self.assertIn(r.status_code, (400, 403))

    def test_self_actions_blocked(self):
        self.assertEqual(self.A.post(V1 + f"users/{self.admin.id}/deactivate/", {}, format="json").status_code, 400)
        self.assertEqual(self.A.post(V1 + f"users/{self.admin.id}/reset-password/", {}, format="json").status_code, 400)

    def test_owner_can_grant_only_partner_modules_within_own_rights(self):
        base = {"email": "s@p.kg", "first_name": "С", "role": "franchisee_staff"}
        ok = self.OWN.post(V1 + "users/invite/", base, format="json")
        self.assertEqual(ok.status_code, 201, ok.content)
        self.assertEqual(User.objects.get(email="s@p.kg").franchisee_id, self.fr.id)
        for i, ov in enumerate((
                {"permissions": {"cash_transactions": "read"}},        # не партнёрский раздел
                {"permissions": {"charges": "write"}},                 # выше собственных прав (у владельца read)
                {"actions_add": ["payouts.pay"]},                      # чужое особое право
        )):
            r = self.OWN.post(V1 + "users/invite/", {**base, "email": f"x{i}@p.kg", "permission_overrides": ov},
                              format="json")
            self.assertEqual(r.status_code, 400, (ov, r.content))
        fin_role = AccessRole.objects.get(code="finance")
        r = self.OWN.post(V1 + "users/invite/", {**base, "email": "n@p.kg", "access_role": fin_role.id},
                          format="json")
        self.assertEqual(r.status_code, 400)
        r = self.OWN.post(V1 + "users/invite/", {**base, "email": "y@p.kg",
                                                 "permission_overrides": {"permissions": {"leads": "read"}}},
                          format="json")
        self.assertEqual(r.status_code, 201, r.content)
        staff = make_user(self.fr, "franchisee_staff")
        self.assertEqual(api(staff).post(V1 + "users/invite/", base, format="json").status_code, 403)
        other = make_user(make_fr("Другой", "Ош"), "franchisee_staff")
        self.assertEqual(self.OWN.post(V1 + f"users/{other.id}/deactivate/", {}, format="json").status_code, 404)

    def test_owner_cannot_change_own_rights(self):
        r = self.OWN.patch(V1 + f"users/{self.owner.id}/", {"permission_overrides": {
            "permissions": {"leads": "read"}}}, format="json")
        self.assertEqual(r.status_code, 400)


class InviteFlowTests(Base):
    def test_invite_login_blocked_until_change_password(self):
        r = self.A.post(V1 + "users/invite/", {"email": "new@nbs.kg", "first_name": "Нов", "last_name": "Ый",
                                               "phone": "0555", "role": "finance"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        tmp = r.json()["temporary_password"]
        self.assertTrue(r.json()["must_change_password"])
        self.assertNotIn("password", r.json())
        c = api()
        login = c.post(V1 + "auth/login/", {"email": "new@nbs.kg", "password": tmp}, format="json")
        self.assertEqual(login.status_code, 200, login.content)
        self.assertTrue(login.json()["user"]["must_change_password"])
        U = api()
        U.credentials(HTTP_AUTHORIZATION="Bearer " + login.json()["access"])
        blocked = U.get(V1 + "dashboard/")
        self.assertEqual(blocked.status_code, 403)
        self.assertIn("пароль", blocked.json()["detail"].lower())
        me = U.get(V1 + "auth/me/")
        self.assertEqual((me.status_code, me.json()["must_change_password"]), (200, True))
        self.assertEqual(U.post(V1 + "auth/change-password/", {"old_password": "bad", "new_password": "Zx9-secure-pass"},
                                format="json").status_code, 400)
        self.assertEqual(U.post(V1 + "auth/change-password/", {"old_password": tmp, "new_password": "123"},
                                format="json").status_code, 400)
        self.assertEqual(U.post(V1 + "auth/change-password/", {"old_password": tmp, "new_password": tmp},
                                format="json").status_code, 400)
        ok = U.post(V1 + "auth/change-password/", {"old_password": tmp, "new_password": "Zx9-secure-pass"},
                    format="json")
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertEqual(U.get(V1 + "dashboard/").status_code, 200)
        self.assertEqual(api().post(V1 + "auth/login/", {"email": "new@nbs.kg", "password": tmp},
                                    format="json").status_code, 401)
        u = User.objects.get(email="new@nbs.kg")
        self.assertEqual((u.invited_by_id, u.must_change_password), (self.admin.id, False))
        self.assertTrue(AuditLog.objects.filter(entity="accounts.User", entity_id=str(u.id)).exists())

    def test_reset_password_and_deactivate_activate(self):
        u = make_user(role="finance")
        r = self.A.post(V1 + f"users/{u.id}/reset-password/", {}, format="json")
        self.assertEqual(r.status_code, 200)
        tmp = r.json()["temporary_password"]
        u.refresh_from_db()
        self.assertTrue(u.must_change_password and u.check_password(tmp))
        self.assertEqual(self.A.post(V1 + f"users/{u.id}/deactivate/", {"reason": "Отпуск"}, format="json")
                         .status_code, 200)
        self.assertEqual(api().post(V1 + "auth/login/", {"email": u.email, "password": tmp}, format="json")
                         .status_code, 401)
        self.assertEqual(self.A.post(V1 + f"users/{u.id}/deactivate/", {}, format="json").status_code, 400)
        self.assertEqual(self.A.post(V1 + f"users/{u.id}/activate/", {}, format="json").status_code, 200)
        self.assertEqual(api().post(V1 + "auth/login/", {"email": u.email, "password": tmp}, format="json")
                         .status_code, 200)
        statuses = AuditLog.objects.filter(entity="accounts.User", entity_id=str(u.id), action="status")
        self.assertGreaterEqual(statuses.count(), 2)

    def test_invite_roles_and_duplicates_validated(self):
        self.assertEqual(self.A.post(V1 + "users/invite/", {"email": "a@b.kg", "role": "franchisee"},
                                     format="json").status_code, 400)  # партнёрская роль без партнёра
        ok = self.A.post(V1 + "users/invite/", {"email": "p@b.kg", "role": "franchisee_staff",
                                                "franchisee": self.fr.id}, format="json")
        self.assertEqual(ok.status_code, 201, ok.content)
        self.assertEqual(self.A.post(V1 + "users/invite/", {"email": "p@b.kg", "role": "finance"},
                                     format="json").status_code, 400)
        self.assertEqual(api(make_user(role="finance")).post(V1 + "users/invite/", {"email": "q@b.kg",
                                                                                    "role": "finance"},
                                                             format="json").status_code, 403)
