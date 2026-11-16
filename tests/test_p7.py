"""P7 + P6 (остаток): установки, OnAI split (C10), оборудование, шаблоны, дашборды ролей."""
from datetime import timedelta
from decimal import Decimal as D

from django.test import TestCase

from billing.models import Charge
from clients import installations as isvc
from clients.models import (ChecklistTemplate, Client, ClientSale, Implementation, ImplementerAccrual, Subscription)
from clients.services import create_client_sale
from core.models import today
from core.testing import api, make_client, make_contract, make_fr, make_product, make_rule, make_user
from network.commission import create_tariff, split_amounts
from network.models import Equipment, SetupSplitRule

V1 = "/api/v1/"


class Base(TestCase):
    def setUp(self):
        self.onai = make_product("onai", setup_price=20000, min_setup_price=15000, max_discount_percent=50, payment_source="manual")
        self.fr = make_fr()
        self.admin, self.fin, self.fin2 = make_user(role="super_admin"), make_user(role="finance"), \
            make_user(role="finance")
        self.impl = make_user(role="implementer")
        self.owner = make_user(self.fr)
        self.A, self.FIN, self.FIN2, self.IMPL, self.OWN = (api(self.admin), api(self.fin), api(self.fin2),
                                                            api(self.impl), api(self.owner))
        make_rule(name="Setup", sale_kind="setup", percent=100)
        make_rule(name="Sub", sale_kind="subscription", percent=20)
        make_contract(self.fr, product=self.onai, signed=today() - timedelta(days=100))
        self.rule = SetupSplitRule.objects.create(product=self.onai, total=20000, partner_amount=12000,
                                                  implementer_amount=8000,
                                                  technical_channels=["whatsapp", "instagram", "api_telephony"])
        ChecklistTemplate.objects.create(product=self.onai, items=["Установить", "Подключить WhatsApp"])
        self.tariff = create_tariff(product=self.onai, code="std", name="Std", monthly_price=3000)
        self.client_ = make_client(self.fr)

    def deal(self, price_discount=None):
        line = {"kind": "setup"}
        if price_discount:
            line["discount_amount"] = str(price_discount)
        r = self.OWN.post(V1 + "deals/", {"client": self.client_.id, "product": self.onai.id, "lines": [
            line, {"kind": "subscription", "tariff": self.tariff.id, "months": 1}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        c = self.OWN.post(V1 + f"deals/{r.json()['id']}/confirm/")
        self.assertEqual(c.status_code, 200, c.content)
        return r.json()["id"]


class SplitTests(Base):
    def test_split_amounts_full_and_prorata_c10(self):
        self.assertEqual(split_amounts(self.rule, D(20000)), {"partner": D("12000.00"), "implementer": D("8000.00"),
                                                              "ratio": D(1)})
        s = split_amounts(self.rule, D(16000))  # 12/20 и 8/20 от 16 000
        self.assertEqual((s["partner"], s["implementer"]), (D("9600.00"), D("6400.00")))
        self.assertEqual(split_amounts(self.rule, D(25000))["implementer"], D("8000.00"))

    def test_partner_income_is_partner_share_and_withholding_on_it(self):
        make_contract(make_fr("Другой", "Ош"), product=self.onai)
        sale, _ = create_client_sale(client=self.client_, product=self.onai, kind="setup", amount=20000)
        self.assertEqual(sale.franchisee_income, D("12000.00"))  # не 20 000 (100%)
        self.assertIsNone(sale.commission_rule)
        cheap, _ = create_client_sale(client=self.client_, product=self.onai, kind="setup", amount=16000)
        self.assertEqual(cheap.franchisee_income, D("9600.00"))


class InstallationFlowTests(Base):
    def test_deal_confirm_creates_installation_with_checklist_and_channels(self):
        deal_id = self.deal()
        inst = Implementation.objects.get(deal_id=deal_id)
        self.assertEqual((inst.status, inst.stage, inst.franchisee_id), ("assigned", "contract", self.fr.id))
        self.assertEqual([c["item"] for c in inst.checklist], ["Установить", "Подключить WhatsApp"])
        self.assertEqual([c["channel"] for c in inst.technical_channels], ["whatsapp", "instagram", "api_telephony"])
        self.assertIsNotNone(inst.deadline)
        self.assertEqual(Subscription.objects.get(deal_id=deal_id).status, "awaiting_payment")

    def test_full_process_to_subscription_start_and_implementer_accrual(self):
        deal_id = self.deal()
        inst = Implementation.objects.get(deal_id=deal_id)
        url = V1 + f"installations/{inst.id}/"
        # назначаем внедренца
        self.assertEqual(self.A.patch(url, {"implementer": self.impl.id}, format="json").status_code, 200)
        # недопустимый переход и преждевременное завершение
        self.assertEqual(self.A.post(url + "status/", {"status": "done"}, format="json").status_code, 400)
        for st in ("in_progress", "training"):
            self.assertEqual(self.IMPL.post(url + "status/", {"status": st}, format="json").status_code, 200)
        r = self.IMPL.post(url + "checklist/", {"items": [{"item": "Установить", "done": True}]}, format="json")
        self.assertEqual(r.json()["checklist_progress"], {"done": 1, "total": 2})
        self.assertEqual(self.IMPL.post(url + "training-result/", {"result": "passed"}, format="json").status_code, 200)
        self.assertEqual(self.IMPL.post(url + "status/", {"status": "awaiting_act"}, format="json").status_code, 200)
        # акт подтверждён, но каналы не подключены -> начисления внедренцу нет
        self.assertEqual(self.OWN.post(url + "act/", {"client_confirmed_by": "Айбек К."}, format="json").status_code, 200)
        self.assertFalse(ImplementerAccrual.objects.exists())
        for ch in ("whatsapp", "instagram"):
            self.IMPL.post(url + "channels/", {"channel": ch, "status": "connected"}, format="json")
        self.assertFalse(ImplementerAccrual.objects.exists())
        self.assertEqual(self.OWN.post(url + "channels/", {"channel": "api_telephony", "status": "connected"},
                                       format="json").status_code, 403)
        r = self.IMPL.post(url + "channels/", {"channel": "api_telephony", "status": "connected"}, format="json")
        self.assertEqual(r.json()["technical_status"], "connected")
        acc = ImplementerAccrual.objects.get()
        self.assertEqual((acc.amount, acc.user_id, acc.status, acc.sold_amount), (D("8000.00"), self.impl.id,
                                                                                 "accrued", D("20000.00")))
        # оплата подписки, затем завершение: подписка стартует и активна
        from billing import payments
        sub = Subscription.objects.get(deal_id=deal_id)
        from django.utils import timezone
        payments.register_payment(branch=sub.branch, subscription=sub, amount=3000, paid_at=timezone.now(),
                                  source="manual", external_id="o1", months=1)
        r = self.A.post(url + "status/", {"status": "done"}, format="json")
        self.assertEqual((r.status_code, r.json()["stage"]), (200, "launched"))
        sub.refresh_from_db()
        self.assertEqual((sub.status, sub.started_at is not None), ("active", True))
        # выплата: проверка и выплата разными людьми
        r = self.FIN.post(V1 + f"implementer-accruals/{acc.id}/verify/")
        self.assertEqual(r.json()["status"], "verified")
        self.assertEqual(self.FIN.post(V1 + f"implementer-accruals/{acc.id}/pay/").status_code, 400)
        self.assertEqual(self.FIN2.post(V1 + f"implementer-accruals/{acc.id}/pay/").json()["status"], "paid")
        self.assertEqual(self.IMPL.post(V1 + f"implementer-accruals/{acc.id}/verify/").status_code, 403)
        self.assertEqual(len(self.IMPL.get(V1 + "implementer-accruals/").json()["results"]), 1)

    def test_prorata_implementer_accrual_when_sold_cheaper(self):
        deal_id = self.deal(price_discount=4000)  # установка за 16 000
        inst = Implementation.objects.get(deal_id=deal_id)
        inst.implementer = self.impl
        inst.save()
        for c in ("whatsapp", "instagram", "api_telephony"):
            isvc.set_channel(inst, c, "connected")
        isvc.confirm_act(inst, confirmed_by="Клиент")
        self.assertEqual(ImplementerAccrual.objects.get().amount, D("6400.00"))

    def test_done_requires_act_and_rating_validation(self):
        deal_id = self.deal()
        inst = Implementation.objects.get(deal_id=deal_id)
        inst.implementer = self.impl
        inst.status = "awaiting_act"
        inst.save()
        url = V1 + f"installations/{inst.id}/"
        self.assertEqual(self.A.post(url + "status/", {"status": "done"}, format="json").status_code, 400)
        self.assertEqual(self.OWN.post(url + "rate/", {"rating": 7}, format="json").status_code, 400)
        self.assertEqual(self.OWN.post(url + "rate/", {"rating": 5}, format="json").json()["client_rating"], 5)
        c = self.IMPL.post(url + "comments/", {"text": "Начали"}, format="json")
        self.assertEqual(c.status_code, 201)
        self.assertEqual(len(self.OWN.get(url + "comments/").json()), 1)

    def test_legacy_implementations_endpoint_and_stage_sync(self):
        inst = Implementation.objects.create(franchisee=self.fr, client=self.client_, product=self.onai)
        r = self.OWN.post(V1 + f"implementations/{inst.id}/stage/", {"stage": "setup"}, format="json")
        self.assertEqual(r.status_code, 200)
        inst.refresh_from_db()
        self.assertEqual(inst.status, "in_progress")

    def test_implementer_sees_only_own_installations(self):
        deal_id = self.deal()
        inst = Implementation.objects.get(deal_id=deal_id)
        self.assertEqual(len(self.IMPL.get(V1 + "installations/").json()["results"]), 0)
        inst.implementer = self.impl
        inst.save()
        self.assertEqual(len(self.IMPL.get(V1 + "installations/").json()["results"]), 1)


class EquipmentTests(Base):
    def test_equipment_margin_serials_warranty_delivery(self):
        eq = Equipment.objects.create(name="Принтер", product=self.onai, price=10000, min_price=8000,
                                      purchase_price=7000, warranty_months=12)
        r = self.A.get(V1 + f"equipment/{eq.id}/").json()
        self.assertEqual((r["sale_price"], r["margin"], r["margin_percent"], r["warranty_months"]),
                         ("10000.00", "3000.00", "30.00", 12))
        body = {"client": self.client_.id, "product": self.onai.id, "lines": [
            {"kind": "equipment", "equipment": eq.id, "quantity": 2, "serial_numbers": ["S1", "S2"]}]}
        d = self.OWN.post(V1 + "deals/", body, format="json").json()
        self.OWN.post(V1 + f"deals/{d['id']}/confirm/")
        sale = ClientSale.objects.get(deal_id=d["id"], kind="equipment")
        self.assertEqual((sale.serial_numbers, sale.sold_by_id, sale.warranty_until.year >= today().year),
                         (["S1", "S2"], self.fr.id, True))
        url = V1 + f"equipment-sales/{sale.id}/"
        r = self.OWN.patch(url, {"delivery_status": "delivered", "serial_numbers": ["S1", "S2", "S3"]}, format="json")
        self.assertEqual(r.status_code, 400)  # серийников больше количества
        r = self.OWN.patch(url, {"delivery_status": "installed", "installed_by": self.owner.id}, format="json")
        self.assertEqual((r.status_code, r.json()["delivery_status"]), (200, "installed"))
        self.assertEqual(len(self.OWN.get(V1 + "equipment-sales/").json()["results"]), 1)

    def test_min_price_equipment_setup_training_m1(self):
        self.onai.min_training_price = 3000
        self.onai.training_price = 5000
        self.onai.save()
        r = self.OWN.post(V1 + "deals/", {"client": self.client_.id, "product": self.onai.id, "lines": [
            {"kind": "training", "discount_amount": "3000"}]}, format="json")
        self.assertEqual(r.status_code, 201)
        c = self.OWN.post(V1 + f"deals/{r.json()['id']}/confirm/").json()
        self.assertEqual(c["status"], "pending_approval")  # 2000 < минимума 3000
        bad = self.A.patch(V1 + f"products/{self.onai.id}/", {"min_training_price": "9000"}, format="json")
        self.assertEqual(bad.status_code, 400)


class TemplatesAndRulesTests(Base):
    def test_contract_templates_super_admin_only_write(self):
        r = self.A.post(V1 + "contract-templates/", {"kind": "client", "name": "Договор клиента"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.OWN.post(V1 + "contract-templates/", {"kind": "client", "name": "x"},
                                       format="json").status_code, 403)
        self.assertEqual(len(self.OWN.get(V1 + "contract-templates/").json()["results"]), 1)

    def test_split_rule_and_checklist_endpoints_validated(self):
        bad = self.A.post(V1 + "setup-split-rules/", {"product": self.onai.id, "total": "20000",
                                                      "partner_amount": "12000", "implementer_amount": "7000"},
                          format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.FIN.post(V1 + "setup-split-rules/", {}, format="json").status_code, 403)
        self.assertEqual(self.A.post(V1 + "checklist-templates/", {"product": self.onai.id, "items": ["a"]},
                                     format="json").status_code, 201)

    def test_m5_rule_validation(self):
        base = {"name": "r", "valid_from": "2026-01-01", "percent": "10"}
        r = self.A.post(V1 + "commission-rules/", {**base, "sale_kind": "setup", "tariff": self.tariff.id,
                                                   "product": self.onai.id}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.A.post(V1 + "commission-rules/", {**base, "sale_kind": "setup", "fixed_per": "month"}, format="json")
        self.assertEqual(r.status_code, 400)


class RoleDashboardTests(Base):
    def test_four_role_dashboards(self):
        rec = make_user(role="recruiter")
        self.fr.recruited_by = rec
        self.fr.save()
        d = api(rec).get(V1 + "dashboard/").json()
        self.assertEqual((d["scope"], d["partners_total"]), ("recruiter", 1))
        reg = make_user(role="regional_support", region="Ош")
        self.fr.region = "Ош"
        self.fr.save()
        d = api(reg).get(V1 + "dashboard/").json()
        self.assertEqual((d["scope"], d["clients_total"]), ("regional_support", 1))
        d = self.IMPL.get(V1 + "dashboard/").json()
        self.assertEqual(d["scope"], "implementer")
        self.assertIn("accruals", d)
        d = api(make_user(role="quality")).get(V1 + "dashboard/").json()
        self.assertEqual(d["scope"], "quality")
        self.assertIn("audits_avg_score", d)
        # не утекает общий HQ-дашборд
        self.assertNotIn("network_revenue", d)


class ApplyDueChangesAndAdminTests(TestCase):
    def test_admin_registered(self):
        from django.contrib import admin
        from billing.models import CommissionAccrual, Payment, Payout
        from clients.models import Branch, ImplementerAccrual
        for m in (Branch, Payment, CommissionAccrual, Payout, ImplementerAccrual, SetupSplitRule):
            self.assertIn(m, admin.site._registry)
