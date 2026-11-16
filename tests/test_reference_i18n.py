"""Двуязычные справочники OZOK: поля *_ky, локализованный вывод, запись обоих языков."""
from django.test import TestCase

from accounts.models import AccessRole
from billing.models import CashCategory
from clients.models import ChecklistTemplate
from core.testing import api, make_fr, make_product, make_user
from network.models import Equipment, Package, Tariff
from training.models import Course

KY = {"HTTP_ACCEPT_LANGUAGE": "ky"}


def rows(r):
    d = r.json()
    return d["results"] if isinstance(d, dict) and "results" in d else d


class ReferenceI18nTests(TestCase):
    def setUp(self):
        self.admin = api(make_user(role="super_admin"))
        self.p = make_product("market", name="Маркет", sphere="Магазины", description="Учёт",
                              name_ky="Базар", sphere_ky="Дүкөндөр")

    def get(self, **kw):
        return self.admin.get(f"/api/v1/products/{self.p.id}/", **kw).json()

    def test_ky_header_returns_ky_values_and_raw_pair(self):
        ky = self.get(**KY)
        self.assertEqual((ky["name"], ky["sphere"]), ("Базар", "Дүкөндөр"))
        self.assertEqual((ky["name_ru"], ky["name_ky"]), ("Маркет", "Базар"))
        self.assertEqual((ky["sphere_ru"], ky["sphere_ky"]), ("Магазины", "Дүкөндөр"))
        ru = self.get()
        self.assertEqual((ru["name"], ru["sphere"]), ("Маркет", "Магазины"))
        self.assertEqual(ru["name_ky"], "Базар")

    def test_empty_ky_falls_back_to_ru(self):
        ky = self.get(**KY)
        self.assertEqual(ky["description"], "Учёт")  # description_ky пусто
        self.assertEqual(ky["description_ky"], "")

    def test_user_language_without_header(self):
        make_user(email="k@t.kg", role="super_admin", language="ky")
        r = api().post("/api/v1/auth/login/", {"email": "k@t.kg", "password": "pass12345"}, format="json")
        c = api()
        c.credentials(HTTP_AUTHORIZATION="Bearer " + r.json()["access"])
        self.assertEqual(c.get(f"/api/v1/products/{self.p.id}/").json()["name"], "Базар")

    def test_both_languages_editable(self):
        r = self.admin.patch(f"/api/v1/products/{self.p.id}/", {"name_ru": "Маркет 2", "name_ky": "Базар 2"},
                             format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.p.refresh_from_db()
        self.assertEqual((self.p.name, self.p.name_ky), ("Маркет 2", "Базар 2"))
        self.admin.patch(f"/api/v1/products/{self.p.id}/", {"name": "Маркет 3", "sphere_ky": ""}, format="json")
        self.p.refresh_from_db()
        self.assertEqual((self.p.name, self.p.name_ky, self.p.sphere_ky), ("Маркет 3", "Базар 2", ""))
        created = self.admin.post("/api/v1/products/", {"code": "newp", "name": "Новый", "name_ky": "Жаңы",
                                                        "sphere": "Кафе", "sphere_ky": "Кафе"}, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()["name_ky"], "Жаңы")

    def test_other_reference_models(self):
        pkg = Package.objects.create(name="Пакет", name_ky="Пакет-ky", price_cash=1, price_prepay=1, price_per_sale=1)
        t = Tariff.objects.create(product=self.p, code="t1", name="Старт", name_ky="Баштоо")
        Equipment.objects.create(name="Весы", name_ky="Таразы", price=1)
        CashCategory.objects.create(code="zz", name="Аренда", name_ky="Ижара", direction="out")
        Course.objects.create(title="Курс", title_ky="Курс-ky")
        role = AccessRole.objects.create(code="rr", name="Роль", name_ky="Ролу", description="Описание",
                                         description_ky="Сүрөттөмө")
        for url, field, ky in (("packages", "name", "Пакет-ky"), ("tariffs", "name", "Баштоо"),
                               ("equipment", "name", "Таразы"), ("cash-categories", "name", "Ижара"),
                               ("courses", "title", "Курс-ky"), ("access-roles", "name", "Ролу")):
            got = {x[field] for x in rows(self.admin.get(f"/api/v1/{url}/", **KY))}
            self.assertIn(ky, got, url)
            raw = rows(self.admin.get(f"/api/v1/{url}/"))
            self.assertTrue(all(f"{field}_ky" in x and f"{field}_ru" in x for x in raw), url)
        self.assertEqual(self.admin.get(f"/api/v1/access-roles/{role.id}/", **KY).json()["description"], "Сүрөттөмө")
        self.assertEqual(pkg.name, "Пакет")
        self.assertEqual(t.name, "Старт")

    def test_nested_names_are_localized(self):
        fr = make_fr()
        t = Tariff.objects.create(product=self.p, code="t2", name="Бизнес", name_ky="Бизнес-ky")
        r = rows(self.admin.get("/api/v1/tariffs/", **KY))
        self.assertIn(("Базар", "Бизнес-ky"), {(x["product_name"], x["name"]) for x in r})
        self.assertIsNotNone(fr)
        self.assertIsNotNone(t)

    def test_checklist_template_pairs(self):
        r = self.admin.post("/api/v1/checklist-templates/",
                            {"product": self.p.id, "items": ["Один", {"ru": "Два", "ky": "Эки"}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["items"], [{"ru": "Один", "ky": ""}, {"ru": "Два", "ky": "Эки"}])
        tid = r.json()["id"]
        ky = self.admin.get(f"/api/v1/checklist-templates/{tid}/", **KY).json()
        self.assertEqual(ky["labels"], ["Один", "Эки"])
        self.assertTrue(ChecklistTemplate.objects.filter(pk=tid).exists())

    def test_reports_use_localized_sphere(self):
        for url in ("reports/sphere-analytics/?product=market", "reports/sales/", "reports/ozok-clients/"):
            self.assertEqual(self.admin.get(f"/api/v1/{url}", **KY).status_code, 200, url)
        sales = self.admin.get("/api/v1/reports/sales/", **KY).json()
        names = {r["name"] for r in sales["by_product"]}
        self.assertIn("Базар", names)
        spheres = self.admin.get("/api/v1/reports/sphere-analytics/?product=market", **KY).json()
        self.assertIn("Базар", str(spheres))

    def test_reference_fill_dictionary(self):
        from django.apps import apps

        from core import reference_ky
        prod = make_product("kafe", name="Кафе-прод", sphere="Магазины")
        reference_ky.fill(apps.get_model)
        prod.refresh_from_db()
        self.assertEqual(prod.sphere_ky, "Дүкөндөр")
