"""Бизнес-правила клиентов: продажи, доход франчайзи по правилам (5Д), удержание в счёт франшизы (5Г),
возвраты, этапы внедрения."""
from django.utils.translation import gettext, gettext_lazy
from datetime import timedelta
from decimal import Decimal

from django.db import transaction

from django.utils import timezone

from billing.models import Charge, CommissionPayout, FranchiseePayment
from core.exceptions import BusinessError
from core.models import q2, today
from core.settings_registry import get_int
from network.commission import SUB_KINDS, compute_partner_income, price_on
from network.models import Contract, Franchisee
from network.partners import register_paid_sale, status_on, tier_on
from network.services import withhold_remaining
from billing import accruals

from .models import ClientSale, Implementation

ZERO = Decimal("0")
_UNSET = object()


def active_contract_for(franchisee_id, product, on=None):
    """Договор франчайзи с продуктом, действовавший на дату: активный либо закрытый с valid_until ≥ даты
    (при уходе партнёра valid_until = closed_at − 1: оплаты до закрытия начисляются и при позднем вводе, H3)."""
    from django.db.models import Q
    qs = Contract.objects.filter(franchisee_id=franchisee_id, products=product)
    if on is None:
        qs = qs.filter(status=Contract.Status.ACTIVE)
    else:
        qs = qs.filter(Q(status=Contract.Status.ACTIVE)
                       | Q(status=Contract.Status.CLOSED, valid_until__gte=on, signed_at__lte=on))
    return qs.order_by("-signed_at", "-id").first()


def cancel_derived(qs, user, reason):
    """Отменить производные записи (зачёты, удержания, доля OZOK): физически не удаляются (H7)."""
    now = timezone.now()
    for obj in list(qs):
        obj.cancelled_at, obj.cancelled_by, obj.cancel_reason = now, user, reason
        if isinstance(obj, FranchiseePayment):
            obj.status = FranchiseePayment.Status.CANCELLED
        obj.save()


def _clear_offsets(sale, user=None, reason=gettext_lazy("Пересчёт продажи")):
    cancel_derived(sale.offset_payments.all(), user, reason)
    cancel_derived(sale.offset_payouts.all(), user, reason)
    cancel_derived(sale.withhold_charges.all(), user, reason)  # включает и долю OZOK (kind=nbs_lead_share)
    sale.withheld_amount = ZERO


def _apply_withholding(sale, contract):
    """Удержание из дохода франчайзи в счёт остатка франшизы (5Г).

    withheld = franchisee_income × withhold_percent / 100, не больше остатка; зачёт FranchiseePayment(offset);
    абонплата — ещё CommissionPayout(offset); установка/оборудование/прочее — ещё Charge(withhold, +30 дней)."""
    if (contract is None or sale.franchisee_income <= 0 or sale.amount <= 0
            or Decimal(contract.withhold_percent) <= 0):
        return ZERO
    amount = min(q2(sale.franchisee_income * Decimal(contract.withhold_percent) / 100),
                 withhold_remaining(contract, exclude_sale=sale))
    if amount <= 0:
        return ZERO
    note = gettext('Удержание в счёт франшизы по договору {number}').format(number=contract.number)
    FranchiseePayment.objects.create(franchisee_id=sale.franchisee_id, contract=contract, client_sale=sale,
                                     amount=amount, date=sale.date, method=FranchiseePayment.Method.OFFSET,
                                     comment=note)
    if sale.kind not in SUB_KINDS:  # по абонплате удержание хранится в accrual.withheld_amount (C6)
        Charge.objects.create(franchisee_id=sale.franchisee_id, contract=contract, client_sale=sale,
                              kind=Charge.Kind.WITHHOLD, amount=amount,
                              due_date=sale.date + timedelta(days=get_int("withhold.due_days")),
                              description=gettext('Удержание с продажи ({v})').format(v=sale.get_kind_display().lower()))
    sale.withheld_amount = amount
    sale.save(update_fields=["withheld_amount", "updated_at"])
    return amount


def _withholding_contract(sale):
    contract = active_contract_for(sale.franchisee_id, sale.product)
    return contract if contract is not None and contract.withhold_target > 0 else None


def _lead_of(sale):
    """Лид продажи: явно заданный или лид клиента (Client.lead); источник лида определяет корректировку OZOK."""
    use_client = getattr(sale, "_use_client_lead", True)
    lead = sale.lead or (sale.client.lead if sale.client_id and use_client else None)
    if lead is not None:
        sale.lead = lead
        sale.lead_source = lead.source
    return lead


def _snapshot(sale):
    """Двухшаговый расчёт дохода франчайзи (C11); пишет снимок в продажу (без сохранения)."""
    _lead_of(sale)
    tier = tier_on(sale.franchisee, sale.date)
    r = compute_partner_income(
        franchisee_id=sale.franchisee_id, product_id=sale.product_id, tariff_id=sale.tariff_id, kind=sale.kind,
        amount=sale.amount, on=sale.date, partner_tier=tier, lead_source=sale.lead_source,
        list_amount=sale.list_amount, months=sale.months)
    sale.partner_tier = tier
    sale.commission_rule = r["rule"]
    sale.commission_percent, sale.commission_fixed = r["percent"], r["fixed"]
    sale.commission_base_amount, sale.franchisee_income = r["base"], r["income"]
    sale.adjustment_rule, sale.adjustment_amount = r["adjustment_rule"], r["adjustment_amount"]
    sale.nbs_share_amount = r["nbs_share"]
    sale.income_overridden = False
    return r["rule"]


def _apply_nbs_share(sale):
    """Доля OZOK по лиду OZOK (деньги у франчайзи): Charge(kind=nbs_lead_share) со сроком оплаты (P0)."""
    if sale.nbs_share_amount <= 0 or sale.kind in SUB_KINDS:
        return
    Charge.objects.create(
        franchisee_id=sale.franchisee_id, client_sale=sale, kind=Charge.Kind.NBS_LEAD_SHARE,
        amount=sale.nbs_share_amount, due_date=sale.date + timedelta(days=get_int("nbs_lead.share_due_days")),
        description=gettext('Доля OZOK по лиду OZOK ({v})').format(v=sale.get_kind_display().lower()))


def _sync_commission(sale):
    sale.commission_amount = sale.franchisee_income if sale.kind in SUB_KINDS else ZERO


def create_client_sale(*, client, product, kind, amount, date=None, paid_until=None, comment="",
                       source="manual", external_payment_id=None, months=None, created_by=None,
                       equipment=None, quantity=1, tariff=None, list_amount=None, discount_amount=None, lead=None, deal=None,
                       payment=None, franchisee=None, payee=_UNSET, no_payee_reason="", branch=None,
                       use_client_lead=True, serial_numbers=None, sold_by=None, warranty_until=None):
    """Создать продажу. Возвращает (sale, warning|None).

    Абонплата (P9): вызывается из billing.payments.register_payment (payment=Payment). franchisee — чей это платёж
    для видимости; payee — получатель комиссии на дату оплаты (None — комиссии нет, no_payee_reason — почему)."""
    amount = q2(amount)
    if amount <= 0:
        raise BusinessError(gettext("Сумма продажи должна быть больше нуля."))
    if discount_amount is not None:
        discount_amount = max(q2(discount_amount), ZERO)  # M4: скидка не отрицательная
    date = date or today()
    if list_amount is None:
        if discount_amount is not None:
            list_amount = amount + q2(discount_amount)
        elif tariff is not None and months and kind in SUB_KINDS and price_on(tariff, date) is not None:
            list_amount = q2(price_on(tariff, date) * months)
        else:
            list_amount = amount
    list_amount = q2(list_amount)
    discount = q2(discount_amount) if discount_amount is not None else max(list_amount - amount, ZERO)
    warnings = []
    scope_id = franchisee.id if franchisee is not None else client.franchisee_id
    partner = payee if (payee is not _UNSET and payee is not None) else (franchisee or client.franchisee)
    if payee is _UNSET and kind in SUB_KINDS and client.origin != client.Origin.PARTNER:
        # клиент OZOK: комиссия только если OZOK включил её для филиала (branches.commission_allowed)
        from .branches import commission_allowed, ensure_main_branch
        b = branch or ensure_main_branch(client)
        if not commission_allowed(b, date):
            payee, no_payee_reason = None, gettext("Клиент OZOK: комиссия не начисляется.")
    if partner is None and kind in SUB_KINDS:
        payee = None
        no_payee_reason = no_payee_reason or gettext("Клиент OZOK без партнёра: комиссия не начисляется.")
    contract = active_contract_for(partner.id, product, date) if partner is not None else None
    with transaction.atomic():
        sale = ClientSale(
            franchisee_id=scope_id, client=client, branch=branch, product=product, kind=kind, date=date, amount=amount,
            equipment=equipment, quantity=quantity, tariff=tariff, list_amount=list_amount, discount_amount=discount,
            paid_until=paid_until, comment=comment, source=source, external_payment_id=external_payment_id,
            months=months, created_by=created_by, lead=lead, deal=deal, serial_numbers=serial_numbers or [],
            sold_by=sold_by, warranty_until=warranty_until)
        sale._use_client_lead = use_client_lead
        sale._payment = payment
        p_status = status_on(partner, date) if partner is not None else None
        if partner is None and kind not in SUB_KINDS:
            warnings.append(gettext("Клиент OZOK без партнёра: вознаграждение партнёру не начисляется."))
        elif kind in SUB_KINDS and payee is None:
            _lead_of(sale)
            warnings.append(no_payee_reason or gettext("Нет получателя комиссии: начисление не создаётся."))
        elif kind in SUB_KINDS and p_status == Franchisee.Status.SUSPENDED:
            # H2: приостановленный партнёр — начисление создаётся со статусом on_hold до решения OZOK
            if contract is not None and _snapshot(sale) is not None:
                sale._held_amount = sale.franchisee_income
                sale.franchisee_income = ZERO
            warnings.append(gettext("Партнёр приостановлен: комиссия удержана (on_hold) до решения OZOK."))
        elif kind in SUB_KINDS and p_status != Franchisee.Status.FRANCHISEE:
            _lead_of(sale)
            sale.partner_tier = tier_on(partner, date)
            warnings.append(gettext('Абонентское вознаграждение не начислено: на дату оплаты статус партнёра не «Франчайзи» ({label}).').format(label=partner.__class__.Status(p_status).label))
        elif kind in SUB_KINDS and contract is None:
            _lead_of(sale)
            warnings.append(gettext('Нет действующего договора с продуктом «{name}»: вознаграждение не начислено.').format(name=product.name))
        else:
            if _snapshot(sale) is None:
                warnings.append(gettext("Нет подходящего правила вознаграждения: доход франчайзи не начислен."))
        _sync_commission(sale)
        sale.save()
        _apply_withholding(sale, _withholding_contract(sale))
        _apply_nbs_share(sale)
        accruals.sync_withheld(sale)
        # AC1: продажа в рамках сделки двигает воронку только когда сделка ОПЛАЧЕНА (deals.refresh_payment_status)
        if (amount > 0 and deal is None and partner is not None and p_status == Franchisee.Status.MANAGER
                and kind not in SUB_KINDS):
            register_paid_sale(partner, _paid_deals_count(partner))
    return sale, ("; ".join(warnings) or None)


def _paid_deals_count(partner):
    """Оплаченные продажи менеджера: сделки с payment_status=paid + отдельные продажи без сделки (H8)."""
    from .models import Deal
    paid_deals = Deal.objects.filter(partner=partner, status=Deal.Status.CONFIRMED, payment_status="paid").count()
    standalone = ClientSale.objects.filter(franchisee=partner, amount__gt=0, refund_of__isnull=True,
                                           deal__isnull=True).count()
    return paid_deals + standalone


def recalculate_sale(sale):
    """Пересчитать продажу по текущим правилам (снимает ручной доход) и заново применить удержание."""
    _guard_editable(sale)
    with transaction.atomic():
        _clear_offsets(sale)
        _snapshot(sale)
        _sync_commission(sale)
        sale.save()
        _apply_withholding(sale, _withholding_contract(sale))
        _apply_nbs_share(sale)
        accruals.sync_amounts(sale)
    return sale


def set_income(sale, income):
    """Задать доход франчайзи вручную (флаг income_overridden); удержание пересчитывается от итогового дохода."""
    _guard_editable(sale)
    income = q2(income)
    if income < 0:
        raise BusinessError(gettext("Доход не может быть отрицательным."))
    with transaction.atomic():
        _clear_offsets(sale)
        sale.franchisee_income, sale.income_overridden = income, True
        _sync_commission(sale)
        sale.save()
        _apply_withholding(sale, _withholding_contract(sale))
        _apply_nbs_share(sale)
        accruals.sync_amounts(sale)
    return sale


def set_commission_percent(sale, percent):
    """Изменить ставку по конкретной продаже (hq_admin/hq_accountant): доход = база × ставка + фикс."""
    percent = Decimal(str(percent))
    if percent < 0 or percent > 100:
        raise BusinessError(gettext("Ставка должна быть от 0 до 100."))
    if sale.kind not in SUB_KINDS:
        raise BusinessError(gettext("Вознаграждение начисляется только за подписку и продление."))
    _guard_editable(sale)
    with transaction.atomic():
        _clear_offsets(sale)
        base = sale.commission_base_amount or sale.amount
        sale.commission_percent = percent
        sale.franchisee_income = q2(base * percent / 100) + sale.commission_fixed
        sale.income_overridden = True
        _sync_commission(sale)
        sale.save()
        _apply_withholding(sale, _withholding_contract(sale))
        _apply_nbs_share(sale)
        accruals.sync_amounts(sale)
    return sale


def _guard_editable(sale):
    if sale.amount < 0 or sale.refund_of_id:
        raise BusinessError(gettext("Ставку нельзя менять у возврата."))
    if sale.refunds.exists():
        raise BusinessError(gettext("Ставку нельзя менять у продажи, по которой уже есть возврат."))


def set_stage(implementation, stage):
    if stage not in Implementation.Stage.values:
        raise BusinessError(gettext("Неизвестный этап внедрения."))
    if implementation.stage == stage:
        raise BusinessError(gettext("Внедрение уже находится на этом этапе."))
    implementation.stage = stage
    from .installations import STAGE_STATUS
    implementation.status = STAGE_STATUS.get(stage, implementation.status)
    if stage == Implementation.Stage.LAUNCHED and not implementation.launched_at:
        implementation.launched_at = today()
    implementation.save()
    return implementation


def refunded_total(original):
    """Сумма уже возвращённого по продаже (положительное число)."""
    return -sum((r.amount for r in original.refunds.all()), Decimal("0"))


def is_fully_refunded(original):
    return original.amount > 0 and refunded_total(original) >= original.amount


def refund_sale(original, amount, *, date=None, external_payment_id=None):
    """Возврат платежа: отрицательная корректировка с отрицательными доходом и удержанием (пропорционально).

    Возвраты могут быть частичными; сумма возвратов не больше суммы платежа; сторно удержания суммарно не больше
    исходного удержания (последний возврат забирает остаток)."""
    amount = q2(amount)
    if original.amount <= 0 or original.refund_of_id:
        raise BusinessError(gettext("Возврат возможен только по платежу клиента."))
    already = refunded_total(original)
    if amount <= 0 or already + amount > original.amount:
        raise BusinessError(gettext("Сумма возвратов не может превышать сумму платежа."))
    final = already + amount == original.amount
    refunds = list(original.refunds.all())
    prior_income = sum((r.franchisee_income for r in refunds), Decimal("0"))  # отрицательное
    prior_commission = sum((r.commission_amount for r in refunds), Decimal("0"))
    ratio_income = -q2(original.franchisee_income * amount / original.amount)
    prior_share = sum((r.nbs_share_amount for r in refunds), Decimal("0"))  # отрицательное
    nbs_share = (-(original.nbs_share_amount + prior_share) if final
                 else -q2(original.nbs_share_amount * amount / original.amount))
    with transaction.atomic():
        income = -(original.franchisee_income + prior_income) if final else ratio_income
        is_sub = original.kind in SUB_KINDS
        commission = (-(original.commission_amount + prior_commission) if final
                      else -q2(original.commission_amount * amount / original.amount)) if is_sub else ZERO
        sale = ClientSale.objects.create(
            franchisee_id=original.franchisee_id, client_id=original.client_id, product_id=original.product_id,
            kind=ClientSale.Kind.RENEWAL if is_sub else original.kind, date=date or today(), amount=-amount,
            tariff_id=original.tariff_id, commission_rule_id=original.commission_rule_id,
            commission_percent=original.commission_percent, commission_amount=commission,
            franchisee_income=income, income_overridden=original.income_overridden,
            nbs_share_amount=nbs_share, lead_id=original.lead_id, lead_source=original.lead_source,
            partner_tier=original.partner_tier, branch_id=original.branch_id,
            source=ClientSale.Source.INTEGRATION, external_payment_id=external_payment_id, refund_of=original,
            comment=gettext('Возврат по платежу {external_payment_id}').format(external_payment_id=original.external_payment_id))
        if nbs_share != 0:
            Charge.objects.create(
                franchisee_id=sale.franchisee_id, client_sale=sale, kind=Charge.Kind.NBS_LEAD_SHARE, amount=nbs_share,
                due_date=sale.date, description=gettext("Сторно доли OZOK (возврат платежа клиента)"))
        orig_offsets = list(original.offset_payments.all())
        total_offset = sum((p.amount for p in orig_offsets), Decimal("0"))
        reversed_before = -sum((p.amount for r in refunds for p in r.offset_payments.all()), Decimal("0"))
        left = total_offset - reversed_before
        offset = left if final else min(q2(total_offset * amount / original.amount), left)
        if offset > 0:
            note = gettext("Сторно удержания (возврат платежа клиента)")
            FranchiseePayment.objects.create(
                franchisee_id=sale.franchisee_id, client_sale=sale, amount=-offset, date=sale.date,
                contract=orig_offsets[0].contract, method=FranchiseePayment.Method.OFFSET, comment=note)
            if not is_sub:
                src = original.withhold_charges.first()
                Charge.objects.create(franchisee_id=sale.franchisee_id, contract=orig_offsets[0].contract,
                                      client_sale=sale, kind=Charge.Kind.WITHHOLD, amount=-offset,
                                      due_date=src.due_date if src else sale.date, description=note)
            sale.withheld_amount = -offset
            sale.save(update_fields=["withheld_amount", "updated_at"])
        accruals.sync_withheld(sale)
        if is_sub:
            from billing import payments
            payments.apply_refund(original, amount, sale)
    return sale


def cancel_sale(sale, user, reason):
    """Отмена продажи вместо удаления (H7): продажа и её зачёты/удержания/доля OZOK помечаются отменёнными.
    Уже оплаченные начисления остаются оплаченными: баланс уходит в кредит (минус), записи не стираются."""
    reason = (reason or "").strip()
    if not reason:
        raise BusinessError(gettext("Укажите причину отмены."))
    if sale.cancelled_at is not None:
        raise BusinessError(gettext("Продажа уже отменена."))
    if sale.refunds.exists():
        raise BusinessError(gettext("По продаже есть возвраты: сначала отмените их."))
    with transaction.atomic():
        cancel_derived(sale.offset_payments.all(), user, reason)
        cancel_derived(sale.offset_payouts.all(), user, reason)
        cancel_derived(sale.withhold_charges.all(), user, reason)
        if sale.kind in SUB_KINDS:
            accruals.reverse_for_cancel(sale, user, reason)
        sale.mark_cancelled(user, reason)
        if sale.kind in SUB_KINDS:
            from billing import payments
            payments.on_sale_cancelled(sale, user, reason)
    return sale
