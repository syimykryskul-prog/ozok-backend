"""Филиалы (P5): основной филиал, правило второго филиала, закрепление с датой вступления (C8), получатель комиссии."""
from django.utils.translation import gettext
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from core.exceptions import BusinessError
from core.models import today
from core.settings_registry import get_int

from .models import Branch, BranchAssignmentHistory, Client

H = BranchAssignmentHistory
REASON_CODES = tuple(v for v, _ in H.Reason.choices if v not in ("accepted", "partner_closed"))


def ensure_main_branch(client):
    """У каждого клиента есть основной филиал; создаётся при необходимости (партнёры = франчайзи клиента, C5)."""
    branch = client.branches.filter(is_main=True).first() or client.branches.first()
    if branch is not None:
        return branch
    fid = client.franchisee_id
    return Branch.objects.create(
        client=client, name="Основной", address=client.address, city=client.city, region=client.region,
        is_main=True, seller_partner_id=fid, implementer_partner_id=fid, responsible_partner_id=fid,
        lead=client.lead, lead_source=client.lead.source if client.lead_id else "",
        status=Branch.Status.ACTIVE)


def record(branch, *, field, from_partner, to_partner, reason_code, reason="", user=None, effective_from=None,
           from_priority=False):
    return H.objects.create(branch=branch, field=field, from_partner=from_partner, to_partner=to_partner,
                            reason_code=reason_code, reason=reason or "", decided_by=user,
                            effective_from=effective_from or today(), from_priority=from_priority)


def _value_on(branch, field, on):
    """Партнёр (поле field) филиала на дату: по истории; без истории — текущее значение."""
    current = getattr(branch, f"{field}_partner_id")
    rows = sorted((r for r in branch.assignment_history.all() if r.field in (field, H.Field.SERVICE)),
                  key=lambda r: (r.effective_from, r.id))
    if not rows:
        return current
    value = None if rows[0].from_priority else rows[0].from_partner_id
    for r in rows:
        if r.effective_from <= on:
            value = r.to_partner_id
    return value


def implementer_on(branch, on):
    return _value_on(branch, "implementer", on)


def responsible_on(branch, on):
    return _value_on(branch, "responsible", on)


def commission_allowed(branch, on):
    """Клиенты OZOK (legacy/direct): комиссия по филиалу не начисляется никому, пока OZOK явно не включил её
    (Branch.commission_enabled) с даты commission_enabled_from — в том числе после передачи филиала партнёру."""
    if branch.client.origin == Client.Origin.PARTNER:
        return True
    return bool(branch.commission_enabled and (branch.commission_enabled_from is None
                                               or branch.commission_enabled_from <= on))


@transaction.atomic
def set_commission_switch(branch, enabled, *, effective_from=None, reason="", user=None):
    """Включить/выключить комиссию сопровождающему партнёру (только super_admin; причина обязательна; аудит)."""
    from core import audit
    if branch.client.origin == Client.Origin.PARTNER:
        raise BusinessError(gettext("Переключатель действует только для клиентов OZOK (существующих и прямых)."))
    if not (reason or "").strip():
        raise BusinessError(gettext("Укажите причину."))
    enabled = bool(enabled)
    if enabled and not (branch.implementer_partner_id and branch.implementer_partner_id == branch.responsible_partner_id):
        raise BusinessError(gettext("Сначала передайте филиал партнёру на сопровождение (reassign): некому начислять комиссию."))
    old = {"commission_enabled": branch.commission_enabled,
           "commission_enabled_from": branch.commission_enabled_from.isoformat()
           if branch.commission_enabled_from else None}
    branch.commission_enabled = enabled
    branch.commission_enabled_from = (effective_from or today()) if enabled else None
    branch.save(update_fields=["commission_enabled", "commission_enabled_from", "updated_at"])
    audit.log_event("update", branch, changes={
        "commission_enabled": [old["commission_enabled"], branch.commission_enabled],
        "commission_enabled_from": [old["commission_enabled_from"],
                                    branch.commission_enabled_from.isoformat() if branch.commission_enabled_from
                                    else None]}, reason=reason.strip(), user=user)
    return branch


def payee_on(branch, on):
    """Получатель комиссии на дату оплаты (C8, H10): внедренец, он же ответственный, статус «Франчайзи»."""
    from network.models import Franchisee
    from network.partners import status_on
    if not commission_allowed(branch, on):
        return None, gettext("Клиент OZOK: комиссия не начисляется (включается владельцем для филиала).")
    impl, resp = implementer_on(branch, on), responsible_on(branch, on)
    if impl is None or impl != resp:
        return None, (gettext("Нет внедренца филиала на дату оплаты.") if impl is None else
                      gettext("Внедренец не является ответственным за филиал: комиссия не начисляется (H10)."))
    partner = Franchisee.objects.filter(pk=impl).first()
    if partner is None:
        return None, gettext("Партнёр не найден.")
    return partner, ""


def scope_partner_on(branch, on):
    """Партнёр, чьим считать платёж для видимости: получатель, иначе ответственный/внедренец/владелец клиента."""
    from network.models import Franchisee
    pid = (implementer_on(branch, on) or responsible_on(branch, on) or branch.priority_partner_id
           or branch.client.franchisee_id)
    return Franchisee.objects.get(pk=pid) if pid else None  # клиент OZOK без партнёра — None


def served_by(partner_id):
    """Q-условие: филиалы, которые партнёр сопровождает/внедряет/имеет приоритет."""
    from django.db.models import Q
    return (Q(responsible_partner_id=partner_id) | Q(implementer_partner_id=partner_id)
            | Q(priority_partner_id=partner_id, implementer_partner__isnull=True))


def served_by_prefixed(prefix, partner_id):
    from django.db.models import Q
    return (Q(**{f"{prefix}responsible_partner_id": partner_id}) | Q(**{f"{prefix}implementer_partner_id": partner_id})
            | Q(**{f"{prefix}priority_partner_id": partner_id, f"{prefix}implementer_partner__isnull": True}))


def visible_to(branch, partner_id):
    """Тот же критерий для уже загруженного филиала (C9): приоритет виден только пока филиал не закреплён."""
    return (partner_id in (branch.responsible_partner_id, branch.implementer_partner_id)
            or (branch.priority_partner_id == partner_id and branch.implementer_partner_id is None))


# ---- создание с правилом второго филиала -----------------------------------------------------------------------

def second_branch_partner(client):
    """Партнёр, у которого у клиента уже есть филиал (приоритет на второй филиал)."""
    for b in client.branches.order_by("-is_main", "id"):
        if b.implementer_partner_id:
            return b.implementer_partner_id
    return client.franchisee_id


def setup_new_branch(branch, *, user, explicit_partners=False):
    """Заполнить партнёров нового филиала. Первый филиал клиента — партнёры = франчайзи клиента (C5).
    Второй и следующие: priority_partner + дедлайн; если создаёт сам приоритетный партнёр — закрепляется сразу."""
    client = branch.client
    others = client.branches.exclude(pk=branch.pk)
    if not others.exists():
        fid = client.franchisee_id
        branch.seller_partner_id = branch.seller_partner_id or fid
        branch.implementer_partner_id = branch.implementer_partner_id or fid
        branch.responsible_partner_id = branch.responsible_partner_id or fid
        if not client.branches.filter(is_main=True).exclude(pk=branch.pk).exists():
            branch.is_main = True
        branch.status = Branch.Status.ACTIVE if branch.status == Branch.Status.PENDING else branch.status
        return branch
    prio = second_branch_partner(client)
    branch.priority_partner_id = prio
    branch.priority_deadline = timezone.now() + timedelta(days=get_int("partner.second_branch_response_days"))
    mine = user.franchisee_id
    wanted = {branch.implementer_partner_id, branch.responsible_partner_id} - {None}
    if wanted and wanted != {prio}:
        raise BusinessError(gettext("Второй филиал закреплён за партнёром первого филиала: назначайте другого партнёра "
                            "действием reassign с основанием."))
    if mine == prio or wanted == {prio}:
        branch.implementer_partner_id = branch.responsible_partner_id = prio
        branch.seller_partner_id = branch.seller_partner_id or prio
        branch.priority_accepted_at = timezone.now()
    else:
        branch.implementer_partner_id = branch.responsible_partner_id = None
        branch.seller_partner_id = branch.seller_partner_id or (mine or None)
    return branch


@transaction.atomic
def accept(branch, user):
    if branch.priority_partner_id is None:
        raise BusinessError(gettext("Для этого филиала нет приоритетного партнёра."))
    if branch.priority_accepted_at is not None or branch.implementer_partner_id:
        raise BusinessError(gettext("Филиал уже закреплён."))
    if user.franchisee_id and user.franchisee_id != branch.priority_partner_id:
        raise BusinessError(gettext("Принять филиал может только приоритетный партнёр."))
    if branch.priority_expired:
        raise BusinessError(gettext("Срок приоритета истёк: филиал назначает OZOK (reassign)."))
    pid = branch.priority_partner_id
    record(branch, field=H.Field.SERVICE, from_partner=None, to_partner=pid_obj(pid), reason_code="accepted",
           reason=gettext("Принято приоритетным партнёром"), user=user)
    branch.implementer_partner_id = branch.responsible_partner_id = pid
    branch.priority_accepted_at = timezone.now()
    branch.save()
    return branch


def pid_obj(pid):
    from network.models import Franchisee
    return Franchisee.objects.get(pk=pid) if pid else None


@transaction.atomic
def reassign(branch, partner, *, reason_code, reason, user, effective_from=None):
    from network.models import Franchisee
    if reason_code not in REASON_CODES:
        raise BusinessError(gettext("Укажите основание переназначения: ") + ", ".join(REASON_CODES) + ".")
    if not (reason or "").strip():
        raise BusinessError(gettext("Опишите основание переназначения."))
    if partner.status != Franchisee.Status.FRANCHISEE:
        raise BusinessError(gettext("Назначить можно только партнёра со статусом «Франчайзи»."))
    effective_from = effective_from or today()
    old = pid_obj(branch.implementer_partner_id)
    from_priority = old is None and branch.priority_partner_id is not None
    if old is not None and old.id == partner.id and branch.responsible_partner_id == partner.id:
        raise BusinessError(gettext("Филиал уже закреплён за этим партнёром."))
    record(branch, field=H.Field.SERVICE, from_partner=old or (pid_obj(branch.priority_partner_id)
                                                               if from_priority else None),
           to_partner=partner, reason_code=reason_code, reason=reason.strip(), user=user,
           effective_from=effective_from, from_priority=from_priority)
    branch.implementer_partner = branch.responsible_partner = partner
    branch.priority_accepted_at = branch.priority_accepted_at or timezone.now()
    branch.save()
    return branch
