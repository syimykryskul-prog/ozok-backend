"""P2–P4: воронка кандидата, рекрутёр (FIFO), отмена вместо удаления, лиды OZOK, двухшаговая ставка."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from billing.models import Charge, CommissionPayout, FranchiseePayment, RecruiterReward
from clients.models import ClientSale
from clients.services import create_client_sale, refund_sale
from core.models import AuditLog, ChangeRequest
from core.testing import api, days_ago, make_client, make_contract, make_fr, make_rule, make_user
from leads.models import Lead
from leads.services import create_lead
from network.models import Franchisee, PartnerStatusHistory
from training.models import Course
from training.services import enroll

from .test_nbs_core import Base, nbs


class FunnelTests(Base):
    def test_candidate_to_manager_via_exam_and_three_sales(self):
        rec = nbs("recruiter")
        r = self.H.post("/api/v1/franchisees/", {"name": "Азамат", "full_name": "Азамат Т.", "city": "Ош",
                                                  "status": "candidate", "recruited_by": rec.id,
                                                  "candidate_source": "instagram"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        pid = r.json()["id"]
        self.assertEqual((r.json()["funnel_stage"], r.json()["responsible"]), ("new_application", self.head.id))
        for stage in ("interview", "training", "exam"):
            self.assertEqual(self.H.patch(f"/api/v1/franchisees/{pid}/", {"funnel_stage": stage},
                                          format="json").status_code, 200)
        for bad in ("third_sale", "activated"):  # автоматические этапы вручную нельзя
            self.assertEqual(self.H.patch(f"/api/v1/franchisees/{pid}/", {"funnel_stage": bad},
                                          format="json").status_code, 400)
        p = Franchisee.objects.get(pk=pid)
        self.assertEqual(p.status, "trainee")
        user = make_user(p, "manager")
        course = Course.objects.create(title="Экзамен менеджера", pass_score=80)
        e = enroll(course, user)
        self.A.post(f"/api/v1/enrollments/{e.id}/complete/", {"score": 85}, format="json")
        p.refresh_from_db()
        self.assertEqual((p.certification_result, p.status, p.funnel_stage), ("passed", "manager", "manager"))
        client = make_client(p)
        stages = []
        for i in range(3):
            create_client_sale(client=client, product=self.market, kind="setup", amount=1000 + i)
            p.refresh_from_db()
            stages.append(p.funnel_stage)
        self.assertEqual(stages, ["first_sale", "second_sale", "third_sale"])
        hist = list(PartnerStatusHistory.objects.filter(partner=p, field="funnel_stage")
                    .order_by("id").values_list("new", flat=True))
        self.assertEqual(hist[0], "interview")
        self.assertIn("third_sale", hist)
        sale, _ = create_client_sale(client=client, product=self.market, kind="renewal", amount=1000)
        self.assertEqual(sale.commission_amount, 0)  # менеджер — без абонентских комиссий

    def test_contract_activation_makes_franchisee(self):
        p = make_fr("Менеджер", "Ош", status="manager")
        p.funnel_stage = "third_sale"
        p.save()
        make_contract(p, product=self.market, scheme="cash")
        p.refresh_from_db()
        self.assertEqual((p.status, p.funnel_stage, p.tier), ("franchisee", "activated", "standard"))

    def test_failed_exam_and_funnel_board(self):
        p = make_fr("Стажёр", "Ош", status="trainee")
        p.funnel_stage = "exam"
        p.save()
        Franchisee.objects.filter(pk=p.pk).update(funnel_stage_since=days_ago(20))
        user = make_user(p, "manager")
        e = enroll(Course.objects.create(title="Э", pass_score=80), user)
        self.A.post(f"/api/v1/enrollments/{e.id}/complete/", {"score": 50}, format="json")
        p.refresh_from_db()
        self.assertEqual((p.certification_result, p.status, p.funnel_stage), ("failed", "trainee", "exam"))
        board = self.H.get("/api/v1/franchisees/funnel/").json()
        exam = next(s for s in board["stages"] if s["stage"] == "exam")
        self.assertEqual((exam["count"], exam["items"][0]["stuck"], exam["items"][0]["days_in_stage"]), (1, True, 20))
        self.assertEqual(self.H.get("/api/v1/franchisees/", {"stuck_days": 15}).json()["count"], 1)


class RecruiterTests(Base):
    def setUp(self):
        super().setUp()
        self.rec = nbs("recruiter")
        self.p = make_fr("Новый франчайзи", "Ош", status="manager")
        self.p.recruited_by = self.rec
        self.p.save()
        self.contract = make_contract(self.p, product=self.market, scheme="hybrid", fee=100000, upfront_amount=10000,
                                      installments_total=90000, installments_count=3, withhold_percent=0)

    def pay(self, amount, method="bank", **kw):
        return FranchiseePayment.objects.create(franchisee=self.p, contract=self.contract, amount=amount,
                                                method=method, date=timezone.localdate(), **kw)

    def reward(self):
        return RecruiterReward.objects.get(partner=self.p)

    def test_no_accrual_before_money_then_fifo_cap(self):
        self.assertEqual(self.reward().accrued_amount, 0)
        self.pay(10000)
        r = self.reward()
        self.assertEqual((r.accrued_amount, r.status), (Decimal("10000.00"), "accruing"))
        self.pay(30000)
        r = self.reward()
        self.assertEqual((r.accrued_amount, r.status), (Decimal("13125.00"), "accrued"))
        self.pay(30000)
        self.assertEqual(self.reward().accrued_amount, Decimal("13125.00"))
        self.assertEqual([a.amount for a in self.reward().allocations.all()],
                         [Decimal("10000.00"), Decimal("3125.00")])

    def test_cancel_payment_reallocates_fifo(self):
        p1, p2 = self.pay(10000), self.pay(30000)
        self.F.post(f"/api/v1/franchisee-payments/{p1.id}/cancel/", {"reason": "Ошибка"}, format="json")
        r = self.reward()
        self.assertEqual((r.accrued_amount, r.status), (Decimal("13125.00"), "accrued"))
        self.assertEqual([(a.payment_id, a.amount) for a in r.allocations.all()], [(p2.id, Decimal("13125.00"))])
        self.F.post(f"/api/v1/franchisee-payments/{p2.id}/cancel/", {"reason": "Ошибка"}, format="json")
        r = self.reward()
        self.assertEqual((r.accrued_amount, r.status), (Decimal("0.00"), "accruing"))
        self.assertEqual(FranchiseePayment.all_objects.get(pk=p2.id).status, "cancelled")

    def test_setup_offsets_do_not_count_but_subscription_offsets_do(self):
        sale_setup, _ = create_client_sale(client=make_client(self.p), product=self.market, kind="setup", amount=50000)
        sale_sub = ClientSale.objects.create(franchisee=self.p, client=make_client(self.p), product=self.market,
                                             kind="renewal", amount=1000)
        self.pay(5000, "offset", client_sale=sale_setup)
        self.assertEqual(self.reward().accrued_amount, 0)  # C3: деньги остались у франчайзи
        self.pay(2000, "offset", client_sale=sale_sub)
        self.assertEqual(self.reward().accrued_amount, Decimal("2000.00"))

    def test_verify_pay_different_people_and_paid_immutable(self):
        p1 = self.pay(40000)
        rid = self.reward().id
        self.assertEqual(self.F.post(f"/api/v1/recruiter-rewards/{rid}/pay/", {}, format="json").status_code, 400)
        self.assertEqual(api(self.rec).post(f"/api/v1/recruiter-rewards/{rid}/verify/", {}, format="json")
                         .status_code, 403)
        self.assertEqual(self.F.post(f"/api/v1/recruiter-rewards/{rid}/verify/", {}, format="json").status_code, 200)
        self.assertEqual(self.F.post(f"/api/v1/recruiter-rewards/{rid}/pay/", {}, format="json").status_code, 400,
                         "verified_by != paid_by")
        r = self.F2.post(f"/api/v1/recruiter-rewards/{rid}/pay/", {"payment_ref": "ПП-1"}, format="json")
        self.assertEqual((r.status_code, r.json()["status"], r.json()["paid_amount"]), (200, "paid", "13125.00"))
        self.F.post(f"/api/v1/franchisee-payments/{p1.id}/cancel/", {"reason": "Возврат"}, format="json")
        r = self.reward()
        self.assertEqual((r.status, r.paid_amount, r.debt_amount), ("paid", Decimal("13125.00"), Decimal("13125.00")))

    def test_cancelled_after_verify_returns_to_accruing(self):
        p1 = self.pay(40000)
        self.F.post(f"/api/v1/recruiter-rewards/{self.reward().id}/verify/", {}, format="json")
        self.F.post(f"/api/v1/franchisee-payments/{p1.id}/cancel/", {"reason": "x"}, format="json")
        r = self.reward()
        self.assertEqual((r.status, r.verified_by), ("accruing", None))

    def test_recruiter_isolation_and_totals_only(self):
        self.pay(10000)
        other_rec = nbs("recruiter")
        mine = api(self.rec).get("/api/v1/recruiter-rewards/").json()["results"]
        self.assertEqual(len(mine), 1)
        self.assertIsNone(mine[0]["allocations"])  # M7: только итоги
        self.assertEqual(api(other_rec).get("/api/v1/recruiter-rewards/").json()["count"], 0)
        self.assertEqual(len(self.F.get(f"/api/v1/recruiter-rewards/{mine[0]['id']}/").json()["allocations"]), 1)

    def test_any_nbs_employee_only_with_flag(self):
        u = nbs("network_head")
        p = make_fr("Ещё", "Ош", status="candidate")
        p.recruited_by = u
        p.save()
        self.assertFalse(RecruiterReward.objects.filter(partner=p).exists())  # H9: без флага награды нет
        u.recruiter_reward_enabled = True
        u.save()
        p.save()
        self.assertTrue(RecruiterReward.objects.filter(partner=p).exists())

    def test_franchise_offers_fill_contract(self):
        offers = self.A.get("/api/v1/franchise-offers/").json()["results"]
        self.assertEqual({o["name"] for o in offers}, {"Сразу", "Рассрочка до"})
        inst = next(o for o in offers if o["name"] == "Рассрочка до")
        self.assertEqual((inst["total"], inst["upfront"], inst["installments_count"]), ("175000.00", "8750.00", 4))
        fr = make_fr("Оферта", "Ош", status="manager")
        fr.funnel_stage = "franchise_contract"
        fr.save()
        r = self.A.post("/api/v1/contracts/", {"franchisee": fr.id, "product": self.market.id, "offer": inst["id"],
                                               "payment_scheme": "hybrid"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.json()["fee_total"], r.json()["upfront_amount"], r.json()["installments_count"]),
                         ("175000.00", "8750.00", 4))
        self.A.post(f"/api/v1/contracts/{r.json()['id']}/activate/")
        sums = sorted(Charge.objects.filter(contract_id=r.json()["id"]).values_list("amount", flat=True))
        self.assertEqual(sums, [Decimal("8750.00")] + [Decimal("41562.50")] * 4)
        self.assertEqual(self.O.post("/api/v1/franchise-offers/", {"name": "x", "total": 1}, format="json")
                         .status_code, 403)


class CancelInsteadOfDeleteTests(Base):
    def test_financial_resources_cannot_be_deleted(self):
        ch = Charge.objects.create(franchisee=self.fr, amount=100)
        pay = FranchiseePayment.objects.create(franchisee=self.fr, amount=100, method="cash")
        po = CommissionPayout.objects.create(franchisee=self.fr, amount=100, method="cash")
        self.assertEqual(self.A.delete(f"/api/v1/commission-payouts/{po.id}/").status_code, 405)  # история (D1)
        for url, obj in (("charges", ch), ("franchisee-payments", pay)):
            self.assertEqual(self.A.delete(f"/api/v1/{url}/{obj.id}/").status_code, 405, url)
            self.assertEqual(self.F.post(f"/api/v1/{url}/{obj.id}/cancel/", {}, format="json").status_code, 400)
            r = self.F.post(f"/api/v1/{url}/{obj.id}/cancel/", {"reason": "Ошибка"}, format="json")
            self.assertEqual(r.status_code, 200, url)
            self.assertEqual(self.F.post(f"/api/v1/{url}/{obj.id}/cancel/", {"reason": "Ещё"}, format="json")
                             .status_code, 400)
            self.assertEqual(self.F.patch(f"/api/v1/{url}/{obj.id}/", {"amount": "1"}, format="json").status_code, 400)
            self.assertEqual(self.F.get(f"/api/v1/{url}/").json()["count"], 0)
            self.assertEqual(self.F.get(f"/api/v1/{url}/", {"cancelled": "true"}).json()["count"], 1)
        self.assertEqual(Charge.all_objects.count(), 1)
        from billing.services import finance_map
        self.assertEqual(finance_map([self.fr.id])[self.fr.id]["balance"], 0)

    def test_cancel_sale_cancels_derived_records(self):
        make_contract(self.fr, product=self.market, scheme="per_sale", fee=50000)
        sale, _ = create_client_sale(client=make_client(self.fr), product=self.market, kind="setup", amount=10000)
        self.assertEqual(sale.withheld_amount, Decimal("3000.00"))
        r = self.F.post(f"/api/v1/client-sales/{sale.id}/cancel/", {"reason": "Ошибка ввода"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Charge.objects.filter(client_sale=sale).count(), 0)
        self.assertEqual(Charge.all_objects.filter(client_sale=sale, cancelled_at__isnull=False).count(), 1)
        self.assertEqual(FranchiseePayment.objects.filter(client_sale=sale).count(), 0)
        self.assertTrue(AuditLog.objects.filter(entity="clients.ClientSale", action="cancel").exists())


class LeadTests(Base):
    def setUp(self):
        super().setUp()
        self.O2 = api(make_user(self.other, "franchisee"))

    def test_partner_lead_needs_owner_confirmation(self):
        r = self.O.post("/api/v1/leads/", {"name": "Своя", "phone": "0555", "source": "partner_ads"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertFalse(r.json()["owner_confirmed"])
        lid = r.json()["id"]
        self.assertEqual(self.O.post(f"/api/v1/leads/{lid}/confirm-owner/").status_code, 403)
        r = self.H.post(f"/api/v1/leads/{lid}/confirm-owner/")
        self.assertEqual((r.status_code, r.json()["owner_confirmed"]), (200, True))
        self.assertEqual(Lead.objects.get(pk=lid).owner_confirmed_by_id, self.head.id)
        self.assertEqual(self.H.post(f"/api/v1/leads/{lid}/confirm-owner/").status_code, 400)
        nbs_lead = self.A.post("/api/v1/leads/", {"name": "OZOK", "phone": "1", "source": "nbs_ads",
                                                  "franchisee": self.fr.id}, format="json").json()
        self.assertTrue(nbs_lead["owner_confirmed"])

    def test_partner_change_request_flow(self):
        lead = create_lead(franchisee=self.fr, name="Л", phone="1", source="nbs_ads", interest="program")
        for field, value in (("source", "partner_ads"), ("franchisee", self.other.id), ("implementer", 1)):
            self.assertEqual(self.O.patch(f"/api/v1/leads/{lead.id}/", {field: value}, format="json").status_code, 400)
        url = f"/api/v1/leads/{lead.id}/change-request/"
        self.assertEqual(self.O.post(url, {"field": "source", "value": "partner_ads"}, format="json").status_code, 400)
        r = self.O.post(url, {"field": "source", "value": "partner_ads", "reason": "Привёл сам"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        crid = r.json()["id"]
        lead.refresh_from_db()
        self.assertEqual(lead.source, "nbs_ads")
        self.assertEqual(self.O.post(f"/api/v1/change-requests/{crid}/approve/").status_code, 403)
        self.assertEqual(self.O2.get("/api/v1/change-requests/").json()["count"], 0)
        self.assertEqual(self.O.get("/api/v1/change-requests/").json()["count"], 1)
        r = self.H.post(f"/api/v1/change-requests/{crid}/approve/", {"comment": "ok"}, format="json")
        self.assertEqual((r.status_code, r.json()["status"]), (200, "approved"))
        lead.refresh_from_db()
        self.assertEqual(lead.source, "partner_ads")
        self.assertEqual(self.H.post(f"/api/v1/change-requests/{crid}/approve/").status_code, 400)
        self.assertTrue(AuditLog.objects.filter(entity="core.ChangeRequest", action="approve").exists())
        self.assertTrue(AuditLog.objects.filter(entity="leads.Lead", entity_id=str(lead.id)).exists())
        acts = self.A.get(f"/api/v1/leads/{lead.id}/activities/").json()["results"]
        self.assertIn("change_request", {a["type"] for a in acts})

    def test_reject_change_request(self):
        lead = create_lead(franchisee=self.fr, name="Л", phone="1", source="nbs_ads", interest="program")
        cr = self.O.post(f"/api/v1/leads/{lead.id}/change-request/", {"field": "source", "value": "referral",
                                                                      "reason": "x"}, format="json").json()
        self.H.post(f"/api/v1/change-requests/{cr['id']}/reject/", {"comment": "Нет"}, format="json")
        lead.refresh_from_db()
        self.assertEqual(lead.source, "nbs_ads")
        self.assertEqual(ChangeRequest.objects.get(pk=cr["id"]).status, "rejected")

    def test_approval_roles_come_from_settings(self):
        lead = create_lead(franchisee=self.fr, name="Л", phone="1", source="nbs_ads", interest="program")
        cr = self.O.post(f"/api/v1/leads/{lead.id}/change-request/", {"field": "source", "value": "referral",
                                                                      "reason": "x"}, format="json").json()
        self.A.patch("/api/v1/settings/approvals.roles/", {"value": ["super_admin"], "reason": "тест"}, format="json")
        self.assertEqual(self.H.post(f"/api/v1/change-requests/{cr['id']}/approve/").status_code, 400)
        self.assertEqual(self.A.post(f"/api/v1/change-requests/{cr['id']}/approve/").status_code, 200)

    def test_reassign_needs_reason_and_logs(self):
        lead = create_lead(franchisee=self.fr, name="Л", phone="1", interest="program")
        self.assertEqual(self.H.post(f"/api/v1/leads/{lead.id}/assign/", {"franchisee": self.other.id},
                                     format="json").status_code, 400)
        r = self.H.post(f"/api/v1/leads/{lead.id}/assign/", {"franchisee": self.other.id,
                                                               "reason": "Нет ответа 24 часа"}, format="json")
        self.assertEqual((r.status_code, r.json()["franchisee"]), (200, self.other.id))
        acts = self.H.get(f"/api/v1/leads/{lead.id}/activities/").json()["results"]
        self.assertEqual(acts[0]["type"], "reassign")
        self.assertIn("Нет ответа", acts[0]["text"])
        self.assertTrue(AuditLog.objects.filter(entity="leads.Lead", entity_id=str(lead.id), action="update",
                                                reason="Нет ответа 24 часа").exists())
        free = create_lead(name="Св", phone="2", interest="franchise")
        self.assertEqual(self.H.post(f"/api/v1/leads/{free.id}/assign/", {"franchisee": self.fr.id},
                                     format="json").status_code, 200)  # первое назначение — без причины

    def test_new_statuses_and_pipeline(self):
        lead = create_lead(franchisee=self.fr, name="Л", phone="1", interest="program")
        self.assertEqual(lead.status, "new")
        self.O.post(f"/api/v1/leads/{lead.id}/take/")
        for st in ("demo", "proposal", "contract"):
            r = self.O.post(f"/api/v1/leads/{lead.id}/status/", {"status": st}, format="json")
            self.assertEqual((r.status_code, r.json()["status"]), (200, st))
        self.assertEqual(self.O.post(f"/api/v1/leads/{lead.id}/status/", {"status": "paid"}, format="json")
                         .status_code, 400)
        r = self.O.post(f"/api/v1/leads/{lead.id}/win/", {}, format="json")
        self.assertEqual((r.json()["status"], bool(r.json()["client_id"])), ("paid", True))
        self.assertTrue(self.O.get(f"/api/v1/leads/{lead.id}/activities/").json()["count"] >= 3)

    def test_sla_overdue_follows_setting_and_next_contact(self):
        lead = create_lead(franchisee=self.fr, name="Л", phone="1", interest="program")
        Lead.objects.filter(pk=lead.pk).update(assigned_at=timezone.now() - timedelta(hours=30))
        self.assertEqual(self.A.get("/api/v1/leads/", {"overdue": "true"}).json()["count"], 1)
        self.A.patch("/api/v1/settings/leads.overdue_hours/", {"value": 48, "reason": "тест"}, format="json")
        self.assertEqual(self.A.get("/api/v1/leads/", {"overdue": "true"}).json()["count"], 0)
        Lead.objects.filter(pk=lead.pk).update(status="in_progress",
                                               next_contact_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(self.A.get("/api/v1/leads/", {"overdue": "true"}).json()["count"], 1)

    def test_franchise_request_lead_not_actionable_by_requester(self):
        lead = create_lead(franchisee=self.fr, name="Запрос", phone="1", interest="franchise")
        self.assertEqual(self.O.post(f"/api/v1/leads/{lead.id}/take/").status_code, 403)
        self.assertEqual(self.O.post(f"/api/v1/leads/{lead.id}/win/", {}, format="json").status_code, 403)
        self.assertEqual(self.O.patch(f"/api/v1/leads/{lead.id}/", {"comment": "x"}, format="json").status_code, 403)

    def test_product_accepts_id_and_new_sources(self):
        r = self.O.post("/api/v1/leads/", {"name": "П", "phone": "1", "product": self.market.id,
                                           "source": "whatsapp", "company_name": "ОсОО"}, format="json")
        self.assertEqual((r.status_code, r.json()["product"], r.json()["company_name"]), (201, "market", "ОсОО"))
        self.assertEqual(self.O.post("/api/v1/leads/", {"name": "П", "phone": "1", "source": "phone"},
                                     format="json").status_code, 400)


class NbsLeadRateTests(Base):
    def setUp(self):
        super().setUp()
        make_contract(self.fr, product=self.market, scheme="cash")
        make_rule(name="Standard 20%", product=self.market, sale_kind="subscription", partner_tier="standard",
                  percent=20, basis="OZOK §2.1")
        make_rule(name="Growth 30%", product=self.market, sale_kind="subscription", partner_tier="growth",
                  percent=30, basis="OZOK §2.1")
        make_rule(name="Лид OZOK: абонплата 15%", product=self.market, sale_kind="subscription",
                  lead_source="nbs_ads", percent=15, basis="Решение OZOK №1")
        make_rule(name="Установка 100%", sale_kind="setup", percent=100)
        make_rule(name="Лид OZOK: установка — доля франчайзи 70%", sale_kind="setup", lead_source="nbs_ads",
                  percent=70, basis="Решение OZOK №1")
        self.lead = create_lead(franchisee=self.fr, name="Л", phone="1", source="nbs_ads", interest="program")
        self.client = make_client(self.fr)
        self.client.lead = self.lead
        self.client.save()

    def test_two_step_subscription_rate(self):
        sale, _ = create_client_sale(client=self.client, product=self.market, kind="subscription", amount=3000)
        self.assertEqual(sale.franchisee_income, Decimal("450.00"))  # 15% вместо 20%
        self.assertEqual(sale.commission_rule.name, "Standard 20%")  # шаг 1 хранится
        self.assertEqual(sale.adjustment_rule.name, "Лид OZOK: абонплата 15%")
        self.assertEqual(sale.adjustment_amount, Decimal("-150.00"))
        self.assertEqual((sale.lead_id, sale.lead_source), (self.lead.id, "nbs_ads"))

    def test_growth_partner_nbs_lead_still_15(self):
        self.A.post("/api/v1/partner-status-history/", {"partner": self.fr.id, "field": "tier", "new": "growth",
                                                         "effective_from": str(days_ago(5)), "reason": "g"},
                    format="json")
        sale, _ = create_client_sale(client=self.client, product=self.market, kind="renewal", amount=3000)
        self.assertEqual((sale.franchisee_income, sale.adjustment_amount), (Decimal("450.00"), Decimal("-450.00")))

    def test_non_nbs_lead_uses_base_rate(self):
        sale, _ = create_client_sale(client=make_client(self.fr), product=self.market, kind="subscription",
                                     amount=3000)
        self.assertEqual((sale.franchisee_income, sale.adjustment_rule, sale.adjustment_amount),
                         (Decimal("600.00"), None, Decimal("0.00")))

    def test_setup_nbs_share_charge_and_refund(self):
        sale, _ = create_client_sale(client=self.client, product=self.market, kind="setup", amount=10000)
        self.assertEqual((sale.franchisee_income, sale.nbs_share_amount), (Decimal("10000.00"), Decimal("3000.00")))
        ch = Charge.objects.get(client_sale=sale, kind="nbs_lead_share")
        self.assertEqual((ch.amount, ch.due_date), (Decimal("3000.00"), sale.date + timedelta(days=30)))
        self.assertEqual(ch.franchisee_id, self.fr.id)
        data = self.O.get(f"/api/v1/client-sales/{sale.id}/").json()
        self.assertEqual((data["nbs_share_amount"], data["net_income"]), ("3000.00", "7000.00"))
        refund_sale(sale, 10000)
        self.assertEqual(sum(c.amount for c in Charge.objects.filter(kind="nbs_lead_share")), 0)

    def test_rule_scopes_validation_and_preview(self):
        r = self.A.post("/api/v1/commission-rules/", {"name": "x", "lead_source": "nbs_ads", "percent": "15",
                                                      "sale_kind": "subscription"}, format="json")
        self.assertEqual(r.status_code, 400)  # основание обязательно
        r = self.A.post("/api/v1/commission-rules/", {"name": "x", "lead_source": "bogus", "basis": "b",
                                                      "percent": "15"}, format="json")
        self.assertEqual(r.status_code, 400)
        p = self.A.get("/api/v1/commission-rules/preview/", {
            "franchisee": self.fr.id, "product": self.market.id, "sale_kind": "subscription", "amount": "3000",
            "lead_source": "nbs_ads"}).json()
        self.assertEqual((p["income"], p["base_income"], p["adjustment_amount"], p["partner_tier"]),
                         ("450.00", "600.00", "-150.00", "standard"))

    def test_personal_tier_rule_stops_after_downgrade(self):
        make_rule(name="Юг 30% (growth)", franchisee=self.fr, product=self.market, sale_kind="subscription",
                  partner_tier="growth", percent=30, basis="Индивидуальные условия")
        sale, _ = create_client_sale(client=make_client(self.fr), product=self.market, kind="subscription",
                                     amount=1000)
        self.assertEqual(sale.franchisee_income, Decimal("200.00"))  # партнёр Standard — 30% не действует
