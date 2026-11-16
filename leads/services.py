"""Бизнес-правила заявок: автораспределение, воронка, владение, запросы на изменение (P4)."""
from django.utils.translation import gettext
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from clients.models import Client
from core import change_requests
from core.exceptions import BusinessError
from network.models import Franchisee

from .models import Lead, LeadActivity


def _norm(value):
    return (value or "").strip().lower()


def log(lead, user, type_, text=""):
    return LeadActivity.objects.create(lead=lead, user=user if getattr(user, "pk", None) else None, type=type_,
                                       text=text)


def pick_franchisee(city):
    """Франчайзи (статус franchisee), у которого город есть в cities (без учёта регистра);
    при нескольких — с наименьшим числом открытых заявок (далее — с меньшим id)."""
    city = _norm(city)
    if not city:
        return None
    candidates = [f for f in Franchisee.objects.filter(status=Franchisee.Status.FRANCHISEE).order_by("id")
                  if city in {_norm(c) for c in (f.cities or [])}]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    load = {r["franchisee_id"]: r["n"] for r in Lead.objects.filter(
        franchisee__in=candidates, status__in=Lead.OPEN_STATUSES + (Lead.Status.NEW,))
        .values("franchisee_id").annotate(n=Count("id"))}
    return min(candidates, key=lambda f: (load.get(f.id, 0), f.id))


def create_lead(*, franchisee=None, now=None, user=None, by_partner=False, **fields):
    """Создать заявку. franchisee указан явно -> назначается ему; interest=franchise -> остаётся в OZOK;
    иначе автораспределение по городу. Лид, созданный партнёром, ждёт подтверждения владельца OZOK."""
    now = now or timezone.now()
    lead = Lead(**fields)
    target = franchisee
    if target is None and lead.interest != Lead.Interest.FRANCHISE:
        target = pick_franchisee(lead.city)
    lead.status = Lead.Status.NEW
    if target is not None:
        lead.franchisee = target
        lead.assigned_at = now
    if by_partner:
        lead.owner_confirmed = False
    lead.save()
    log(lead, user, "note", gettext("Заявка создана") + (gettext(' (партнёр {name})').format(name=target.name) if target else ""))
    return lead


def _check_open(lead, allowed, message):
    if lead.status not in allowed:
        raise BusinessError(message)


OPEN_OR_NEW = (Lead.Status.NEW,) + Lead.OPEN_STATUSES


def assign_lead(lead, franchisee, now=None, reason="", user=None):
    """Назначение/переназначение OZOK. Переназначение (у заявки уже есть партнёр) — только с причиной (§6.2.5)."""
    _check_open(lead, OPEN_OR_NEW, gettext("Закрытую заявку нельзя переназначить."))
    reason = (reason or "").strip()
    previous = lead.franchisee
    if previous is not None and previous.id != franchisee.id and not reason:
        raise BusinessError(gettext("Укажите причину переназначения."))
    lead.franchisee = franchisee
    lead.status = Lead.Status.NEW
    lead.assigned_at = now or timezone.now()
    lead.taken_at = None
    lead._audit_reason = reason
    lead.save()
    log(lead, user, "reassign", f"{previous.name if previous else '—'} → {franchisee.name}. {reason}".strip())
    return lead


def take_lead(lead, now=None, user=None):
    if lead.status != Lead.Status.NEW or lead.franchisee_id is None:
        raise BusinessError(gettext("Взять в работу можно только назначенную заявку."))
    lead.status = Lead.Status.IN_PROGRESS
    lead.taken_at = now or timezone.now()
    lead.save()
    log(lead, user, "status", gettext("В работе"))
    return lead


def demo_lead(lead, now=None, user=None):
    _check_open(lead, (Lead.Status.IN_PROGRESS,), gettext("Перейти к демо можно только из статуса «В работе»."))
    lead.status = Lead.Status.DEMO
    lead.save()
    log(lead, user, "demo", gettext("Демо"))
    return lead


def set_status(lead, status, user=None):
    """Продвижение по воронке: КП, договор (и возврат между этапами)."""
    if status not in Lead.PIPELINE:
        raise BusinessError(gettext("Статус можно выбрать из: в работе, демо, КП, договор. Оплата — win, отказ — lose."))
    _check_open(lead, Lead.OPEN_STATUSES, gettext("Сменить статус можно только у заявки в работе."))
    if lead.status == status:
        raise BusinessError(gettext("Заявка уже в этом статусе."))
    lead.status = status
    lead.save()
    log(lead, user, "proposal" if status == "proposal" else "status", lead.get_status_display())
    return lead


def win_lead(lead, business_name="", business_type="", now=None, user=None):
    _check_open(lead, Lead.OPEN_STATUSES, gettext("Закрыть успешно можно заявку, которая в работе."))
    if business_type and business_type not in Client.BusinessType.values:
        raise BusinessError(gettext("Неизвестный тип бизнеса."))
    if lead.franchisee_id is None:
        raise BusinessError(gettext("Заявка не назначена партнёру."))
    with transaction.atomic():
        client = Client.objects.create(
            franchisee=lead.franchisee, business_name=business_name or lead.company_name or lead.name,
            business_type=business_type or Client.BusinessType.OTHER, contact_name=lead.contact_name or lead.name,
            phone=lead.phone, city=lead.city, lead=lead)
        lead.status = Lead.Status.PAID
        lead.closed_at = now or timezone.now()
        lead.client = client
        lead.save()
        log(lead, user, "status", gettext("Оплачено: клиент создан"))
    return lead


def lose_lead(lead, reason, now=None, user=None):
    if not (reason or "").strip():
        raise BusinessError(gettext("Укажите причину отказа."))
    _check_open(lead, OPEN_OR_NEW, gettext("Заявка уже закрыта."))
    lead.status = Lead.Status.LOST
    lead.lost_reason = reason.strip()
    lead.closed_at = now or timezone.now()
    lead.save()
    log(lead, user, "status", gettext('Отказ: {lost_reason}').format(lost_reason=lead.lost_reason))
    return lead


def confirm_owner(lead, user):
    if lead.owner_confirmed:
        raise BusinessError(gettext("Владелец заявки уже подтверждён."))
    lead.owner_confirmed, lead.owner_confirmed_by, lead.owner_confirmed_at = True, user, timezone.now()
    lead.save()
    log(lead, user, "note", gettext("OZOK подтвердила владельца заявки"))
    return lead


# ---- запросы на изменение (франчайзи не меняет источник/ответственных напрямую) ---------------------

LOCKED_FIELDS = ("source", "franchisee", "implementer")


def request_change(lead, user, field, value, reason):
    if field not in LOCKED_FIELDS:
        raise BusinessError(gettext("Через запрос меняются: source, franchisee, implementer."))
    if not (reason or "").strip():
        raise BusinessError(gettext("Укажите причину."))
    if field == "source":
        if value not in Lead.Source.values:
            raise BusinessError(gettext("Неизвестный источник."))
        old = lead.source
    elif field == "franchisee":
        if not Franchisee.objects.filter(pk=value).exists():
            raise BusinessError(gettext("Партнёр не найден."))
        old = lead.franchisee_id
    else:
        from accounts.models import User
        if not User.objects.filter(pk=value, franchisee__isnull=True).exists():
            raise BusinessError(gettext("Сотрудник OZOK не найден."))
        old = lead.implementer_id
    cr = change_requests.create_request(user=user, entity="leads.Lead", entity_id=lead.pk, field=field,
                                        old_value=old, new_value=value, reason=reason,
                                        franchisee=user.franchisee if user.franchisee_id else lead.franchisee)
    log(lead, user, "change_request", gettext('Запрос на изменение {field}: {old} → {value}. {reason}').format(field=field, old=old, value=value, reason=reason))
    return cr


def _apply_lead_change(cr, user):
    lead = Lead.objects.get(pk=cr.entity_id)
    if cr.field == "source":
        lead.source = cr.new_value
    elif cr.field == "franchisee":
        lead.franchisee = Franchisee.objects.get(pk=cr.new_value)
        lead.assigned_at = timezone.now()
    else:
        lead.implementer_id = cr.new_value
    lead._audit_reason = cr.reason
    lead.save()
    log(lead, user, "change_request", gettext('Одобрено: {field} → {new_value}').format(field=cr.field, new_value=cr.new_value))


for _f in LOCKED_FIELDS:
    change_requests.register("leads.Lead", _f, _apply_lead_change)
