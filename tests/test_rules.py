"""Заявки, SLA, просрочки, обучение, задачи, проверки, рейтинг (раздел 5)."""
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from clients.services import create_client_sale
from core.models import month_first, today
from core.testing import api, days_ago, make_client, make_fr, make_product, make_user
from leads.models import Lead
from leads.services import create_lead, pick_franchisee
from planning.models import SalesPlan
from planning.services import compute_rating, plan_percent, rating_table
from quality.models import Audit, Ticket
from quality.services import audit_score, change_status, create_ticket, rate_ticket
from tasks.models import Task
from training.models import Course, Enrollment
from training.services import complete, enroll, trained_stats

D = Decimal


class LeadAssignmentTests(TestCase):
    def setUp(self):
        cache.clear()
        self.osh = make_fr("Ош-1", "Ош", ["Ош", "Узген"])

    def lead(self, city="Ош", interest="program", **kw):
        return create_lead(name="Иван", phone="0555 111", city=city, interest=interest, **kw)

    def test_city_match_case_insensitive(self):
        lead = self.lead(" ОШ ")
        self.assertEqual((lead.franchisee_id, lead.status), (self.osh.id, "new"))
        self.assertIsNotNone(lead.assigned_at)
        self.assertEqual(self.lead("узген").franchisee_id, self.osh.id)

    def test_least_loaded_wins_and_tie_goes_to_lower_id(self):
        second = make_fr("Ош-2", "Ош", ["Ош"])
        first = self.lead()  # поровну -> меньший id
        self.assertEqual(first.franchisee_id, self.osh.id)
        self.assertEqual(self.lead().franchisee_id, second.id)  # у первого уже 1 открытая
        self.assertEqual(self.lead().franchisee_id, self.osh.id)  # снова поровну
        for lead in Lead.objects.filter(franchisee=second):
            lead.status = "lost"
            lead.save()  # закрытые не считаются нагрузкой
        self.assertEqual(self.lead().franchisee_id, second.id)

    def test_franchise_interest_stays_at_hq(self):
        lead = self.lead(interest="franchise")
        self.assertEqual((lead.franchisee_id, lead.status, lead.assigned_at), (None, "new", None))

    def test_no_match_and_inactive_stay_at_hq(self):
        self.assertIsNone(self.lead("Талас").franchisee_id)
        make_fr("Талас", "Талас", status="trainee")
        self.assertIsNone(self.lead("Талас").franchisee_id)
        self.assertIsNone(self.lead("").franchisee_id)
        self.assertIsNone(pick_franchisee(None))

    def test_public_endpoint_without_auth(self):
        c = api()
        r = c.post("/api/v1/leads/public/", {"name": "Айбек", "phone": "0700 1", "city": "Ош",
                                             "interest": "program", "product": "help"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        lead = Lead.objects.get(pk=r.json()["id"])
        self.assertEqual((lead.source, lead.franchisee_id), ("site", self.osh.id))
        self.assertEqual(set(r.json()), {"id"})
        self.assertEqual(c.post("/api/v1/leads/public/", {"name": "x"}, format="json").status_code, 400)
        self.assertEqual(c.post("/api/v1/leads/public/", {"name": "x", "phone": "1", "interest": "program",
                                                          "product": "несуществует"}, format="json").status_code, 400)

    def test_public_endpoint_throttled_10_per_minute(self):
        c = api()
        body = {"name": "Айбек", "phone": "0700 1", "interest": "consult"}
        codes = [c.post("/api/v1/leads/public/", body, format="json").status_code for _ in range(11)]
        self.assertEqual(codes[:10], [201] * 10)
        self.assertEqual(codes[10], 429)

    def test_overdue_flag_and_filter(self):
        old = self.lead()
        Lead.objects.filter(pk=old.pk).update(assigned_at=timezone.now() - timedelta(hours=25))
        fresh = self.lead()
        Lead.objects.filter(pk=fresh.pk).update(assigned_at=timezone.now() - timedelta(hours=23))
        taken = self.lead()
        Lead.objects.filter(pk=taken.pk).update(assigned_at=timezone.now() - timedelta(hours=40), status="in_progress")
        owner = api(make_user(self.osh, "franchisee"))
        rows = {r["id"]: r for r in owner.get("/api/v1/leads/").json()["results"]}
        self.assertEqual([rows[old.id]["overdue"], rows[fresh.id]["overdue"], rows[taken.id]["overdue"]],
                         [True, False, False])
        ids = [r["id"] for r in owner.get("/api/v1/leads/", {"overdue": "true"}).json()["results"]]
        self.assertEqual(ids, [old.id])

    def test_unassigned_filter(self):
        stuck = self.lead("Талас")
        self.lead()
        admin = api(make_user(role="super_admin"))
        ids = [r["id"] for r in admin.get("/api/v1/leads/", {"unassigned": "true"}).json()["results"]]
        self.assertEqual(ids, [stuck.id])

    def test_funnel_actions(self):
        owner = api(make_user(self.osh, "franchisee"))
        lead = self.lead()
        url = f"/api/v1/leads/{lead.id}"
        self.assertEqual(owner.post(f"{url}/demo/").status_code, 400)  # сначала take
        self.assertEqual(owner.post(f"{url}/win/", {}, format="json").status_code, 400)
        self.assertEqual(owner.post(f"{url}/take/").json()["status"], "in_progress")
        self.assertEqual(owner.post(f"{url}/take/").status_code, 400)
        self.assertEqual(owner.post(f"{url}/demo/").json()["status"], "demo")
        r = owner.post(f"{url}/win/", {"business_name": "Магазин «Алтын»", "business_type": "shop"}, format="json")
        self.assertEqual((r.status_code, r.json()["status"]), (200, "paid"))
        client = owner.get(f"/api/v1/clients/{r.json()['client_id']}/").json()
        self.assertEqual((client["business_name"], client["business_type"], client["franchisee"]),
                         ("Магазин «Алтын»", "shop", self.osh.id))
        self.assertEqual(owner.post(f"{url}/lose/", {"lost_reason": "x"}, format="json").status_code, 400)

    def test_lose_requires_reason(self):
        owner = api(make_user(self.osh, "franchisee"))
        lead = self.lead()
        self.assertEqual(owner.post(f"/api/v1/leads/{lead.id}/lose/", {}, format="json").status_code, 400)
        r = owner.post(f"/api/v1/leads/{lead.id}/lose/", {"lost_reason": "Дорого"}, format="json")
        self.assertEqual((r.json()["status"], r.json()["lost_reason"]), ("lost", "Дорого"))

    def test_assign_by_center_only_and_franchisee_edits_comment_only(self):
        other = make_fr("Бишкек", "Бишкек")
        lead = self.lead("Талас")
        admin = api(make_user(role="network_head"))
        r = admin.post(f"/api/v1/leads/{lead.id}/assign/", {"franchisee": other.id}, format="json")
        self.assertEqual((r.status_code, r.json()["status"], r.json()["franchisee"]), (200, "new", other.id))
        owner = api(make_user(other, "franchisee"))
        self.assertEqual(owner.post(f"/api/v1/leads/{lead.id}/assign/", {"franchisee": other.id},
                                    format="json").status_code, 403)
        self.assertEqual(owner.patch(f"/api/v1/leads/{lead.id}/", {"comment": "звонил"}, format="json").status_code, 200)
        self.assertEqual(owner.patch(f"/api/v1/leads/{lead.id}/", {"source": "partner_ads"}, format="json").status_code, 400)
        self.assertEqual(owner.delete(f"/api/v1/leads/{lead.id}/").status_code, 403)


class TicketSLATests(TestCase):
    def setUp(self):
        self.fr = make_fr()
        self.client_ = make_client(self.fr)
        self.now = timezone.now()

    def ticket(self, priority, hours_ago, **kw):
        return create_ticket(franchisee=self.fr, client=self.client_, subject="Т", priority=priority,
                             now=self.now - timedelta(hours=hours_ago), **kw)

    def test_sla_hours_per_priority(self):
        for prio, hours in (("critical", 1), ("high", 4), ("normal", 24), ("low", 72)):
            self.assertEqual(self.ticket(prio, 0).sla_hours, hours)

    def test_breach_without_response_uses_now(self):
        self.assertTrue(self.ticket("critical", 2).is_sla_breached(self.now))
        self.assertFalse(self.ticket("critical", 0.5).is_sla_breached(self.now))
        self.assertTrue(self.ticket("normal", 25).sla_breached)
        self.assertFalse(self.ticket("low", 71).sla_breached)

    def test_breach_with_response_time(self):
        t = self.ticket("high", 10)
        change_status(t, "in_progress", now=t.opened_at + timedelta(hours=3))
        self.assertFalse(t.sla_breached)  # ответили за 3 ч при норме 4 ч, «сейчас» уже не важно
        t2 = self.ticket("high", 10)
        change_status(t2, "in_progress", now=t2.opened_at + timedelta(hours=4, minutes=1))
        self.assertTrue(t2.sla_breached)
        t3 = self.ticket("normal", 30)
        change_status(t3, "in_progress", now=t3.opened_at + timedelta(hours=24))
        self.assertFalse(t3.sla_breached)  # ровно по нормативу — не нарушение

    def test_status_flow_and_rating_rules(self):
        t = self.ticket("normal", 5)
        with self.assertRaises(Exception):
            rate_ticket(t, 5)  # оценка только у resolved/closed
        first = t.opened_at + timedelta(hours=1)
        change_status(t, "in_progress", now=first)
        change_status(t, "open", now=first + timedelta(hours=1))
        change_status(t, "in_progress", now=first + timedelta(hours=2))
        t.refresh_from_db()
        self.assertEqual(t.first_response_at, first)  # проставляется один раз
        change_status(t, "resolved", now=first + timedelta(hours=3))
        self.assertIsNotNone(t.resolved_at)
        rate_ticket(t, 4)
        with self.assertRaises(Exception):
            rate_ticket(t, 6)
        change_status(t, "closed")
        with self.assertRaises(Exception):
            change_status(t, "open")

    def test_api_filter_and_actions(self):
        late = self.ticket("critical", 5)
        ok = self.ticket("low", 5)
        owner = api(make_user(self.fr, "franchisee"))
        rows = {r["id"]: r for r in owner.get("/api/v1/tickets/").json()["results"]}
        self.assertEqual((rows[late.id]["sla_breached"], rows[late.id]["sla_hours"], rows[ok.id]["sla_breached"]),
                         (True, 1, False))
        ids = [r["id"] for r in owner.get("/api/v1/tickets/", {"sla_breached": "true"}).json()["results"]]
        self.assertEqual(ids, [late.id])
        self.assertEqual(owner.post(f"/api/v1/tickets/{ok.id}/rate/", {"rating": 5}, format="json").status_code, 400)
        r = owner.post(f"/api/v1/tickets/{ok.id}/status/", {"status": "resolved"}, format="json")
        self.assertEqual(r.json()["status"], "resolved")
        self.assertEqual(owner.post(f"/api/v1/tickets/{ok.id}/rate/", {"rating": 5}, format="json").json()["rating"], 5)
        self.assertEqual(owner.post(f"/api/v1/tickets/{ok.id}/status/", {"status": "bogus"}, format="json").status_code, 400)

    def test_ticket_create_via_api_by_franchisee(self):
        owner = api(make_user(self.fr, "franchisee"))
        r = owner.post("/api/v1/tickets/", {"client": self.client_.id, "subject": "Не работает касса",
                                            "description": "Ошибка", "priority": "high"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.json()["status"], r.json()["franchisee"], r.json()["sla_hours"]), ("open", self.fr.id, 4))


class OverdueTests(TestCase):
    def test_tasks_overdue(self):
        fr = make_fr()
        hq = make_user(role="network_head")
        owner = make_user(fr, "franchisee")
        mk = lambda title, due, status="new": Task.objects.create(  # noqa: E731
            franchisee=fr, title=title, due_date=due, status=status, created_by=hq)
        late, ontime, done, nodue = (mk("a", days_ago(1)), mk("b", days_ago(-1)), mk("c", days_ago(5), "done"),
                                     mk("d", None))
        rejected = mk("e", days_ago(3), "rejected")
        self.assertEqual([t.overdue for t in (late, ontime, done, nodue, rejected)],
                         [True, False, False, False, False])
        rows = api(owner).get("/api/v1/tasks/", {"overdue": "true"}).json()["results"]
        self.assertEqual([r["id"] for r in rows], [late.id])
        self.assertTrue(rows[0]["overdue"])
        self.assertIn("comments_count", rows[0])

    def test_task_workflow_and_comments(self):
        fr = make_fr()
        hq = api(make_user(role="network_head"))
        owner_user = make_user(fr, "franchisee")
        owner = api(owner_user)
        r = hq.post("/api/v1/tasks/", {"franchisee": fr.id, "title": "Отчёт", "due_date": str(days_ago(-3)),
                                       "assignee": owner_user.id, "priority": "high"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        tid = r.json()["id"]
        self.assertEqual(owner.post("/api/v1/tasks/", {"title": "x"}, format="json").status_code, 403)
        self.assertEqual(owner.patch(f"/api/v1/tasks/{tid}/", {"title": "y"}, format="json").status_code, 403)
        self.assertEqual(hq.post(f"/api/v1/tasks/{tid}/start/").status_code, 403)  # выполняет франчайзи
        self.assertEqual(owner.post(f"/api/v1/tasks/{tid}/start/").json()["status"], "in_progress")
        self.assertEqual(owner.post(f"/api/v1/tasks/{tid}/reject/", {}, format="json").status_code, 400)
        r = owner.post(f"/api/v1/tasks/{tid}/done/", {"comment": "Готово"}, format="json")
        self.assertEqual(r.json()["status"], "done")
        self.assertIsNotNone(r.json()["done_at"])
        self.assertEqual(r.json()["comments_count"], 1)
        self.assertEqual(owner.post(f"/api/v1/tasks/{tid}/start/").status_code, 400)
        c = hq.post(f"/api/v1/tasks/{tid}/comments/", {"text": "Принято"}, format="json")
        self.assertEqual(c.status_code, 201)
        self.assertEqual(c.json()["author_name"], hq.handler._force_user.full_name)
        rows = owner.get(f"/api/v1/tasks/{tid}/comments/").json()
        self.assertEqual((rows["count"], [x["text"] for x in rows["results"]]), (2, ["Готово", "Принято"]))
        # чужой франчайзи не видит комментарии
        stranger = api(make_user(make_fr("Другой", "Бишкек"), "franchisee"))
        self.assertEqual(stranger.get(f"/api/v1/tasks/{tid}/comments/").status_code, 404)
        # отмена: центр; отклонить с комментарием: франчайзи
        t2 = Task.objects.create(franchisee=fr, title="2", created_by=make_user(role="super_admin"))
        self.assertEqual(owner.post(f"/api/v1/tasks/{t2.id}/cancel/").status_code, 403)
        self.assertEqual(hq.post(f"/api/v1/tasks/{t2.id}/cancel/").json()["status"], "cancelled")
        t3 = Task.objects.create(franchisee=fr, title="3", created_by=make_user(role="super_admin"))
        self.assertEqual(owner.post(f"/api/v1/tasks/{t3.id}/reject/", {"comment": "Нет времени"},
                                    format="json").json()["status"], "rejected")


class TrainingTests(TestCase):
    def setUp(self):
        self.fr = make_fr()
        self.owner = make_user(self.fr, "franchisee")
        self.manager = make_user(self.fr, "franchisee_staff")
        self.course = Course.objects.create(title="Стандарты", pass_score=80, validity_months=12)

    def test_pass_fail_and_expiry(self):
        e = enroll(self.course, self.owner)
        e = complete(e, 79)
        self.assertEqual((e.status, e.expires_at), ("failed", None))
        e = enroll(self.course, self.owner)  # повторное назначение после провала
        self.assertEqual((e.status, e.score), ("assigned", None))
        e = complete(e, 80)
        self.assertEqual(e.status, "passed")
        self.assertEqual(e.expires_at.year, today().year + 1)
        self.assertFalse(e.expired)
        with self.assertRaises(Exception):
            enroll(self.course, self.owner)  # действующий сертификат
        e.expires_at = days_ago(1)
        e.save()
        self.assertTrue(e.expired)
        self.assertEqual(enroll(self.course, self.owner).status, "assigned")  # истёк — можно снова

    def test_trained_share(self):
        self.assertEqual(trained_stats([self.fr.id])[self.fr.id], (0, 2))
        complete(enroll(self.course, self.owner), 90)
        self.assertEqual(trained_stats([self.fr.id])[self.fr.id], (1, 2))
        Enrollment.objects.update(expires_at=days_ago(1))
        self.assertEqual(trained_stats([self.fr.id])[self.fr.id], (0, 2))
        self.manager.is_active = False
        self.manager.save()
        self.assertEqual(trained_stats([self.fr.id])[self.fr.id][1], 1)  # считаются только активные сотрудники

    def test_api_flow_and_expired_filter(self):
        hq = api(make_user(role="network_head"))
        r = hq.post("/api/v1/enrollments/", {"course": self.course.id, "user": self.manager.id}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        eid = r.json()["id"]
        self.assertEqual(hq.post("/api/v1/enrollments/", {"course": self.course.id, "user": self.manager.id},
                                 format="json").status_code, 400)
        mgr = api(self.manager)
        self.assertEqual(mgr.post(f"/api/v1/enrollments/{eid}/start/").json()["status"], "in_progress")
        self.assertEqual(mgr.post(f"/api/v1/enrollments/{eid}/complete/", {"score": 90}, format="json").status_code, 403)
        self.assertEqual(hq.post(f"/api/v1/enrollments/{eid}/complete/", {"score": 101}, format="json").status_code, 400)
        r = hq.post(f"/api/v1/enrollments/{eid}/complete/", {"score": 85}, format="json")
        self.assertEqual((r.json()["status"], r.json()["expired"]), ("passed", False))
        Enrollment.objects.filter(pk=eid).update(expires_at=days_ago(2))
        rows = hq.get("/api/v1/enrollments/", {"expired": "true"}).json()["results"]
        self.assertEqual([x["id"] for x in rows], [eid])
        self.assertTrue(rows[0]["expired"])
        owner = api(self.owner)
        r = owner.post("/api/v1/enrollments/", {"course": self.course.id, "user": self.owner.id}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(mgr.post("/api/v1/enrollments/", {"course": self.course.id, "user": self.manager.id},
                                  format="json").status_code, 403)


class AuditTests(TestCase):
    def test_score(self):
        self.assertEqual(audit_score([{"ok": True}] * 4 + [{"ok": False}] * 2), 67)
        self.assertEqual(audit_score([{"ok": True}, {"ok": False}] * 4), 50)
        self.assertEqual(audit_score([{"ok": True}] + [{"ok": False}] * 7), 13)  # 12.5 -> half-up
        self.assertEqual(audit_score([]), 0)

    def test_api(self):
        fr = make_fr()
        curator = make_user(role="network_head")
        hq = api(curator)
        items = [{"criterion": "Вывеска", "ok": True}, {"criterion": "Демо", "ok": False},
                 {"criterion": "Обучение", "ok": True}]
        r = hq.post("/api/v1/audits/", {"franchisee": fr.id, "items": items, "summary": "ок"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.json()["score"], r.json()["inspector"]), (67, curator.id))
        self.assertEqual(hq.post("/api/v1/audits/", {"franchisee": fr.id, "items": []}, format="json").status_code, 400)
        owner = api(make_user(fr, "franchisee"))
        self.assertEqual(owner.get("/api/v1/audits/").json()["count"], 1)
        self.assertEqual(owner.post("/api/v1/audits/", {"items": items}, format="json").status_code, 403)


class RatingTests(TestCase):
    def test_plan_percent_formula(self):
        self.assertEqual(plan_percent(4, D("100000"), 3, D("90000")), D("0.825"))
        self.assertEqual(plan_percent(4, D("100000"), 8, D("500000")), D("1.2"))  # каждая часть ≤ 1.2
        self.assertEqual(plan_percent(5, D("0"), 2, D("1")), D("0.4"))
        self.assertIsNone(plan_percent(0, D("0"), 3, D("10")))

    def test_compute_rating_components(self):
        r, discipline = compute_rating(D("0.825"), 4.0, 80, D("0.5"), D("0"), D("0"))
        self.assertEqual((r, discipline), (79, 1))
        r, discipline = compute_rating(None, None, None, D("0"), D("0"), D("0"))  # нет плана/оценок/проверок
        self.assertEqual(r, 37)  # 20*0.8 + 15*0.7 + 10 = 36.5 -> half-up 37
        # просрочка 40% от взносов -> дисциплина 0.6
        _, d = compute_rating(D("1"), 5.0, 100, D("1"), D("4000"), D("10000"))
        self.assertEqual(d, D("0.6"))
        self.assertEqual(compute_rating(D("2"), 5.0, 100, D("1"), D("20000"), D("10000"))[1], 0)

    def test_rating_on_hand_computed_example(self):
        """План 0.825·40=33; оценки 4/5·20=16; проверка 0.8·15=12; обучено 1/2·15=7.5; дисциплина 1·10=10
        -> 78.5 -> 79."""
        fr = make_fr()
        owner = make_user(fr, "franchisee")
        make_user(fr, "franchisee_staff")
        month = month_first(today())
        SalesPlan.objects.create(franchisee=fr, month=month, target_clients=4, target_revenue=D("100000"))
        clients = [make_client(fr) for _ in range(3)]  # 3 новых клиента в этом месяце
        product = make_product("market")
        create_client_sale(client=clients[0], product=product, kind="setup", amount=D("90000"))
        t = create_ticket(franchisee=fr, client=clients[0], subject="Т")
        change_status(t, "resolved")
        rate_ticket(t, 4)
        Audit.objects.create(franchisee=fr, items=[], score=80)
        complete(enroll(Course.objects.create(title="К"), owner), 95)
        row = rating_table(month)[0]
        self.assertEqual(
            (row["rating"], row["place"], row["plan_percent"], row["avg_ticket_rating"], row["last_audit_score"],
             row["trained_share"], row["payment_discipline"], row["new_clients"], row["revenue"]),
            (79, 1, 0.83, 4.0, 80, 0.5, 1.0, 3, "90000.00"))
        # отчёт и места по убыванию рейтинга
        other = make_fr("Слабый", "Бишкек")
        make_user(other, "franchisee")
        rows = api(make_user(role="super_admin")).get("/api/v1/reports/rating/", {"month": month.strftime("%Y-%m")}).json()
        self.assertEqual([r["franchisee_id"] for r in rows], [fr.id, other.id])
        self.assertEqual([r["place"] for r in rows], [1, 2])
        self.assertEqual(set(rows[0]), {"franchisee_id", "name", "city", "rating", "place", "plan_percent",
                                        "avg_ticket_rating", "last_audit_score", "trained_share",
                                        "payment_discipline", "new_clients", "revenue"})

    def test_only_active_franchisees_rated(self):
        make_fr("Новый", "Нарын", status="trainee")
        make_fr("Приостановлен", "Баткен", status="suspended")
        self.assertEqual(rating_table(month_first(today())), [])

    def test_sales_plan_api_with_facts(self):
        fr = make_fr()
        hq = api(make_user(role="network_head"))
        r = hq.post("/api/v1/sales-plans/", {"franchisee": fr.id, "month": today().strftime("%Y-%m"),
                                             "target_clients": 2, "target_revenue": "10000"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["month"], month_first(today()).isoformat())
        create_client_sale(client=make_client(fr), product=make_product("market"), kind="setup", amount=5000)
        row = hq.get("/api/v1/sales-plans/", {"month": today().strftime("%Y-%m")}).json()["results"][0]
        self.assertEqual((row["fact_clients"], row["fact_revenue"], row["plan_percent"]), (1, "5000.00", 0.5))
        self.assertEqual(hq.post("/api/v1/sales-plans/", {"franchisee": fr.id, "month": today().strftime("%Y-%m"),
                                                          "target_clients": 1}, format="json").status_code, 400)
        owner = api(make_user(fr, "franchisee"))
        self.assertEqual(owner.get("/api/v1/sales-plans/").json()["count"], 1)
        self.assertEqual(owner.post("/api/v1/sales-plans/", {"month": "2026-01"}, format="json").status_code, 403)


