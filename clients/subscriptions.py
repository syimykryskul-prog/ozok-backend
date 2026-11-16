"""Статусы подписок (§10.3): awaiting_payment / active / grace / overdue / restricted / closed."""
from datetime import timedelta

from core.models import add_months, month_first, month_label, q2, today
from core.settings_registry import get_int

from .models import Subscription

S = Subscription.Status


def grace_days(sub):
    for obj in (sub.tariff, sub.product):
        if obj is not None and obj.grace_days is not None:
            return obj.grace_days
    return get_int("subscriptions.grace_days")


def restrict_after_days(sub):
    for obj in (sub.tariff, sub.product):
        if obj is not None and obj.restrict_after_days is not None:
            return obj.restrict_after_days
    return get_int("subscriptions.restrict_after_days")


def status_for(sub, on=None):
    """Статус по paid_until: ≥ сегодня — active; до grace_days — grace; до restrict_after_days — overdue; дальше — restricted."""
    on = on or today()
    if sub.status == S.CLOSED or sub.closed_at:
        return S.CLOSED
    if sub.paid_until is None:
        return S.AWAITING_PAYMENT
    late = (on - sub.paid_until).days
    if late <= 0:
        return S.ACTIVE
    if late <= grace_days(sub):
        return S.GRACE
    if late <= restrict_after_days(sub):
        return S.OVERDUE
    return S.RESTRICTED


def refresh_status(sub, on=None, save=True):
    new = status_for(sub, on)
    if new != sub.status:
        sub.status = new
        if save:
            sub.save(update_fields=["status", "updated_at"])
        return True
    return False


def update_all(on=None):
    """Команда update_subscription_statuses: пересчитать все не закрытые подписки. Возвращает {статус: изменено}."""
    changed = {}
    for sub in Subscription.objects.exclude(status=S.CLOSED).select_related("product", "tariff"):
        old = sub.status
        if refresh_status(sub, on):
            key = f"{old}->{sub.status}"
            changed[key] = changed.get(key, 0) + 1
    return changed


def periods(sub):
    """Помесячный разбор: ожидалось / оплачено / статус (paid|partial|unpaid)."""
    from billing.models import Payment
    from network.commission import price_on
    pays = list(Payment.objects.filter(subscription=sub, status="confirmed").order_by("paid_at", "id"))
    paid = {}
    first = None
    for p in pays:
        start = month_first(p.period_from or p.paid_at.date())
        m = max(p.months, 1)
        per = q2(p.amount / m)
        left = p.amount - p.refunded_amount
        for i in range(m):
            key = add_months(start, i)
            part = per if i < m - 1 else q2(p.amount - per * (m - 1))
            part = min(part, max(left, 0)) if p.refunded_amount else part
            paid[key] = paid.get(key, 0) + part
            first = key if first is None or key < first else first
    start = month_first(sub.started_at or sub.connected_at or today())
    if first is not None and first < start:
        start = first
    last = max([month_first(today())] + list(paid))
    rows, cur = [], start
    while cur <= last and len(rows) < 240:
        expected = (price_on(sub.tariff, cur) if sub.tariff_id else None) or sub.monthly_fee or 0
        got = q2(paid.get(cur, 0))
        expected = q2(expected)
        if got <= 0:
            st = "unpaid"
        elif expected > 0 and got < expected:
            st = "partial"
        else:
            st = "paid"
        rows.append({"period": month_label(cur), "expected": str(expected), "paid": str(got), "status": st})
        cur = add_months(cur, 1)
    return rows
