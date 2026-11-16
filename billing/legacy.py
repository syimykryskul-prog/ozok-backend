"""Старые ручные выплаты (CommissionPayout) -> погашение начислений FIFO (самые старые первыми).

Единый источник долга по комиссиям — начисления: owed = amount − withheld − paid_amount. Ручная выплата не
создаётся (commission-payouts/ только для чтения), но исторические строки и строки, внесённые через ORM/админку,
погашают начисления через эту функцию (data-миграция 0013 и сигнал post_save)."""
from django.utils.translation import gettext
from decimal import Decimal

from django.db.models import Q

ZERO = Decimal("0.00")


def legacy_total(partner_id, payout_model):
    rows = payout_model.objects.filter(franchisee_id=partner_id, cancelled_at__isnull=True).exclude(method="offset")
    return sum((r.amount for r in rows), ZERO)


def apply_legacy_credit(partner_id, accrual_model, payout_model):
    """Распределить непогашенный остаток ручных выплат партнёра на начисления accrued без выплаты, FIFO.
    Идемпотентно: учитывается уже погашенное (paid_amount). Возвращает нераспределённый остаток."""
    qs = accrual_model.objects.filter(partner_id=partner_id)
    used = sum((a.paid_amount for a in qs), ZERO)
    credit = legacy_total(partner_id, payout_model) - used
    if credit <= 0:
        return ZERO
    open_rows = qs.filter(reversal_of__isnull=True).filter(
        Q(status="accrued", payout__isnull=True) | Q(status="in_payout", payout__status__in=("draft", "verified"))
    ).select_related("payout").order_by("date", "id")
    touched = {}
    for a in open_rows:
        owed = a.amount - a.withheld_amount - a.paid_amount
        if owed <= 0:
            continue
        take = min(owed, credit)
        a.paid_amount += take
        covered = a.paid_amount >= a.amount - a.withheld_amount
        if covered:
            a.status = "paid"
        if a.payout_id:
            touched[a.payout_id] = a.payout
            if covered:
                a.payout = None  # погашена старой выплатой: из открытого пакета уходит
        a.save(update_fields=["paid_amount", "status", "payout"])
        credit -= take
        if credit <= 0:
            break
    for payout in touched.values():  # пересчёт открытых пакетов: сумма = остаток по оставшимся начислениям
        left = list(qs.filter(payout=payout))
        total = sum((x.amount - x.withheld_amount - x.paid_amount for x in left), ZERO)
        if not left or total <= 0:
            for x in left:
                x.status, x.payout = "accrued", None
                x.save(update_fields=["status", "payout"])
            payout.status = "cancelled"
            payout.cancel_reason = gettext("Погашено старыми ручными выплатами")
            payout.cancelled_at = _now()
            payout.save(update_fields=["status", "cancel_reason", "cancelled_at"])
        else:
            payout.amount = total
            payout.save(update_fields=["amount"])
    return max(credit, ZERO)


def _now():
    from django.utils import timezone
    return timezone.now()
