"""Выплаты комиссий (P8): draft → verified → paid; verified_by ≠ paid_by; отрицательный остаток переносится."""
from django.utils.translation import gettext
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.exceptions import BusinessError
from core.models import month_first, month_last, q2, today

from .models import CommissionAccrual, Payout

A = CommissionAccrual.Status
P = Payout.Status
ZERO = Decimal("0.00")


def payable(partner, period):
    """Начисления к выплате за период: статус accrued, ещё не в выплате, дата оплаты ≤ конец периода (в т.ч. сторно)."""
    return CommissionAccrual.objects.filter(partner=partner, status=A.ACCRUED, payout__isnull=True,
                                            date__lte=month_last(period))


def balance(partner, period):
    return q2(sum((a.owed for a in payable(partner, period)), ZERO))


@transaction.atomic
def create(partner, period, user, comment=""):
    """Пакет выплаты = только то, что ещё должны: начисления за вычетом удержаний и погашенного старыми ручными
    выплатами (D1). Старые выплаты, не успевшие погасить начисления, распределяются здесь FIFO."""
    from . import legacy
    from .models import CommissionPayout
    period = month_first(period)
    legacy.apply_legacy_credit(partner.id, CommissionAccrual, CommissionPayout)
    accs = list(payable(partner, period).select_for_update())
    total = q2(sum((a.owed for a in accs), ZERO))
    if not accs:
        raise BusinessError(gettext("Нет начислений к выплате за период."))
    if total <= 0:
        raise BusinessError(gettext("К выплате нет положительной суммы: отрицательный остаток переносится на следующий период."))
    payout = Payout.objects.create(partner=partner, period=period, amount=total, created_by=user, comment=comment)
    for a in accs:
        a.status, a.payout = A.IN_PAYOUT, payout  # paid_amount остаётся: в пакет вошёл остаток owed
        a.save(update_fields=["status", "payout", "updated_at"])
    return payout


@transaction.atomic
def verify(payout, user):
    payout = Payout.all_objects.select_for_update().get(pk=payout.pk)
    if payout.status != P.DRAFT:
        raise BusinessError(gettext("Проверить можно только черновик выплаты."))
    payout.status, payout.verified_by, payout.verified_at = P.VERIFIED, user, timezone.now()
    payout.save()
    return payout


@transaction.atomic
def pay(payout, user, *, date=None, payment_ref=""):
    payout = Payout.all_objects.select_for_update().get(pk=payout.pk)
    if payout.status != P.VERIFIED:
        raise BusinessError(gettext("Выплатить можно только проверенную выплату (§11.1)."))
    if payout.verified_by_id == user.id:
        raise BusinessError(gettext("Проверяющий и выплачивающий должны быть разными людьми."))
    payout.status, payout.paid_by, payout.paid_at = P.PAID, user, timezone.now()
    payout.paid_date, payout.payment_ref = date or today(), payment_ref or ""
    payout.save()
    for a in payout.accruals.all():
        a.status = A.PAID
        a.save(update_fields=["status", "updated_at"])
    from . import cash
    cash.on_payout_paid(payout)
    return payout


@transaction.atomic
def cancel(payout, user, reason):
    payout = Payout.all_objects.select_for_update().get(pk=payout.pk)
    if payout.status == P.PAID:
        raise BusinessError(gettext("Выплаченную выплату отменить нельзя: оформите корректировку (сторно)."))
    if payout.status == P.CANCELLED:
        raise BusinessError(gettext("Выплата уже отменена."))
    for a in payout.accruals.all():
        a.status, a.payout = A.ACCRUED, None
        a.save(update_fields=["status", "payout", "updated_at"])
    payout.status = P.CANCELLED
    return payout.mark_cancelled(user, reason)
