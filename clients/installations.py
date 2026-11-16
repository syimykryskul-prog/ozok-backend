"""Установки (P7): статусы, чек-лист, каналы OnAI, акт, старт подписки, начисление внедренцу (C10)."""
from core import reference_ky
from django.utils.translation import gettext
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.exceptions import BusinessError
from core.models import q2, today
from core.settings_registry import get_int

from . import subscriptions as sub_svc
from .models import (ChecklistTemplate, ClientSale, Implementation, ImplementerAccrual, InstallationComment,
                     Subscription)

STATUSES = ("assigned", "in_progress", "training", "awaiting_act", "done", "cancelled")
NEXT = {"assigned": ("in_progress", "cancelled"), "in_progress": ("training", "awaiting_act", "cancelled"),
        "training": ("in_progress", "awaiting_act", "cancelled"), "awaiting_act": ("training", "done", "cancelled"),
        "done": (), "cancelled": ()}
STATUS_STAGE = {"assigned": "contract", "in_progress": "setup", "training": "training", "awaiting_act": "training",
                "done": "launched", "cancelled": "cancelled"}
STAGE_STATUS = {"contract": "assigned", "setup": "in_progress", "training": "training", "launched": "done",
                "cancelled": "cancelled"}
CHANNEL_STATUSES = ("pending", "connected", "failed")


def checklist_for(product):
    tpl = ChecklistTemplate.objects.filter(product=product, is_active=True).order_by("-id").first()
    items = reference_ky.normalize_items(tpl.items if tpl else [])
    return [{"item": i["ru"], "item_ky": i["ky"], "done": False, "done_at": None, "comment": ""} for i in items]


def channels_for(product, on=None):
    from network.commission import split_rule_for
    rule = split_rule_for(product.id, on or today())
    return [{"channel": c, "status": "pending"} for c in (rule.technical_channels if rule else [])]


def create_for_deal(deal, user=None):
    """Продажа с установкой/обучением подтверждена -> установка (assigned) с чек-листом и каналами."""
    if deal.client_id is None:
        return None
    if not deal.lines.filter(kind__in=("setup", "training")).exists():
        return None
    existing = Implementation.objects.filter(deal=deal).first()
    if existing:
        return existing
    from .branches import ensure_main_branch
    branch = deal.branch or ensure_main_branch(deal.client)
    return Implementation.objects.create(
        franchisee_id=deal.partner_id, client=deal.client,  # partner None — прямая продажа OZOK
         product=deal.product, deal=deal, branch=branch,
        started_at=deal.date, deadline=deal.date + timedelta(days=get_int("installations.default_deadline_days")),
        planned_launch=deal.date + timedelta(days=get_int("installations.default_deadline_days")),
        checklist=checklist_for(deal.product), technical_channels=channels_for(deal.product, deal.date))


def _sync(inst, status):
    inst.status = status
    inst.stage = STATUS_STAGE[status]
    if status == "done" and not inst.launched_at:
        inst.launched_at = today()


@transaction.atomic
def set_status(inst, status, user=None):
    if status not in STATUSES:
        raise BusinessError(gettext("Неизвестный статус установки."))
    if status == inst.status:
        raise BusinessError(gettext("Установка уже в этом статусе."))
    if status not in NEXT[inst.status]:
        raise BusinessError(gettext("Недопустимый переход статуса установки."))
    if status == "in_progress" and inst.implementer_id is None and inst.responsible_id is None:
        raise BusinessError(gettext("Назначьте внедренца."))
    if status == "done":
        if inst.act_confirmed_at is None:
            raise BusinessError(gettext("Завершить можно только после подтверждения акта клиентом."))
        if inst.training_result == "needs_repeat":
            raise BusinessError(gettext("Обучение нужно повторить: завершение невозможно."))
    _sync(inst, status)
    inst.save()
    if status == "done":
        start_subscription(inst)
        maybe_accrue(inst)
    return inst


def start_subscription(inst):
    """§9.1: установка завершена и акт подтверждён -> подписка филиала стартует (если оплачено — active)."""
    qs = Subscription.objects.filter(client=inst.client, product=inst.product).exclude(status="closed")
    if inst.branch_id:
        qs = qs.filter(branch=inst.branch)
    for sub in qs:
        if sub.started_at is None:
            sub.started_at = today()
            sub.save(update_fields=["started_at", "updated_at"])
        sub_svc.refresh_status(sub)
    return qs


def update_checklist(inst, items):
    cur = list(inst.checklist or [])
    by_name = {c["item"]: c for c in cur}
    for i in items:
        name = i.get("item")
        row = by_name.get(name)
        if row is None:
            raise BusinessError(gettext('Пункта «{name}» нет в чек-листе.').format(name=name))
        if "done" in i and bool(i["done"]) != row["done"]:
            row["done"] = bool(i["done"])
            row["done_at"] = timezone.now().isoformat() if row["done"] else None
        if "comment" in i:
            row["comment"] = str(i["comment"])[:300]
    inst.checklist = cur
    inst.save(update_fields=["checklist", "updated_at"])
    return inst


@transaction.atomic
def set_channel(inst, channel, status):
    if status not in CHANNEL_STATUSES:
        raise BusinessError(gettext("Статус канала: pending, connected или failed."))
    chans = list(inst.technical_channels or [])
    row = next((c for c in chans if c["channel"] == channel), None)
    if row is None:
        raise BusinessError(gettext("Такого канала нет у установки."))
    row["status"] = status
    inst.technical_channels = chans
    inst.save(update_fields=["technical_channels", "updated_at"])
    maybe_accrue(inst)
    return inst


def technical_status(inst):
    chans = inst.technical_channels or []
    if not chans:
        return "n/a"
    sts = {c["status"] for c in chans}
    return "failed" if "failed" in sts else ("connected" if sts == {"connected"} else "pending")


def set_training_result(inst, result):
    if result not in ("passed", "needs_repeat"):
        raise BusinessError(gettext("Результат обучения: passed или needs_repeat."))
    inst.training_result = result
    inst.save(update_fields=["training_result", "updated_at"])
    return inst


@transaction.atomic
def confirm_act(inst, *, confirmed_by, act_file="", user=None):
    if not (confirmed_by or "").strip():
        raise BusinessError(gettext("Укажите, кто из клиента подтвердил акт."))
    if inst.status == "cancelled":
        raise BusinessError(gettext("Установка отменена."))
    inst.client_confirmed_by = confirmed_by.strip()
    inst.act_file = act_file or inst.act_file
    inst.act_confirmed_at = timezone.now()
    inst.save()
    maybe_accrue(inst)
    return inst


def rate(inst, rating):
    if not isinstance(rating, int) or not 1 <= rating <= 5:
        raise BusinessError(gettext("Оценка клиента — от 1 до 5."))
    inst.client_rating = rating
    inst.save(update_fields=["client_rating", "updated_at"])
    return inst


def sold_amount(inst, rule):
    if inst.deal_id:
        s = sum((x.amount for x in ClientSale.objects.filter(deal_id=inst.deal_id, kind="setup", amount__gt=0,
                                                              refund_of__isnull=True)), Decimal("0"))
        if s > 0:
            return q2(s)
    return q2(rule.total)


def maybe_accrue(inst):
    """C10: начисление внедренцу — только когда все каналы connected и акт подтверждён клиентом."""
    from network.commission import split_amounts, split_rule_for
    if getattr(inst, "implementer_accrual", None) is not None or inst.implementer_id is None:
        return None
    if ImplementerAccrual.objects.filter(installation=inst).exists():
        return None
    rule = split_rule_for(inst.product_id, inst.started_at)
    if rule is None or inst.act_confirmed_at is None or inst.status == "cancelled":
        return None
    chans = inst.technical_channels or []
    if any(c["status"] != "connected" for c in chans):
        return None
    sold = sold_amount(inst, rule)
    amount = split_amounts(rule, sold)["implementer"]
    if amount <= 0:
        return None
    return ImplementerAccrual.objects.create(installation=inst, user_id=inst.implementer_id, amount=amount,
                                             split_rule=rule, sold_amount=sold, status="accrued")


def add_comment(inst, user, text):
    if not (text or "").strip():
        raise BusinessError(gettext("Введите комментарий."))
    return InstallationComment.objects.create(installation=inst, author=user, text=text.strip())


def verify_accrual(acc, user):
    if acc.status != "accrued":
        raise BusinessError(gettext("Проверить можно только начисленное."))
    acc.status, acc.verified_by, acc.verified_at = "verified", user, timezone.now()
    acc.save()
    return acc


def pay_accrual(acc, user, payment_ref=""):
    if acc.status != "verified":
        raise BusinessError(gettext("Выплатить можно только проверенное начисление."))
    if acc.verified_by_id == user.id:
        raise BusinessError(gettext("Проверяющий и выплачивающий должны быть разными людьми."))
    acc.status, acc.paid_by, acc.paid_at, acc.payment_ref = "paid", user, timezone.now(), payment_ref or ""
    acc.save()
    from billing import cash
    cash.on_implementer_paid(acc)
    return acc
