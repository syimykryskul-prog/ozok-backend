"""Вознаграждение рекрутера (P3, C3/C4/H9): FIFO по подтверждённым реальным деньгам партнёра."""
from django.utils.translation import gettext
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.exceptions import BusinessError
from core.models import q2, today
from core.settings_registry import get_decimal

from .models import FranchiseePayment, RecruiterReward, RecruiterRewardAllocation

ZERO = Decimal("0")
SUB_KINDS = ("subscription", "renewal")


def eligible(user):
    """H9: рекрутёром может быть любой сотрудник OZOK; вознаграждение — у роли recruiter или с флагом."""
    return bool(user and user.franchisee_id is None and (user.role == "recruiter" or user.recruiter_reward_enabled))


def ensure_reward(partner):
    if not partner.recruited_by_id:
        return None
    recruiter = partner.recruited_by
    reward = RecruiterReward.objects.filter(partner=partner).first()
    if reward is None:
        if not eligible(recruiter):
            return None
        return RecruiterReward.objects.create(partner=partner, recruiter=recruiter,
                                              target_amount=get_decimal("recruiter.reward_amount"))
    if reward.recruiter_id != recruiter.id and reward.status in ("accruing", "accrued", "verified"):
        reward.recruiter = recruiter
        reward.save()
    return reward


def counted_payments(partner):
    """Деньги, реально полученные OZOK по договору франшизы: платежи cash/bank и удержания из абонплаты (C3).
    Зачёты по установке/оборудованию не считаются: деньги остаются у франчайзи (оформляются Charge)."""
    rows = []
    qs = FranchiseePayment.objects.filter(franchisee=partner, contract__isnull=False,
                                          status=FranchiseePayment.Status.CONFIRMED).select_related("client_sale")
    for p in qs.order_by("date", "id"):
        if p.method == FranchiseePayment.Method.OFFSET:
            if p.client_sale_id is None or p.client_sale.kind not in SUB_KINDS:
                continue
        rows.append(p)
    return rows


@transaction.atomic
def recompute(partner):
    """Пересчитать начисление FIFO по всем подтверждённым платежам; вызывается при любом изменении платежей."""
    reward = ensure_reward(partner)
    if reward is None or reward.status == RecruiterReward.Status.CANCELLED:
        return reward
    payments = counted_payments(partner)
    total = max(sum((p.amount for p in payments), ZERO), ZERO)
    accrued = min(q2(total), reward.target_amount)
    if reward.status == RecruiterReward.Status.PAID:
        # выплата неизменна; при уменьшении базы фиксируем долг рекрутера (корректировка видна финансисту)
        debt = max(reward.paid_amount - accrued, ZERO)
        if debt != reward.debt_amount:
            reward.debt_amount = debt
            reward.save(update_fields=["debt_amount", "updated_at"])
        return reward
    reward.allocations.all().delete()
    left = accrued
    for p in payments:
        if left <= 0:
            break
        part = min(left, p.amount) if p.amount > 0 else ZERO
        if part > 0:
            RecruiterRewardAllocation.objects.create(reward=reward, payment=p, amount=part)
            left -= part
    reward.accrued_amount = accrued
    if accrued >= reward.target_amount:
        if reward.status not in (RecruiterReward.Status.VERIFIED,):
            reward.status = RecruiterReward.Status.ACCRUED
    else:
        reward.status = RecruiterReward.Status.ACCRUING
        reward.verified_by, reward.verified_at = None, None
    reward.save()
    return reward


def verify(reward, user):
    if reward.status != RecruiterReward.Status.ACCRUED:
        raise BusinessError(gettext("Проверить можно только полностью начисленное вознаграждение."))
    reward.status, reward.verified_by, reward.verified_at = RecruiterReward.Status.VERIFIED, user, timezone.now()
    reward.save()
    return reward


def pay(reward, user, *, date=None, payment_ref=""):
    if reward.status != RecruiterReward.Status.VERIFIED:
        raise BusinessError(gettext("Выплатить можно только проверенное вознаграждение."))
    if reward.verified_by_id == user.id:
        raise BusinessError(gettext("Проверяющий и исполнитель выплаты должны быть разными сотрудниками."))
    reward.status, reward.paid_by, reward.paid_at = RecruiterReward.Status.PAID, user, timezone.now()
    reward.paid_date = date or today()
    reward.paid_amount, reward.payment_ref = reward.accrued_amount, payment_ref or ""
    reward.save()
    from . import cash
    cash.on_reward_paid(reward)
    return reward


def cancel(reward, user, reason):
    if not (reason or "").strip():
        raise BusinessError(gettext("Укажите причину отмены."))
    if reward.status == RecruiterReward.Status.PAID:
        raise BusinessError(gettext("Выплаченное вознаграждение не отменяется: оформите корректировку."))
    if reward.status == RecruiterReward.Status.CANCELLED:
        raise BusinessError(gettext("Вознаграждение уже отменено."))
    reward.status, reward.cancel_reason = RecruiterReward.Status.CANCELLED, reason.strip()
    reward.save()
    return reward


def recruiters_report():
    """Отчёт рекрутеров: привлечено, активировано, начислено, выплачено."""
    from accounts.models import User
    from network.models import Franchisee
    out = []
    users = User.objects.filter(franchisee__isnull=True).filter(
        id__in=Franchisee.objects.exclude(recruited_by=None).values("recruited_by_id")) | \
        User.objects.filter(role="recruiter")
    for u in users.distinct().order_by("id"):
        partners = Franchisee.objects.filter(recruited_by=u)
        rewards = RecruiterReward.objects.filter(recruiter=u).exclude(status="cancelled")
        out.append({
            "recruiter": u.id, "name": u.full_name, "recruited": partners.count(),
            "activated": partners.filter(status__in=("franchisee", "suspended")).count()
            + partners.filter(funnel_stage="activated").exclude(status__in=("franchisee", "suspended")).count(),
            "accrued": str(sum((r.accrued_amount for r in rewards), ZERO)),
            "paid": str(sum((r.paid_amount for r in rewards.filter(status="paid")), ZERO)),
        })
    return out
