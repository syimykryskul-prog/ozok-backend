"""Простые дашборды ролей OZOK без общего дашборда: recruiter, regional_support, implementer, quality."""
from decimal import Decimal

from django.db.models import Avg, Count, Q, Sum

from billing.models import RecruiterReward
from clients.models import Client, Implementation, ImplementerAccrual, Subscription
from core.models import money, month_first, month_label, month_last
from network.models import Franchisee
from quality.models import Audit, Ticket

ZERO = Decimal("0")


def recruiter_dashboard(user, month):
    partners = Franchisee.objects.filter(recruited_by=user)
    funnel = {r["funnel_stage"]: r["n"] for r in partners.values("funnel_stage").annotate(n=Count("id"))}
    by_status = {r["status"]: r["n"] for r in partners.values("status").annotate(n=Count("id"))}
    rewards = RecruiterReward.objects.filter(recruiter=user)
    amounts = {st: money(sum((r.accrued_amount if st != "paid" else r.paid_amount
                              for r in rewards.filter(status=st)), ZERO))
               for st in ("accruing", "accrued", "verified", "paid")}
    return {"scope": "recruiter", "month": month_label(month_first(month)), "partners_total": partners.count(),
            "funnel": funnel, "by_status": by_status, "rewards_count": rewards.count(), "rewards_amount": amounts,
            "rewards": [{"id": r.id, "partner": r.partner_id, "partner_name": r.partner.name, "status": r.status,
                         "accrued_amount": money(r.accrued_amount), "target_amount": money(r.target_amount)}
                        for r in rewards.select_related("partner")[:20]]}


def regional_dashboard(user, month):
    region = user.region
    if not region:
        return {"scope": "regional_support", "region": "", "clients_total": 0, "subscriptions": {},
                "installations": {}, "tickets": {}}
    clients = Client.objects.filter(Q(franchisee__region__iexact=region) | Q(branches__region__iexact=region)).distinct()
    subs = Subscription.objects.filter(client__in=clients)
    inst = Implementation.objects.filter(client__in=clients)
    tickets = Ticket.objects.filter(client__in=clients)
    open_t = [t for t in tickets.filter(status__in=("open", "in_progress"))]
    return {"scope": "regional_support", "region": region, "month": month_label(month_first(month)),
            "clients_total": clients.count(),
            "subscriptions": {r["status"]: r["n"] for r in subs.values("status").annotate(n=Count("id"))},
            "installations": {**{r["status"]: r["n"] for r in inst.values("status").annotate(n=Count("id"))},
                              "overdue": sum(1 for i in inst if i.overdue)},
            "tickets": {"open": len(open_t), "sla_breached": sum(1 for t in open_t if t.sla_breached)}}


def implementer_dashboard(user, month):
    inst = Implementation.objects.filter(implementer=user)
    acc = ImplementerAccrual.objects.filter(user=user)
    return {"scope": "implementer", "month": month_label(month_first(month)),
            "installations": {**{r["status"]: r["n"] for r in inst.values("status").annotate(n=Count("id"))},
                              "overdue": sum(1 for i in inst if i.overdue)},
            "accruals": {st: money(acc.filter(status=st).aggregate(s=Sum("amount"))["s"])
                         for st in ("accrued", "verified", "paid")},
            "upcoming": [{"id": i.id, "client": i.client.business_name, "deadline": i.deadline, "status": i.status}
                         for i in inst.exclude(status__in=("done", "cancelled")).select_related("client")
                         .order_by("deadline")[:10]]}


def quality_dashboard(user, month):
    first, last = month_first(month), month_last(month)
    audits = Audit.objects.filter(date__gte=first, date__lte=last)
    tickets = Ticket.objects.filter(opened_at__date__gte=first, opened_at__date__lte=last)
    complaints = tickets.filter(Q(rating__lte=2) | Q(priority__in=("high", "critical")))
    return {"scope": "quality", "month": month_label(first), "audits_count": audits.count(),
            "audits_avg_score": round(audits.aggregate(a=Avg("score"))["a"] or 0, 1),
            "audits_low": audits.filter(score__lt=60).count(),
            "complaints": complaints.count(),
            "tickets_open": Ticket.objects.filter(status__in=("open", "in_progress")).count(),
            "tickets_sla_breached": sum(1 for t in Ticket.objects.filter(status__in=("open", "in_progress"))
                                        if t.sla_breached),
            "low_rated": tickets.filter(rating__lte=2).count()}


BY_ROLE = {"recruiter": recruiter_dashboard, "regional_support": regional_dashboard,
           "implementer": implementer_dashboard, "quality": quality_dashboard}
