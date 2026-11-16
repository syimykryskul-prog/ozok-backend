"""Партнёры: история статуса/уровня с датой действия (P1), воронка кандидата и её автоматика (P2)."""
from django.utils.translation import gettext, gettext_lazy
from django.db import transaction

from core.exceptions import BusinessError
from core.models import add_months, month_first, today

from .models import Franchisee, PartnerStatusHistory

F = PartnerStatusHistory.Field
DATED_FIELDS = (F.STATUS, F.TIER)  # действуют с effective_from; этап воронки — просто журнал


def _value_on(partner, field, on):
    rows = list(PartnerStatusHistory.objects.filter(partner=partner, field=field).order_by("effective_from", "id"))
    if not rows:
        return getattr(partner, field)
    if on < rows[0].effective_from:
        return rows[0].old or getattr(partner, field)
    value = rows[0].old
    for r in rows:
        if r.effective_from <= on:
            value = r.new
    return value


def status_on(partner, on=None):
    return _value_on(partner, F.STATUS, on or today())


def tier_on(partner, on=None):
    return _value_on(partner, F.TIER, on or today())


def sync_partner(partner, on=None):
    """Привести текущие поля partner.status/tier к значениям на дату (после наступления effective_from)."""
    on = on or today()
    changed = []
    for field in DATED_FIELDS:
        value = _value_on(partner, field, on)
        if value != getattr(partner, field):
            setattr(partner, field, value)
            changed.append(field)
    if changed:
        _set_status_dates(partner)
        partner.save()
    return changed


def apply_due_changes(on=None):
    """Применить изменения, у которых наступила дата effective_from (команда apply_partner_changes / чтение списка)."""
    on = on or today()
    due = PartnerStatusHistory.objects.filter(applied=False, effective_from__lte=on, field__in=DATED_FIELDS)
    ids = set(due.values_list("partner_id", flat=True))
    for partner in Franchisee.objects.filter(id__in=ids):
        sync_partner(partner, on)
    due.update(applied=True)
    return len(ids)


def _set_status_dates(partner):
    if partner.status == Franchisee.Status.SUSPENDED and not partner.suspended_at:
        partner.suspended_at = today()
    if partner.status == Franchisee.Status.CLOSED and not partner.closed_at:
        partner.closed_at = today()


def default_effective_from(partner, field, old, new, on):
    """Понижение уровня — с 1-го числа следующего расчётного месяца; остальное — сразу."""
    if field == F.TIER and old == Franchisee.Tier.GROWTH and new == Franchisee.Tier.STANDARD:
        return add_months(month_first(on), 1)
    return on


@transaction.atomic
def change_partner_field(partner, field, new, *, user=None, reason="", effective_from=None, allow_backdate=False):
    """Сменить статус/уровень/этап партнёра с историей. Возвращает запись истории (или None, если без изменений)."""
    if field not in F.values:
        raise BusinessError(gettext("Неизвестное поле истории."))
    valid = {F.STATUS: Franchisee.Status.values, F.TIER: Franchisee.Tier.values,
             F.FUNNEL_STAGE: Franchisee.Stage.values}[field]
    if new not in valid:
        raise BusinessError(gettext("Недопустимое значение."))
    on = today()
    old = getattr(partner, field)
    if field == F.FUNNEL_STAGE:
        if old == new:
            return None
        partner.funnel_stage, partner.funnel_stage_since = new, on
        partner.save(update_fields=["funnel_stage", "funnel_stage_since", "updated_at"])
        return PartnerStatusHistory.objects.create(partner=partner, field=field, old=old, new=new,
                                                   effective_from=on, reason=reason, decided_by=user)
    if effective_from is None:
        effective_from = default_effective_from(partner, field, old, new, on)
    if effective_from < on:
        if not (user and user.role == "super_admin") and not allow_backdate:
            raise BusinessError(gettext("Изменение задним числом доступно только Super Admin OZOK."))
        if not (reason or "").strip():
            raise BusinessError(gettext("Для изменения задним числом укажите причину."))
    if old == new and effective_from <= on:
        return None
    # «старое» значение для записи — действующее на дату начала (важно при вставке в середину истории)
    old_on_date = _value_on(partner, field, effective_from)
    hist = PartnerStatusHistory.objects.create(
        partner=partner, field=field, old=old_on_date, new=new, effective_from=effective_from,
        reason=reason or "", decided_by=user, applied=effective_from <= on)
    sync_partner(partner)
    if field == F.STATUS:
        if new == Franchisee.Status.SUSPENDED:
            partner.suspended_at = effective_from
        elif new == Franchisee.Status.CLOSED:
            partner.closed_at = effective_from
        partner.status_reason = reason or partner.status_reason
        partner.save()
    return hist


# ---- воронка (P2) -----------------------------------------------------------------------------------

MANUAL_STAGES = ("new_application", "interview", "training", "exam", "franchise_contract")


def set_stage(partner, stage, *, user=None, reason="", auto=False):
    """Переместить кандидата по воронке; статус партнёра следует за этапом (кроме activated/франчайзи)."""
    if stage not in Franchisee.Stage.values:
        raise BusinessError(gettext("Неизвестный этап воронки."))
    if not auto:
        if stage == Franchisee.Stage.ACTIVATED:
            raise BusinessError(gettext("Этап «Активирован» ставится автоматически при активации договора франшизы."))
        if stage in ("manager", "first_sale", "second_sale", "third_sale"):
            if stage == "manager" and partner.certification_result == Franchisee.Certification.PASSED:
                pass
            else:
                raise BusinessError(gettext("Этапы менеджера ставятся автоматически: после сдачи экзамена и по продажам."))
    change_partner_field(partner, F.FUNNEL_STAGE, stage, user=user, reason=reason)
    target_status = Franchisee.STAGE_STATUS[stage]
    if partner.status in (Franchisee.Status.CANDIDATE, Franchisee.Status.TRAINEE, Franchisee.Status.MANAGER) \
            and partner.status != target_status and stage != "activated":
        change_partner_field(partner, F.STATUS, target_status, user=user, reason=reason or gettext("Этап воронки"))
    return partner


def record_certification(partner, result, *, user=None, on=None):
    """Результат экзамена; passed на этапе «экзамен» → этап и статус «менеджер»."""
    partner.certification_result = result
    partner.certification_date = on or today()
    partner.save(update_fields=["certification_result", "certification_date", "updated_at"])
    if result == Franchisee.Certification.PASSED and partner.funnel_stage in ("exam", "training"):
        set_stage(partner, "manager", user=user, reason=gettext("Экзамен сдан"), auto=True)


SALE_STAGES = ("first_sale", "second_sale", "third_sale")


def register_paid_sale(partner, count):
    """Оплаченная продажа менеджера двигает этап 1-я/2-я/3-я продажа (H8: возврат этап не откатывает)."""
    if partner is None or partner.status != Franchisee.Status.MANAGER:
        return
    stage = SALE_STAGES[min(max(count, 1), 3) - 1]
    order = Franchisee.STAGE_ORDER
    if order.index(stage) > order.index(partner.funnel_stage):
        set_stage(partner, stage, reason=gettext('Оплаченная продажа №{v}').format(v=min(count, 3)), auto=True)


def activate_partner(partner, *, user=None, reason=gettext_lazy("Договор франшизы активирован")):
    """Активация договора франшизы: статус franchisee, tier standard (если ещё не growth), этап activated."""
    if partner.status in (Franchisee.Status.CLOSED,):
        return
    if partner.status != Franchisee.Status.FRANCHISEE:
        change_partner_field(partner, F.STATUS, Franchisee.Status.FRANCHISEE, user=user, reason=reason)
    if partner.funnel_stage != Franchisee.Stage.ACTIVATED:
        change_partner_field(partner, F.FUNNEL_STAGE, Franchisee.Stage.ACTIVATED, user=user, reason=reason)
    if not partner.joined_at:
        partner.joined_at = today()
        partner.save(update_fields=["joined_at", "updated_at"])


def days_in_stage(partner):
    return (today() - partner.funnel_stage_since).days
