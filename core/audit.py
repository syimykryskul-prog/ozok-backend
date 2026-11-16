"""Журнал аудита (P1): автоматическая запись изменений отслеживаемых моделей + ручные события."""
import contextvars
from datetime import date, datetime
from decimal import Decimal

from django.apps import apps
from django.db.models.signals import post_save, pre_save

_request = contextvars.ContextVar("audit_request", default=None)

# модель -> поля, которые не пишем в журнал
TRACKED = {
    "accounts.User": {"password", "last_login", "otp_secret", "otp_last_step", "date_joined"},
    "accounts.AccessRole": {"updated_at"},
    "network.Franchisee": set(),
    "network.Contract": set(),
    "network.Product": {"api_key", "last_event_at"},
    "network.Tariff": set(),
    "network.TariffPrice": set(),
    "network.Equipment": set(),
    "network.CommissionRule": set(),
    "network.FranchiseOffer": set(),
    "network.PartnerStatusHistory": set(),
    "billing.Charge": set(),
    "billing.FranchiseePayment": set(),
    "billing.CommissionPayout": set(),
    "billing.RecruiterReward": set(),
    "billing.PartnerLedgerEntry": set(),
    "billing.LedgerCategory": set(),
    "clients.ClientSale": set(),
    "clients.Client": set(),
    "clients.Branch": set(),
    "clients.BranchAssignmentHistory": set(),
    "clients.Subscription": set(),
    "clients.ClientContact": set(),
    "clients.Implementation": set(),
    "clients.ImplementerAccrual": set(),
    "clients.ChecklistTemplate": set(),
    "clients.ContractTemplate": set(),
    "billing.Payment": {"raw"},
    "billing.CommissionAccrual": set(),
    "billing.Payout": set(),
    "billing.CashAccount": set(),
    "billing.CashCategory": set(),
    "billing.CashTransaction": set(),
    "billing.NbsExpense": set(),
    "network.SetupSplitRule": set(),
    "clients.Deal": set(),
    "clients.DealLine": set(),
    "leads.Lead": {"assigned_at", "taken_at"},
    "core.SystemSetting": {"updated_at", "updated_by"},
    "core.ChangeRequest": set(),
}
SKIP = {"created_at", "updated_at"}


def set_request(request):
    return _request.set(request)


def reset_request(token):
    _request.reset(token)


def request_info():
    req = _request.get()
    if req is None:
        return None, None, ""
    user = getattr(req, "user", None)
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None
    return user, req.META.get("REMOTE_ADDR") or None, (req.META.get("HTTP_USER_AGENT") or "")[:300]


def _plain(v):
    if isinstance(v, (Decimal, date, datetime)):
        return str(v)
    if isinstance(v, (list, dict, str, int, float, bool)) or v is None:
        return v
    return str(v)


def snapshot(instance, exclude=()):
    data = {}
    for f in instance._meta.concrete_fields:
        if f.name in SKIP or f.name in exclude:
            continue
        data[f.name] = _plain(getattr(instance, f.attname))
    return data


def write(action, *, entity="", entity_id="", changes=None, reason="", user=None):
    from .models import AuditLog
    ctx_user, ip, ua = request_info()
    user = user or ctx_user
    AuditLog.objects.create(user=user, user_email=getattr(user, "email", "") or "", action=action, entity=entity,
                            entity_id=str(entity_id), changes=changes or {}, reason=(reason or "")[:500], ip=ip,
                            user_agent=ua)


def log_event(action, obj=None, *, changes=None, reason="", user=None, entity=None, entity_id=None):
    write(action, entity=entity or (obj._meta.label if obj is not None else ""),
          entity_id=entity_id if entity_id is not None else (obj.pk if obj is not None else ""),
          changes=changes, reason=reason, user=user)


def _before(sender, instance, **kwargs):
    label = sender._meta.label
    if label not in TRACKED or kwargs.get("raw"):
        return
    instance._audit_old = None
    if instance.pk:
        old = sender._base_manager.filter(pk=instance.pk).first()
        if old is not None:
            instance._audit_old = snapshot(old, TRACKED[label])


def _after(sender, instance, created, **kwargs):
    label = sender._meta.label
    if label not in TRACKED or kwargs.get("raw"):
        return
    new = snapshot(instance, TRACKED[label])
    reason = getattr(instance, "_audit_reason", "") or getattr(instance, "cancel_reason", "") or \
        getattr(instance, "reason", "") or ""
    old = getattr(instance, "_audit_old", None)
    if created or old is None:
        changes = {k: [None, v] for k, v in new.items() if v not in (None, "", [], {})}
        write("create", entity=label, entity_id=instance.pk, changes=changes, reason=reason)
        return
    changes = {k: [old.get(k), v] for k, v in new.items() if old.get(k) != v}
    if not changes:
        return
    action = "update"
    if "cancelled_at" in changes and changes["cancelled_at"][0] is None:
        action = "cancel"
    elif "status" in changes:
        action = "status"
    write(action, entity=label, entity_id=instance.pk, changes=changes, reason=reason)
    instance._audit_reason = ""


def connect():
    for label in TRACKED:
        model = apps.get_model(label)
        pre_save.connect(_before, sender=model, dispatch_uid=f"audit_pre_{label}")
        post_save.connect(_after, sender=model, dispatch_uid=f"audit_post_{label}")
