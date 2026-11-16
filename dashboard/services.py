"""Дашборды (раздел 6.3): центр и франчайзи."""
from django.utils.translation import gettext
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

from billing.models import FranchiseePayment
from billing.services import finance_map
from clients.models import Client, ClientProductAccount, ClientSale
from clients.reports import sales_by_product
from core.models import add_months, day_bounds, money, month_first, month_label, month_last, today
from integrations.models import IntegrationEvent
from leads.models import Lead, overdue_hours
from network.models import Franchisee
from planning.services import plan_summary, rating_map, rating_table
from quality.models import Ticket
from tasks.models import Task
from training.models import Enrollment
from training.services import trained_share, trained_stats

ZERO = Decimal("0")
LEAD_STATUSES = ("new", "in_progress", "demo", "proposal", "contract", "paid", "lost")


def _fmt(amount):
    return f"{int(Decimal(amount)):,}".replace(",", " ")


def _overdue_leads(qs):
    return qs.filter(status=Lead.Status.NEW, franchisee__isnull=False,
                     assigned_at__lt=timezone.now() - timedelta(hours=overdue_hours()))


def _overdue_tasks(qs):
    return qs.filter(due_date__lt=today(), status__in=Task.ACTIVE_STATUSES)


def _breached_open_tickets(qs):
    return [t for t in qs.filter(status__in=("open", "in_progress")) if t.sla_breached]


def _lead_counts(qs):
    counts = {s: 0 for s in LEAD_STATUSES}
    for r in qs.values("status").annotate(n=Count("id")):
        counts[r["status"]] = r["n"]
    counts["assigned"] = qs.filter(status="new", franchisee__isnull=False).count()  # совместимость со старым API
    counts["won"] = counts["paid"]
    return counts


def _revenue_by_month(first, franchisee_id=None):
    start_month = add_months(first, -5)
    qs = ClientSale.objects.filter(date__gte=start_month, date__lte=month_last(first))
    if franchisee_id:
        qs = qs.filter(franchisee_id=franchisee_id)
    totals = {}
    for d, amount in qs.values_list("date", "amount"):
        totals[month_label(d)] = totals.get(month_label(d), ZERO) + amount
    return [{"month": month_label(add_months(start_month, i)),
             "revenue": money(totals.get(month_label(add_months(start_month, i))))} for i in range(6)]


def _subscriptions(franchisee_id=None):
    on = today()
    qs = ClientProductAccount.objects.filter(status__in=ClientProductAccount.LIVE_STATUSES)
    if franchisee_id:
        qs = qs.filter(client__franchisee_id=franchisee_id)
    by_status = {r["status"]: r["n"] for r in qs.values("status").annotate(n=Count("id"))}
    return {"by_status": by_status, "active": qs.filter(status="active").count(),
            "renewal_soon": qs.filter(paid_until__gte=on, paid_until__lte=on + timedelta(days=14)).count(),
            "expired": qs.filter(paid_until__lt=on).count()}


def _attention(fin, limit=10):
    """До 10 пунктов; типы чередуются, внутри типа — по тяжести."""
    names = dict(Franchisee.objects.values_list("id", "name"))
    groups = {"overdue_payment": [], "sla": [], "overdue_task": [], "overdue_lead": [], "expired_cert": []}
    for fid, f in fin.items():
        if f["overdue"] > 0 and fid in names:
            groups["overdue_payment"].append((f["overdue"], fid, gettext('Просрочка {v} сом').format(v=_fmt(f['overdue']))))
    sla = {}
    for t in _breached_open_tickets(Ticket.objects.all()):
        sla[t.franchisee_id] = sla.get(t.franchisee_id, 0) + 1
    groups["sla"] = [(n, fid, gettext('Нарушен SLA по обращениям: {n}').format(n=n)) for fid, n in sla.items()]
    for key, qs, label in (("overdue_task", _overdue_tasks(Task.objects.all()), gettext("Просрочено задач")),
                           ("overdue_lead", _overdue_leads(Lead.objects.filter(franchisee__isnull=False)),
                            gettext("Заявки без реакции больше 24 часов"))):
        for r in qs.values("franchisee_id").annotate(n=Count("id")):
            groups[key].append((r["n"], r["franchisee_id"], f"{label}: {r['n']}"))
    expired = {}
    for fid, uid in Enrollment.objects.filter(status="passed", expires_at__lt=today(), user__is_active=True) \
            .values_list("franchisee_id", "user_id").distinct():
        expired.setdefault(fid, set()).add(uid)
    groups["expired_cert"] = [(len(u), fid, gettext('Истёк сертификат у сотрудников: {v}').format(v=len(u)))
                              for fid, u in expired.items()]
    order = list(groups)
    queues = [sorted(groups[k], key=lambda x: -x[0]) for k in order]
    result = []
    while len(result) < limit and any(queues):
        for k, q in zip(order, queues):
            if q and len(result) < limit:
                _, fid, text = q.pop(0)
                result.append({"type": k, "franchisee_id": fid, "franchisee_name": names.get(fid, ""),
                               "text": text})
    return result


def _payment_money(first, last, partner_id=None):
    """Деньги по оплатам абонплаты и комиссиям за месяц (P9/P8): оплаты — Payment, комиссии — начисления."""
    from billing.models import CommissionAccrual, Payment
    pays = Payment.objects.filter(status="confirmed", paid_at__date__gte=first, paid_at__date__lte=last)
    accs = CommissionAccrual.objects.filter(date__gte=first, date__lte=last).exclude(status="on_hold")
    if partner_id:
        from clients.branches import served_by_prefixed
        pays = pays.filter(served_by_prefixed("branch__", partner_id) | Q(accruals__partner_id=partner_id)).distinct()
        accs = accs.filter(partner_id=partner_id)
    from billing.services import paid_commissions  # единый источник «выплачено» (D1)
    on_hold = CommissionAccrual.objects.filter(status="on_hold")
    if partner_id:
        on_hold = on_hold.filter(partner_id=partner_id)
    return {
        "payments_collected": money(sum((p.amount - p.refunded_amount for p in pays), ZERO)),
        "commissions_accrued": money(accs.aggregate(s=Sum("amount"))["s"]),
        "commissions_withheld": money(accs.aggregate(s=Sum("withheld_amount"))["s"]),
        "commissions_paid": money(paid_commissions(first, last, partner_id)),
        "commissions_on_hold": money(on_hold.aggregate(s=Sum("amount"))["s"]),
    }


def hq_dashboard(month):
    first = month_first(month)
    last = month_last(first)
    start, end = day_bounds(first, last)
    fin = finance_map(None)
    fr_status = {s: 0 for s in ("candidate", "trainee", "manager", "franchisee", "suspended", "closed")}
    for r in Franchisee.objects.values("status").annotate(n=Count("id")):
        if r["status"] in fr_status:
            fr_status[r["status"]] = r["n"]
    leads = _lead_counts(Lead.objects.all())
    leads["overdue"] = _overdue_leads(Lead.objects.all()).count()
    leads["unassigned"] = Lead.objects.filter(franchisee__isnull=True, status="new").count()
    month_sales = ClientSale.objects.filter(date__gte=first, date__lte=last)
    active_ids = list(Franchisee.objects.filter(status="franchisee").values_list("id", flat=True))
    stats = trained_stats(active_ids)
    trained = sum(t for t, _ in stats.values())
    total = sum(n for _, n in stats.values())
    payments = FranchiseePayment.objects.filter(date__gte=first, date__lte=last).exclude(method="offset")
    return {
        "scope": "hq",
        "month": month_label(first),
        "franchisees": fr_status,
        "leads": leads,
        "new_clients": Client.objects.filter(created_at__gte=start, created_at__lt=end).count(),
        "network_revenue": money(month_sales.aggregate(s=Sum("amount"))["s"]),
        "commission_month": money(month_sales.aggregate(s=Sum("commission_amount"))["s"]),
        "network_income_month": money(month_sales.aggregate(s=Sum("franchisee_income"))["s"]),
        "withheld_month": money(month_sales.aggregate(s=Sum("withheld_amount"))["s"]),
        "receivables": money(sum((max(f["balance"], ZERO) for f in fin.values()), ZERO)),
        "overdue_receivables": money(sum((f["overdue"] for f in fin.values()), ZERO)),
        "payments_month": money(payments.aggregate(s=Sum("amount"))["s"]),
        "open_tickets": Ticket.objects.filter(status__in=("open", "in_progress")).count(),
        "sla_breached": len(_breached_open_tickets(Ticket.objects.all())),
        "overdue_tasks": _overdue_tasks(Task.objects.all()).count(),
        "trained_share": trained_share(trained, total),
        "subscriptions": _subscriptions(),
        **_payment_money(first, last),
        "unmatched_events": IntegrationEvent.objects.filter(status="unmatched").count(),
        "revenue_by_month": _revenue_by_month(first),
        "sales_by_product": sales_by_product(first, last),
        "rating": [{k: r[k] for k in ("franchisee_id", "name", "city", "rating", "place", "plan_percent")}
                   for r in rating_table(first)],
        "attention": _attention(fin),
    }


def franchisee_dashboard(franchisee, month):
    first = month_first(month)
    last = month_last(first)
    fid = franchisee.id
    fin = finance_map([fid])[fid]
    ratings = rating_map(first)
    r = ratings.get(fid)
    leads_all = Lead.objects.filter(franchisee=franchisee)
    counts = _lead_counts(leads_all)
    leads = {"assigned": counts["assigned"], "in_progress": counts["in_progress"], "demo": counts["demo"],
             "overdue": _overdue_leads(leads_all).count()}
    on = today()
    clients = Client.objects.filter(franchisee=franchisee, is_active=True)
    accounts = ClientProductAccount.objects.filter(client__franchisee=franchisee,
                                                   status__in=ClientProductAccount.LIVE_STATUSES)
    soon = accounts.filter(paid_until__gte=on, paid_until__lte=on + timedelta(days=14)).values("client")
    expired = accounts.filter(paid_until__lt=on).values("client")
    tasks = Task.objects.filter(franchisee=franchisee)
    stats = trained_stats([fid])[fid]
    sales = ClientSale.objects.filter(franchisee=franchisee, date__gte=first, date__lte=last)
    return {
        "scope": "franchisee",
        "month": month_label(first),
        "plan": plan_summary(fid, first),
        "rating": r["rating"] if r else None,
        "place": r["place"] if r else None,
        "network_size": len(ratings),
        "leads": leads,
        "clients_total": clients.count(),
        "renewal_soon": clients.filter(id__in=soon).count(),
        "expired": clients.filter(id__in=expired).count(),
        "balance": money(fin["balance"]),
        "overdue_amount": money(fin["overdue"]),
        "commission_due": money(fin["commission_due"]),
        "commission_month": money(sales.aggregate(s=Sum("commission_amount"))["s"]),
        "income_month": money(sales.aggregate(s=Sum("franchisee_income"))["s"]),
        "withheld_month": money(sales.aggregate(s=Sum("withheld_amount"))["s"]),
        "sales_by_product": sales_by_product(first, last, fid),
        "subscriptions": _subscriptions(fid),
        **_payment_money(first, last, fid),
        "open_tickets": Ticket.objects.filter(franchisee=franchisee, status__in=("open", "in_progress")).count(),
        "sla_breached": len(_breached_open_tickets(Ticket.objects.filter(franchisee=franchisee))),
        "tasks": {"new": tasks.filter(status="new").count(),
                  "in_progress": tasks.filter(status="in_progress").count(),
                  "overdue": _overdue_tasks(tasks).count()},
        "trained_share": trained_share(*stats),
        "revenue_by_month": _revenue_by_month(first, fid),
    }

