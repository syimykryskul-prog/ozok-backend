"""Тема, язык интерфейса и переводы backend (ru/ky)."""
import re
from io import StringIO
from pathlib import Path

from babel.messages.pofile import read_po
from django.conf import settings
from django.core.management import call_command
from django.test import TestCase
from django.utils import translation

from accounts.models import User
from core.models import AuditLog
from core.testing import api, make_fr, make_user

RU_BAD_LOGIN = "Неверный email или пароль."
KY_BAD_LOGIN = "Email же сырсөз туура эмес."
RU_BAD_PWD = "Текущий пароль неверный."
KY_BAD_PWD = "Учурдагы сырсөз туура эмес."


def login(email, password="pass12345"):
    r = api().post("/api/v1/auth/login/", {"email": email, "password": password}, format="json")
    c = api()
    c.credentials(HTTP_AUTHORIZATION="Bearer " + r.json()["access"])
    return c


class PreferencesTests(TestCase):
    def test_defaults_in_me_and_login(self):
        u = make_user(make_fr(), "franchisee")
        r = api().post("/api/v1/auth/login/", {"email": u.email, "password": "pass12345"}, format="json")
        self.assertEqual(r.json()["user"]["language"], "ru")
        self.assertEqual(r.json()["user"]["theme"], "system")
        me = api(u).get("/api/v1/auth/me/").json()
        self.assertEqual((me["language"], me["theme"]), ("ru", "system"))

    def test_patch_me_and_preferences(self):
        u = make_user(make_fr(), "franchisee")
        c = api(u)
        r = c.patch("/api/v1/auth/me/", {"language": "ky", "theme": "dark"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["language"], r.json()["theme"]), ("ky", "dark"))
        u.refresh_from_db()
        self.assertEqual((u.language, u.theme), ("ky", "dark"))
        r = c.patch("/api/v1/auth/preferences/", {"theme": "light"}, format="json")
        self.assertEqual(r.json()["theme"], "light")
        self.assertEqual(r.json()["language"], "ky")
        log = AuditLog.objects.filter(entity="accounts.User", entity_id=str(u.id), action="update").first()
        self.assertIsNotNone(log)
        self.assertIn("theme", log.changes)

    def test_patch_validates_and_ignores_other_fields(self):
        u = make_user(make_fr(), "franchisee")
        c = api(u)
        self.assertEqual(c.patch("/api/v1/auth/me/", {"language": "de"}, format="json").status_code, 400)
        self.assertEqual(c.patch("/api/v1/auth/me/", {"theme": "red"}, format="json").status_code, 400)
        c.patch("/api/v1/auth/me/", {"role": "super_admin", "is_active": False}, format="json")
        u.refresh_from_db()
        self.assertEqual(u.role, "franchisee")
        self.assertTrue(u.is_active)
        self.assertEqual(api().patch("/api/v1/auth/me/", {"theme": "dark"}, format="json").status_code, 401)


class LanguageTests(TestCase):
    def test_login_error_by_accept_language(self):
        make_user(email="x@t.kg")
        body = {"email": "x@t.kg", "password": "bad"}
        no_header = api().post("/api/v1/auth/login/", body, format="json")
        ru = api().post("/api/v1/auth/login/", body, format="json", HTTP_ACCEPT_LANGUAGE="ru")
        ky = api().post("/api/v1/auth/login/", body, format="json", HTTP_ACCEPT_LANGUAGE="ky-KG,ky;q=0.9,en;q=0.5")
        self.assertEqual(no_header.json()["detail"], RU_BAD_LOGIN)
        self.assertEqual(ru.json()["detail"], RU_BAD_LOGIN)
        self.assertEqual(ky.json()["detail"], KY_BAD_LOGIN)
        self.assertEqual(ky["Content-Language"], "ky")

    def test_validation_error_ru_and_ky(self):
        u = make_user(email="y@t.kg")
        c = login("y@t.kg")
        body = {"old_password": "wrong", "new_password": "NewPass12345"}
        ru = c.post("/api/v1/auth/change-password/", body, format="json", HTTP_ACCEPT_LANGUAGE="ru")
        ky = c.post("/api/v1/auth/change-password/", body, format="json", HTTP_ACCEPT_LANGUAGE="ky")
        self.assertEqual(ru.status_code, 400)
        self.assertIn(RU_BAD_PWD, str(ru.json()))
        self.assertIn(KY_BAD_PWD, str(ky.json()))
        self.assertIsNotNone(u)

    def test_user_language_used_without_header(self):
        make_user(email="z@t.kg", language="ky")
        c = login("z@t.kg")
        body = {"old_password": "wrong", "new_password": "NewPass12345"}
        r = c.post("/api/v1/auth/change-password/", body, format="json")
        self.assertIn(KY_BAD_PWD, str(r.json()))
        # явный заголовок приоритетнее профиля
        r = c.post("/api/v1/auth/change-password/", body, format="json", HTTP_ACCEPT_LANGUAGE="ru")
        self.assertIn(RU_BAD_PWD, str(r.json()))
        # неподдерживаемый язык в заголовке -> язык профиля
        r = c.post("/api/v1/auth/change-password/", body, format="json", HTTP_ACCEPT_LANGUAGE="en-US")
        self.assertIn(KY_BAD_PWD, str(r.json()))

    def test_default_is_russian_for_ru_user_and_anonymous(self):
        make_user(email="w@t.kg")
        r = login("w@t.kg").post("/api/v1/auth/change-password/", {"old_password": "wrong", "new_password": "NewPass12345"},
                                format="json")
        self.assertIn(RU_BAD_PWD, str(r.json()))

    def test_choice_labels_translated(self):
        with translation.override("ru"):
            self.assertEqual(str(User.Role.FRANCHISEE.label), "Франчайзи")
            self.assertEqual(str(User.Theme.DARK.label), "Тёмная")
        with translation.override("ky"):
            self.assertEqual(str(User.Role.FRANCHISEE.label), "Франчайзи")
            self.assertEqual(str(User.Role.RECRUITER.label), "Рекрутер")
            self.assertEqual(str(User.Role.NETWORK_HEAD.label), "Өнөктөштүк тармактын жетекчиси")
            self.assertEqual(str(User.Theme.DARK.label), "Караңгы")
            self.assertEqual(str(User._meta.get_field("language").verbose_name), "Интерфейстин тили")
        from clients.models import Client
        with translation.override("ky"):
            self.assertEqual(str(Client._meta.verbose_name_plural), "Кардарлар")

    def test_settings_labels_follow_language(self):
        c = api(make_user(role="super_admin"))
        ru = c.get("/api/v1/settings/").json()
        ky = c.get("/api/v1/settings/", HTTP_ACCEPT_LANGUAGE="ky").json()
        rows = lambda d: d["results"] if isinstance(d, dict) and "results" in d else d
        self.assertEqual(len(rows(ru)), len(rows(ky)))
        label = {r["key"]: r["label"] for r in rows(ru)}["leads.overdue_hours"]
        label_ky = {r["key"]: r["label"] for r in rows(ky)}["leads.overdue_hours"]
        self.assertEqual(label, "Срок реакции на заявку, часов")
        self.assertEqual(label_ky, "Өтүнмөгө жооп берүү мөөнөтү, саат")


class CatalogTests(TestCase):
    def setUp(self):
        self.po = Path(settings.BASE_DIR) / "locale" / "ky" / "LC_MESSAGES" / "django.po"

    def test_all_translated_and_placeholders_match(self):
        with self.po.open("rb") as f:
            cat = read_po(f, locale="ky")
        ph = lambda s: sorted(re.findall(r"{[A-Za-z_][A-Za-z0-9_]*(?:[!:][^{}]*)?}", s))
        msgs = [m for m in cat if m.id]
        self.assertGreater(len(msgs), 1000)
        for m in msgs:
            self.assertTrue(m.string, f"нет перевода: {m.id}")
            self.assertEqual(ph(m.id), ph(m.string), m.id)

    def test_compile_command(self):
        out = StringIO()
        call_command("compile_translations", stdout=out)
        self.assertIn("ky:", out.getvalue())
        self.assertTrue(self.po.with_suffix(".mo").exists())

    def test_extract_has_no_missing(self):
        # все строки из кода уже есть в каталоге (иначе запустите extract_translations и переведите)
        from core.management.commands.extract_translations import collect
        with self.po.open("rb") as f:
            have = {m.id for m in read_po(f, locale="ky") if m.id}
        missing = [k for k in collect(Path(settings.BASE_DIR)) if k not in have]
        self.assertEqual(missing, [])
