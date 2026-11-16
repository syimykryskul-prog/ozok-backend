"""Начисления комиссий (P8): создаются из оплаты, хранят снимок ставки, сторно — отрицательными записями (H5)."""
from django.utils.translation import gettext
from datetime import datetime, time
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.exceptions import BusinessError
from core.models import q2

from .models import CommissionAccrual, Payment

A = CommissionAccrual.Status
ZERO = Decimal("0.00")
SUB_KINDS = ("subscription", "renewal")


def derive_payment(sale):
    """Производная Payment для продажи абонплаты, созданной не через Payment (ORM/сидер/старые клиенты API)."""
    from clients.branches import ensure_main_branch
    from clients.models import Subscription
    branch = sale.branch or ensure_main_branch(sale.client)
    sub = Subscription.objects.filter(branch=branch, product_id=sale.product_id).first() or \
        Subscription.objects.filter(client_id=sale.client_id, product_id=sale.product_id).first()
    paid_at = timezone.make_aware(datetime.combine(sale.date, time(12, 0)))
    ext = sale.external_payment_id or ""
    source = Payment.Source.PRODUCT if sale.source == "integration" else Payment.Source.MANUAL
    if ext and Payment.all_objects.filter(source=source, external_id=ext, product_id=sale.product_id).exists():
        ext = ""
    months = sale.months or 1
    payment = Payment.objects.create(
        branch=branch, subscription=sub, client_id=sale.client_id, product_id=sale.product_id, amount=sale.amount,
        paid_at=paid_at, period_to=sale.paid_until, months=months, source=source, external_id=ext,
        purpose=sale.comment or "", sale=sale, deal_id=sale.deal_id, created_by=sale.created_by)
    return payment


def _fields(sale):
    return dict(rule_id=sale.commission_rule_id, adjustment_rule_id=sale.adjustment_rule_id,
                rate=sale.commission_percent, fixed_amount=sale.commission_fixed,
                base_amount=sale.commission_base_amount, adjustment_amount=sale.adjustment_amount,
                partner_tier=sale.partner_tier, lead_source=sale.lead_source)


def on_sale_created(sale):
    """post_save ClientSale: платёж 1:1 и начисление (или сторно для возврата)."""
    if sale.kind not in SUB_KINDS or sale.amount == 0:
        return
    if sale.refund_of_id:
        create_reversal(sale)
        return
    payment = getattr(sale, "_payment", None)
    if payment is None:
        payment = derive_payment(sale)
    elif payment.sale_id is None:
        payment.sale = sale
        payment.save(update_fields=["sale", "updated_at"])
    held = getattr(sale, "_held_amount", None)
    amount = held if held else sale.commission_amount
    if not amount:
        return
    CommissionAccrual.objects.create(
        payment=payment, sale=sale, partner_id=sale.franchisee_id, branch_id=payment.branch_id,
        client_id=sale.client_id, product_id=sale.product_id, date=sale.date, amount=q2(amount),
        status=A.ON_HOLD if held else A.ACCRUED,
        reason=gettext("Партнёр приостановлен: начисление удержано до решения OZOK.") if held else "", **_fields(sale))


def accrual_of(sale):
    return CommissionAccrual.all_objects.filter(sale=sale, reversal_of__isnull=True).first()


def sync_withheld(sale):
    """После удержания в счёт франшизы (5Г): withheld_amount начисления = withheld_amount продажи."""
    if sale.kind not in SUB_KINDS:
        return
    acc = CommissionAccrual.all_objects.filter(sale=sale).first()
    if acc is not None and acc.withheld_amount != sale.withheld_amount and acc.status != A.PAID:
        acc.withheld_amount = sale.withheld_amount
        acc.save(update_fields=["withheld_amount", "updated_at"])


def _reversal_sum(orig):
    return sum((r.amount for r in orig.reversals.all()), ZERO)


def create_reversal(refund_sale):
    """Возврат платежа: отрицательное начисление тому же партнёру (C8), пропорционально; paid остаётся paid (H5)."""
    orig_sale = refund_sale.refund_of
    orig = accrual_of(orig_sale)
    if orig is None:
        return None
    prior = _reversal_sum(orig)
    refunded = -sum((r.amount for r in orig_sale.refunds.all()), Decimal("0"))  # включая текущий
    final = refunded >= orig_sale.amount
    if orig.status == A.ON_HOLD:
        amount = -(orig.amount + prior) if final else -q2(orig.amount * (-refund_sale.amount) / orig_sale.amount)
    else:
        amount = refund_sale.commission_amount
    if not amount:
        return None
    return CommissionAccrual.objects.create(
        payment_id=orig.payment_id, sale=refund_sale, partner_id=orig.partner_id, branch_id=orig.branch_id,
        client_id=orig.client_id, product_id=orig.product_id, date=refund_sale.date, amount=amount,
        status=A.ON_HOLD if orig.status == A.ON_HOLD else A.ACCRUED, reversal_of=orig,
        reason=gettext("Возврат платежа клиента"), rule_id=orig.rule_id, adjustment_rule_id=orig.adjustment_rule_id,
        rate=orig.rate, fixed_amount=orig.fixed_amount, partner_tier=orig.partner_tier, lead_source=orig.lead_source)


def reverse_for_cancel(sale, user, reason):
    """Отмена продажи/платежа: сторно отрицательной записью (оплаченное остаётся оплаченным); on_hold — отменяется."""
    orig = accrual_of(sale)
    if orig is None or orig.status == A.CANCELLED:
        return None
    if orig.status == A.ON_HOLD:
        orig.status = A.CANCELLED
        orig.mark_cancelled(user, reason)
        return orig
    net_prior = _reversal_sum(orig)
    amount = -(orig.amount + net_prior)
    if not amount and not orig.withheld_amount:
        return None
    return CommissionAccrual.objects.create(
        payment_id=orig.payment_id, sale=sale, partner_id=orig.partner_id, branch_id=orig.branch_id,
        client_id=orig.client_id, product_id=orig.product_id, date=timezone.localdate(), amount=amount,
        withheld_amount=-orig.withheld_amount, status=A.ACCRUED, reversal_of=orig,
        reason=gettext('Отмена оплаты: {reason}').format(reason=reason)[:500], rule_id=orig.rule_id, adjustment_rule_id=orig.adjustment_rule_id,
        rate=orig.rate, fixed_amount=orig.fixed_amount, partner_tier=orig.partner_tier, lead_source=orig.lead_source,
        decided_by=user)


@transaction.atomic
def release(accrual, user):
    if accrual.status != A.ON_HOLD:
        raise BusinessError(gettext("Освободить можно только удержанное начисление."))
    accrual.status, accrual.decided_by = A.ACCRUED, user
    accrual.reason = gettext("Освобождено OZOK")
    accrual.save()
    sale = accrual.sale
    if sale is not None and not accrual.reversal_of_id:
        sale.commission_amount = sale.franchisee_income = accrual.amount
        sale.save(update_fields=["commission_amount", "franchisee_income", "updated_at"])
    return accrual


@transaction.atomic
def cancel_hold(accrual, user, reason):
    if accrual.status != A.ON_HOLD:
        raise BusinessError(gettext("Отменить можно только удержанное начисление."))
    accrual.status, accrual.decided_by = A.CANCELLED, user
    return accrual.mark_cancelled(user, reason)


def sync_amounts(sale):
    """Пересчёт/ручная ставка по продаже: обновить непроведённое начисление (оплаченное — только корректировкой)."""
    if sale.kind not in SUB_KINDS:
        return
    acc = accrual_of(sale)
    if acc is None:
        if sale.commission_amount:
            on_sale_created(sale)
            sync_withheld(sale)
        return
    if acc.status in (A.PAID, A.IN_PAYOUT):
        raise BusinessError(gettext("Начисление уже в выплате или выплачено: пересчёт невозможен, оформите корректировку."))
    if acc.status == A.CANCELLED:
        return
    for k, v in _fields(sale).items():
        setattr(acc, k, v)
    if acc.status != A.ON_HOLD:
        acc.amount = q2(sale.commission_amount)
    acc.withheld_amount = sale.withheld_amount
    acc.save()
