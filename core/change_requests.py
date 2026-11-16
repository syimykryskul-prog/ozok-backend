"""Запросы на изменение, подтверждаемые OZOK (P4/P5): реестр обработчиков по (entity, field)."""
from django.utils.translation import gettext
from django.db import transaction
from django.utils import timezone

from . import audit
from .exceptions import BusinessError
from .models import ChangeRequest
from .settings_registry import get_setting

HANDLERS = {}   # (entity, field) -> callable(cr, user) — применяет одобренное изменение
REJECTORS = {}  # (entity, field) -> callable(cr, user) — реакция на отклонение (необязательно)


def register(entity, field, apply, reject=None):
    HANDLERS[(entity, field)] = apply
    if reject:
        REJECTORS[(entity, field)] = reject


def create_request(*, user, entity, entity_id, field, old_value, new_value, reason="", franchisee=None):
    return ChangeRequest.objects.create(
        requested_by=user, entity=entity, entity_id=entity_id, field=field, old_value=old_value,
        new_value=new_value, reason=(reason or "")[:500], franchisee=franchisee)


def can_decide(user):
    return user.franchisee_id is None and user.role in (get_setting("approvals.roles") or [])


@transaction.atomic
def decide(cr, user, approve, comment=""):
    if not can_decide(user):
        raise BusinessError(gettext("Ваша роль не может решать запросы на изменение (настройка «Согласования»)."))
    if cr.status != ChangeRequest.Status.PENDING:
        raise BusinessError(gettext("Запрос уже рассмотрен."))
    key = (cr.entity, cr.field)
    cr.decision_comment = (comment or "")[:500]
    if approve:
        handler = HANDLERS.get(key)
        if handler is None:
            raise BusinessError(gettext("Для этого запроса нет обработчика."))
        handler(cr, user)
    elif key in REJECTORS:
        REJECTORS[key](cr, user)
    cr.status = ChangeRequest.Status.APPROVED if approve else ChangeRequest.Status.REJECTED
    cr.decided_by, cr.decided_at, cr.decision_comment = user, timezone.now(), (comment or "")[:500]
    cr.save()
    audit.log_event("approve" if approve else "reject", cr, reason=comment, user=user)
    return cr
