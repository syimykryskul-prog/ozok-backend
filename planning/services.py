"""План и рейтинг (раздел 5): факт, plan_percent, рейтинг 0-100 и место в сети."""
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Avg, Count, Sum

from billing.services import finance_map
from clients.models import Client, ClientSale
from core.models import day_bounds, money, month_first, month_last, today as _today
from network.models import Contract, Franchisee
from quality.models import Audit, Ticket
from training.services import trained_stats

from .models import SalesPlan

ZERO = Decimal("0")
PLAN_PART_CAP = Decimal("1.2")


def month_facts(first, franchisee_ids=None):
    """{franchisee_id: (new_clients, revenue)} за месяц."""
    last = month_last(first)
    start, end = day_bounds(first, last)
    cq = Client.objects.filter(created_at__gte=start, created_at__lt=end)
    sq = ClientSale.objects.filter(date__gte=first, date__lte=last)
    if franchisee_ids is not None:
        cq = cq.filter(franchisee_id__in=list(franchisee_ids))
        sq = sq.filter(franchisee_id__in=list(franchisee_ids))
    clients = {r["franchisee_id"]: r["n"] for r in cq.values("franchisee_id").annotate(n=Count("id"))}
    revenue = {r["franchisee_id"]: r["s"] or ZERO for r in sq.values("franchisee_id").annotate(s=Sum("amount"))}
    ids = franchisee_ids if franchisee_ids is not None else set(clients) | set(revenue)
    return {fid: (clients.get(fid, 0), revenue.get(fid, ZERO)) for fid in ids}


def plan_percent(target_clients, target_revenue, fact_clients, fact_revenue):
    """Среднее из (клиенты/план, выручка/план), каждая часть ограничена 1.2. Нет цели — None."""
    parts = []
    if target_clients and target_clients > 0:
        parts.append(min(Decimal(fact_clients) / Decimal(target_clients), PLAN_PART_CAP))
    if target_revenue and Decimal(target_revenue) > 0:
        parts.append(min(Decimal(fact_revenue) / Decimal(target_revenue), PLAN_PART_CAP))
    if not parts:
        return None
    return sum(parts) / len(parts)


def _r2(value):
    return None if value is None else float(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_rating(plan, avg_ticket, audit_score, trained_share, overdue, fees):
    """Рейтинг 0-100: 40% план (≤1) + 20% оценки (нет — 0.8) + 15% проверка (нет — 0.7) + 15% обучение
    + 10% платёжная дисциплина. plan=None считается 0."""
    plan_part = min(plan if plan is not None else ZERO, Decimal(1))
    ticket_part = Decimal(str(avg_ticket)) / 5 if avg_ticket is not None else Decimal("0.8")
    audit_part = Decimal(audit_score) / 100 if audit_score is not None else Decimal("0.7")
    if overdue <= 0:
        discipline = Decimal(1)
    elif fees > 0:
        discipline = max(Decimal(0), 1 - overdue / fees)
    else:
        discipline = Decimal(0)
    total = 100 * (Decimal("0.4") * plan_part + Decimal("0.2") * ticket_part + Decimal("0.15") * audit_part
                   + Decimal("0.15") * Decimal(str(trained_share)) + Decimal("0.1") * discipline)
    return int(total.quantize(Decimal("1"), rounding=ROUND_HALF_UP)), discipline


def rating_table(month, on=None):
    """Таблица рейтинга по всем активным франчайзи за месяц (дата 1-го числа). Отсортирована по месту."""
    today = on or _today()
    first = month_first(month)
    ref = min(today, month_last(first))
    frs = list(Franchisee.objects.filter(status=Franchisee.Status.FRANCHISEE).order_by("id"))
    ids = [f.id for f in frs]
    plans = {p.franchisee_id: p for p in SalesPlan.objects.filter(month=first, franchisee_id__in=ids)}
    facts = month_facts(first, ids)

    start, _ = day_bounds(ref - timedelta(days=89), ref)
    _, end = day_bounds(ref, ref)
    tickets = {r["franchisee_id"]: r["a"] for r in Ticket.objects.filter(
        franchisee_id__in=ids, rating__isnull=False, opened_at__gte=start, opened_at__lt=end)
        .values("franchisee_id").annotate(a=Avg("rating"))}
    audits = {}
    for a in Audit.objects.filter(franchisee_id__in=ids, date__lte=ref).order_by("franchisee_id", "-date", "-id"):
        audits.setdefault(a.franchisee_id, a.score)
    stats = trained_stats(ids, today)
    fin = finance_map(ids, today)
    fees = {r["franchisee_id"]: r["s"] or ZERO for r in Contract.objects.filter(
        franchisee_id__in=ids, status=Contract.Status.ACTIVE).values("franchisee_id").annotate(s=Sum("fee_total"))}

    rows = []
    for f in frs:
        plan = plans.get(f.id)
        nc, rev = facts[f.id]
        pp = plan_percent(plan.target_clients, plan.target_revenue, nc, rev) if plan else None
        avg_t = tickets.get(f.id)
        avg_t = float(avg_t) if avg_t is not None else None
        trained, total = stats[f.id]
        share = (Decimal(trained) / Decimal(total)) if total else ZERO
        rating, discipline = compute_rating(pp, avg_t, audits.get(f.id), share, fin[f.id]["overdue"],
                                            fees.get(f.id, ZERO))
        rows.append({"franchisee_id": f.id, "name": f.name, "city": f.city, "rating": rating,
                     "plan_percent": _r2(pp), "avg_ticket_rating": _r2(avg_t) if avg_t is not None else None,
                     "last_audit_score": audits.get(f.id), "trained_share": _r2(share),
                     "payment_discipline": _r2(discipline), "new_clients": nc, "revenue": money(rev),
                     "_pp": pp or ZERO})
    rows.sort(key=lambda r: (-r["rating"], -r["_pp"], r["name"]))
    for i, r in enumerate(rows, start=1):
        r["place"] = i
        r.pop("_pp")
    keys = ("franchisee_id", "name", "city", "rating", "place", "plan_percent", "avg_ticket_rating",
            "last_audit_score", "trained_share", "payment_discipline", "new_clients", "revenue")
    return [{k: r[k] for k in keys} for r in rows]


def rating_map(month, on=None):
    return {r["franchisee_id"]: r for r in rating_table(month, on)}


def plan_summary(franchisee_id, month):
    """Плановый блок для дашборда/карточки: dict или None, если плана нет."""
    first = month_first(month)
    plan = SalesPlan.objects.filter(franchisee_id=franchisee_id, month=first).first()
    if plan is None:
        return None
    nc, rev = month_facts(first, [franchisee_id])[franchisee_id]
    return {"target_clients": plan.target_clients, "target_revenue": money(plan.target_revenue),
            "fact_clients": nc, "fact_revenue": money(rev),
            "plan_percent": _r2(plan_percent(plan.target_clients, plan.target_revenue, nc, rev))}
