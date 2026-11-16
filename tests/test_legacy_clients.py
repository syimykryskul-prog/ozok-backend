"""P0+ п.9: существующие клиенты OZOK — без партнёра, без комиссии; переключатель комиссии; импорт; аналитика."""
from datetime import timedelta
from decimal import Decimal as D

from django.test import TestCase
from django.utils import timezone

from billing import payments as psvc
from billing.models import CommissionAccrual, Payment
from clients.models import Branch, Client, Subscription
from core.models import add_months, today
from core.testing import api, make_client, make_contract, make_fr, make_product, make_rule, make_user
from network.commission import create_tariff

V1 = "/api/v1/"


class LegacyBase(TestCase):
    def setUp(self):
        self.product = make_product("coca", payment_source="manual", name="Кока")
        self.product2 = make_product("kezek", payment_source="manual", name="Кезек")
        self.osh = make_fr("Ош", "Ош")
        make_rule(name="Std", product=self.product, partner_tier="standard", sale_kind="subscription", percent=20)
        make_rule(name="StdR", product=self.product, partner_tier="standard", sale_kind="renewal", percent=20)
        make_contract(self.osh, product=self.product, signed=today() - timedelta(days=200))
        self.tariff = create_tariff(product=self.product, code="start", name="Старт", monthly_price=1500)
        self.admin, self.fin = make_user(role="super_admin"), make_user(role="finance")
        self.head = make_user(role="network_head")
        self.owner = make_user(self.osh)
        self.A, self.FIN, self.HEAD, self.OWN = api(self.admin), api(self.fin), api(self.head), api(self.owner)

    def sphere(self, **kw):
        return {"product": self.product.id, "tariff": self.tariff.id, "started_at": str(today() - timedelta(days=200)),
                "paid_until": str(today() + timedelta(days=10)), "external_id": "LEG-1", "elqr_account": "", **kw}

    def create(self, who=None, **kw):
        body = {"business_name": "Кафе «Старое»", "inn": "0123456789", "phone": "0555 111 222", "city": "Ош",
                "spheres": [self.sphere()], **kw}
        return (who or self.FIN).post(V1 + "clients/legacy/", body, format="json")

    def make_legacy(self):
        r = self.create()
        self.assertEqual(r.status_code, 201, r.content)
        client = Client.objects.get(pk=r.json()["id"])
        return client, client.branches.get()

    def pay(self, branch, amount=3000, ext="op-1", d=None):
        return psvc.register_payment(branch=branch, amount=amount, paid_at=timezone.now() if d is None else
                                     timezone.make_aware(timezone.datetime(d.year, d.month, d.day, 12)),
                                     source="manual", external_id=ext, months=2, user=self.fin)[0]


class CreateLegacyTests(LegacyBase):
    def test_create_one_with_spheres_status_computed(self):
        late = self.sphere(product=self.product2.id, tariff=None, monthly_price="2000", external_id="LEG-2",
                           paid_until=str(today() - timedelta(days=60)))
        r = self.create(spheres=[self.sphere(), late])
        self.assertEqual(r.status_code, 201, r.content)
        c = Client.objects.get(pk=r.json()["id"])
        self.assertEqual((c.origin, c.franchisee_id), ("ozok_legacy", None))
        subs = {s.product.code: s for s in c.accounts.all()}
        self.assertEqual(subs["coca"].status, "active")
        self.assertEqual(subs["kezek"].status, "restricted")  # статус считается по paid_until
        self.assertEqual((subs["coca"].external_id, subs["coca"].started_at, subs["kezek"].monthly_fee),
                         ("LEG-1", today() - timedelta(days=200), D("2000.00")))
        b = subs["coca"].branch
        self.assertEqual((b.seller_partner_id, b.implementer_partner_id, b.responsible_partner_id,
                          b.commission_enabled), (None, None, None, False))
        self.assertEqual(r.json()["origin"], "ozok_legacy")

    def test_permissions(self):
        self.assertEqual(self.create(self.A).status_code, 201)
        self.assertEqual(self.create(self.HEAD, inn="9", phone="1", spheres=[self.sphere(external_id="x")])
                         .status_code, 403)
        self.assertEqual(self.create(self.OWN).status_code, 403)
        # право можно выдать роли/пользователю
        user = make_user(role="network_head", permission_overrides={
            "permissions": {"legacy_clients": "write"}, "actions_add": ["import_legacy_clients"]})
        r = self.create(api(user), inn="777", phone="0777", spheres=[self.sphere(external_id="g-1")])
        self.assertEqual(r.status_code, 201, r.content)

    def test_validation_and_duplicates(self):
        self.assertEqual(self.create(spheres=[]).status_code, 400)
        bad = self.sphere(tariff=None)
        self.assertEqual(self.create(spheres=[bad]).status_code, 400)  # ни тарифа, ни цены
        self.assertEqual(self.create(spheres=[self.sphere(paid_until="2000-01-01")]).status_code, 400)
        self.assertEqual(self.create().status_code, 201)
        r = self.create(business_name="Другое", inn="0123456789", spheres=[self.sphere(product=self.product2.id,
                                                                                       external_id="Z")])
        self.assertEqual(r.status_code, 400)  # дубль по ИНН
        cid = Client.objects.get().id
        r = self.create(business_name="Другое", inn="0123456789", merge_into=cid,
                        spheres=[self.sphere(product=self.product2.id, tariff=None, monthly_price="900",
                                             external_id="Z")])
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(Client.objects.get().accounts.count(), 2)


class NoCommissionTests(LegacyBase):
    def accruals(self):
        return CommissionAccrual.objects.count()

    def test_legacy_pays_without_commission_before_and_after_reassignment(self):
        client, branch = self.make_legacy()
        sub = branch.subscriptions.get()
        p = self.pay(branch)
        self.assertEqual((p.status, self.accruals()), ("confirmed", 0))
        self.assertIsNone(p.sale.franchisee_id)
        # передача партнёру на сопровождение — комиссия по-прежнему не начисляется
        r = self.A.post(V1 + f"branches/{branch.id}/reassign/", {
            "partner": self.osh.id, "reason_code": "no_response", "reason": "Передача на сопровождение"},
                        format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.pay(branch, ext="op-2")
        self.assertEqual(self.accruals(), 0)
        # партнёр видит филиал, но не чужих неназначенных клиентов
        self.assertIn(branch.id, [b["id"] for b in self.OWN.get(V1 + "branches/").json()["results"]])
        sub.refresh_from_db()
        self.assertIsNotNone(sub.paid_until)

    def test_switch_only_super_admin_with_audit_and_effective_date(self):
        from core.models import AuditLog
        client, branch = self.make_legacy()
        self.A.post(V1 + f"branches/{branch.id}/reassign/", {
            "partner": self.osh.id, "reason_code": "no_response", "reason": "Передача"}, format="json")
        url = V1 + f"branches/{branch.id}/commission-switch/"
        body = {"enabled": True, "effective_from": str(today()), "reason": "Решение владельца"}
        self.assertEqual(self.FIN.post(url, body, format="json").status_code, 403)
        self.assertEqual(self.OWN.post(url, body, format="json").status_code, 403)
        self.assertEqual(self.A.post(url, {"enabled": True}, format="json").status_code, 400)  # причина обязательна
        eff = today() - timedelta(days=3)
        r = self.A.post(url, {**body, "effective_from": str(today())}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["commission_enabled"], r.json()["commission_enabled_from"]),
                         (True, str(today())))
        self.assertTrue(AuditLog.objects.filter(entity="clients.Branch", entity_id=str(branch.id),
                                                reason="Решение владельца").exists())
        branch.refresh_from_db()
        # до даты начала — нет комиссии, с даты — есть по обычным правилам (20% от 3000)
        self.pay(branch, ext="old", d=eff)
        self.assertEqual(self.accruals(), 0)
        self.pay(branch, ext="new")
        acc = CommissionAccrual.objects.get()
        self.assertEqual((acc.partner_id, acc.amount), (self.osh.id, D("600.00")))
        # выключили — снова нет
        self.A.post(url, {"enabled": False, "reason": "Отмена"}, format="json")
        branch.refresh_from_db()
        self.pay(branch, ext="later")
        self.assertEqual(self.accruals(), 1)

    def test_switch_requires_assigned_partner_and_ozok_client(self):
        client, branch = self.make_legacy()
        r = self.A.post(V1 + f"branches/{branch.id}/commission-switch/", {"enabled": True, "reason": "x"},
                        format="json")
        self.assertEqual(r.status_code, 400)  # некому начислять: филиал не передан партнёру
        pc = make_client(self.osh)
        pb = pc.branches.first() or Branch.objects.create(client=pc, name="Основной")
        self.assertEqual(self.A.post(V1 + f"branches/{pb.id}/commission-switch/", {"enabled": True, "reason": "x"},
                                     format="json").status_code, 400)  # клиент партнёра

    def test_integration_payment_matches_legacy_external_id_without_commission(self):
        client, branch = self.make_legacy()
        self.product.payment_source = "product"
        self.product.save()
        ev = {"event_id": "e1", "type": "payment.succeeded", "external_payment_id": "pp-1",
              "external_client_id": "LEG-1", "amount": "3000", "months": 2,
              "paid_until": add_months(today(), 2).isoformat(), "paid_at": timezone.now().isoformat()}
        r = api().post(V1 + "integrations/events/", ev, format="json", HTTP_X_OZOK_KEY=self.product.api_key)
        self.assertEqual(r.json()["results"][0]["status"], "processed", r.content)
        pay = Payment.objects.get(external_id="pp-1")
        self.assertEqual((pay.branch_id, self.accruals()), (branch.id, 0))


class IsolationTests(LegacyBase):
    def test_partner_never_sees_unassigned_legacy_clients(self):
        client, branch = self.make_legacy()
        other = make_client(self.osh)
        ids = [c["id"] for c in self.OWN.get(V1 + "clients/").json()["results"]]
        self.assertEqual(ids, [other.id])
        self.assertEqual(self.OWN.get(V1 + f"clients/{client.id}/").status_code, 404)
        self.assertEqual(self.OWN.get(V1 + f"branches/{branch.id}/").status_code, 404)
        self.assertEqual(self.OWN.get(V1 + "subscriptions/").json()["count"], 0)
        self.assertEqual(self.OWN.get(V1 + "client-accounts/").json()["count"], 0)
        self.pay(branch)
        self.assertEqual(self.OWN.get(V1 + "payments/").json()["count"], 0)
        self.assertEqual(self.OWN.get(V1 + "reports/ozok-clients/").status_code, 403)
        hq = self.A.get(V1 + "clients/", {"origin": "ozok_legacy"}).json()
        self.assertEqual([c["id"] for c in hq["results"]], [client.id])


class ImportTests(LegacyBase):
    HEADER = "Название;ИНН;Контакт;Телефон;Город;Адрес;Тариф;Цена в месяц;Пользуется с;Оплачено до;ID в продукте;Лицевой счёт Элкюар\n"

    def csv(self, *rows):
        return self.HEADER + "\n".join(rows) + "\n"

    def test_template_download(self):
        r = self.FIN.get(V1 + "clients/legacy-import/template/", {"product": self.product.code})
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r["Content-Type"])
        self.assertIn("ИНН", r.content.decode("utf-8"))
        self.assertEqual(self.FIN.get(V1 + "clients/legacy-import/template/").status_code, 400)
        self.assertEqual(self.OWN.get(V1 + "clients/legacy-import/template/", {"product": "coca"}).status_code, 403)

    def test_preview_confirm_duplicates_and_errors(self):
        client, _ = self.make_legacy()  # ИНН 0123456789, тел. 0555 111 222, LEG-1 по coca
        d = (today() + timedelta(days=20)).strftime("%d.%m.%Y")
        text = self.csv(
            f"Новый 1;111;;0700 000 001;Ош;ул 1;start;;01.01.2025;{d};N-1;",            # новый
            f"Новый 2;222;;0700 000 002;Ош;ул 2;;1800;01.01.2025;{d};N-2;E-77",          # новый, цена вместо тарифа
            f"Дубль по ИНН;0123456789;;0700 000 003;Ош;ул 3;start;;01.01.2025;{d};N-3;",  # дубль, сфера уже есть
            f"Дубль по ID;333;;0700 000 004;Ош;ул 4;start;;01.01.2025;{d};LEG-1;",         # дубль по ID продукта
            f"Без тарифа;444;;0700 000 005;Ош;ул 5;;;01.01.2025;{d};N-5;",                 # ошибка
            f"Плохая дата;555;;0700 000 006;Ош;ул 6;start;;31.31.2025;{d};N-6;",           # ошибка
            f"Повтор в файле;111;;0700 000 007;Ош;ул 7;start;;01.01.2025;{d};N-7;")        # дубль внутри файла
        r = self.FIN.post(V1 + "clients/legacy-import/preview/", {"product": self.product.code, "csv": text},
                          format="json")
        self.assertEqual(r.status_code, 200, r.content)
        rows = {x["line"]: x for x in r.json()["rows"]}
        self.assertEqual([rows[i]["status"] for i in range(2, 9)],
                         ["new", "new", "duplicate", "duplicate", "error", "error", "duplicate"])
        self.assertTrue(any("ИНН" in x for x in rows[4]["duplicates"][0]["reasons"]))
        self.assertTrue(any("ID в продукте" in x for x in rows[5]["duplicates"][0]["reasons"]))
        self.assertFalse(rows[5]["can_merge"])
        self.assertIn("тариф", " ".join(rows[6]["errors"]).lower())
        self.assertEqual(r.json()["summary"], {"new": 2, "duplicate": 3, "error": 2})
        self.assertEqual(Client.objects.count(), 1)  # предпросмотр ничего не пишет
        # подтверждение: дубли пропускаются, ошибки отчитываются построчно, новые создаются
        r = self.FIN.post(V1 + "clients/legacy-import/confirm/", {"product": self.product.code, "csv": text,
                                                                  "duplicates": "skip"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["summary"], {"created": 2, "skipped": 3, "error": 2})
        self.assertEqual(Client.objects.filter(origin="ozok_legacy").count(), 3)
        err = [x for x in r.json()["rows"] if x["status"] == "error"]
        self.assertEqual({x["line"] for x in err}, {6, 7})
        sub = Subscription.objects.get(external_id="N-2")
        self.assertEqual((sub.monthly_fee, sub.branch.elqr_account, sub.status), (D("1800.00"), "E-77", "active"))

    def test_merge_adds_sphere_to_existing_legacy_client(self):
        client, _ = self.make_legacy()
        d = (today() + timedelta(days=20)).strftime("%d.%m.%Y")
        text = self.csv(f"Кафе «Старое»;0123456789;;0555 111 222;Ош;ул 1;;900;01.01.2025;{d};K-1;")
        r = self.FIN.post(V1 + "clients/legacy-import/preview/", {"product": self.product2.code, "csv": text},
                          format="json")
        row = r.json()["rows"][0]
        self.assertEqual((row["status"], row["can_merge"]), ("duplicate", True))
        r = self.FIN.post(V1 + "clients/legacy-import/confirm/", {"product": self.product2.code, "csv": text,
                                                                  "duplicates": "merge"}, format="json")
        self.assertEqual(r.json()["summary"], {"merged": 1}, r.content)
        self.assertEqual(Client.objects.count(), 1)
        self.assertEqual(client.accounts.count(), 2)

    def test_partner_clients_are_not_merged(self):
        own = make_client(self.osh, "Партнёрский")
        own.inn = "999"
        own.save()
        d = (today() + timedelta(days=20)).strftime("%d.%m.%Y")
        text = self.csv(f"Партнёрский;999;;;Ош;;start;;01.01.2025;{d};P-1;")
        r = self.FIN.post(V1 + "clients/legacy-import/confirm/", {"product": self.product.code, "csv": text,
                                                                  "duplicates": "merge"}, format="json")
        self.assertEqual(r.json()["summary"], {"skipped": 1})
        self.assertEqual(own.accounts.count(), 0)

    def test_import_forbidden_for_others(self):
        text = self.csv("X;1;;;;;start;;;;;")
        for who in (self.HEAD, self.OWN):
            for url in ("preview", "confirm"):
                self.assertEqual(who.post(V1 + f"clients/legacy-import/{url}/", {"product": "coca", "csv": text},
                                          format="json").status_code, 403)


class AnalyticsTests(LegacyBase):
    def test_ozok_segment_and_share(self):
        client, branch = self.make_legacy()
        self.pay(branch, 3000)
        partner_client = make_client(self.osh)
        pb = partner_client.branches.first() or None
        from clients import branches as bsvc
        pb = bsvc.ensure_main_branch(partner_client)
        pb.product, pb.tariff = self.product, self.tariff
        pb.save()
        psvc.register_payment(branch=pb, amount=1000, paid_at=timezone.now(), source="manual", external_id="pp",
                              months=1, user=self.fin)
        d = self.A.get(V1 + "reports/ozok-clients/").json()
        self.assertEqual(d["clients"], {"total": 1, "legacy": 1, "direct": 0})
        self.assertEqual(d["subscriptions_by_status"], {"active": 1})
        self.assertEqual((d["mrr"], d["collected"]), ("1500.00", "3000.00"))
        self.assertEqual(d["spheres"][0]["code"], "coca")
        self.assertEqual((d["share"]["ozok"], d["share"]["partners"], d["share"]["ozok_percent"]),
                         ("3000.00", "1000.00", "75.00"))
        s = self.A.get(V1 + "reports/sphere-analytics/", {"product": "coca"}).json()
        self.assertEqual(s["ozok_share"]["ozok_percent"], "75.00")
        self.assertNotIn("ozok_clients", self.OWN.get(V1 + "reports/sphere-analytics/", {"product": "coca"}).json())
