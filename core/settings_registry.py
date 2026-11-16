"""Реестр настроек OZOK (P0). Значения здесь — только начальные; меняются в разделе «Настройки».

Код читает параметры только через get_setting(key): сначала строка SystemSetting, иначе значение по умолчанию.
Суммы — в сомах (KGS), решение заказчика.
"""
from django.utils.translation import gettext, gettext_lazy
from decimal import Decimal, InvalidOperation

from .exceptions import BusinessError

# key: (default, type, group, label, is_finance)
_D = [
    # --- франшиза и рекрутер
    ("franchise.cash_amount", "87500", "decimal", "franchise", gettext_lazy("Франшиза «Сразу», сом (≈ $1 000)"), True),
    ("franchise.installment_max_amount", "175000", "decimal", "franchise", gettext_lazy("Франшиза «Рассрочка до», сом (≈ $2 000)"), True),
    ("franchise.default_withhold_percent", "30", "decimal", "franchise", gettext_lazy("Удержание из дохода по умолчанию, %"), True),
    ("recruiter.reward_amount", "13125", "decimal", "franchise", gettext_lazy("Вознаграждение рекрутеру, сом (≈ $150)"), True),
    # --- лиды OZOK
    ("nbs_lead.subscription_percent", "15", "decimal", "commissions", gettext_lazy("Лид OZOK: доля франчайзи с абонплаты, %"), True),
    ("nbs_lead.nbs_share_percent", "30", "decimal", "commissions", gettext_lazy("Лид OZOK: доля OZOK от дохода франчайзи, %"), True),
    ("nbs_lead.share_due_days", 30, "int", "terms", gettext_lazy("Срок оплаты доли OZOK по лидам OZOK, дней"), True),
    ("withhold.due_days", 30, "int", "terms", gettext_lazy("Срок оплаты удержаний с продаж, дней"), True),
    # --- сроки и SLA
    ("leads.overdue_hours", 24, "int", "terms", gettext_lazy("Срок реакции на заявку, часов"), False),
    ("subscriptions.grace_days", 5, "int", "terms", gettext_lazy("Льготный период подписки по умолчанию, дней"), False),
    ("subscriptions.restrict_after_days", 30, "int", "terms", gettext_lazy("Ограничение подписки через N дней просрочки (по умолчанию)"), False),
    ("installations.default_deadline_days", 14, "int", "terms", gettext_lazy("Срок установки по умолчанию, дней"), False),
    ("partner.second_branch_response_days", 3, "int", "terms", gettext_lazy("Срок ответа по второму филиалу, дней"), False),
    ("funnel.stuck_days", 14, "int", "terms", gettext_lazy("Кандидат «застрял» на этапе, дней"), False),
    ("payments.reminder_days_before", 3, "int", "terms", gettext_lazy("Напоминание об оплате за N дней до срока"), False),
    ("payments.reminder_days_after", 3, "int", "terms", gettext_lazy("Напоминание об оплате через N дней после срока"), False),
    ("payments.split_prepaid_by_month", False, "bool", "commissions", gettext_lazy("Делить многомесячную оплату по месяцам"), True),
    # --- цены и согласования
    ("pricing.max_discount_percent", "10", "decimal", "pricing", gettext_lazy("Допустимая скидка по умолчанию, %"), False),
    ("approvals.roles", ["super_admin", "network_head"], "json", "approvals",
     gettext_lazy("Роли, одобряющие скидки ниже минимума и изменения лидов"), False),
    ("cash.allow_negative", False, "bool", "cash", gettext_lazy("Разрешить уход остатка счёта в минус при расходе"), True),
    ("cash.payment_source_account", {"elqr": "elqr", "product": "elqr", "manual": "cash", "bank_import": "bank"},
     "json", "cash", gettext_lazy("Счёт кассы для оплат абонплаты по источнику (вид счёта)"), True),
    # --- рейтинг / Growth
    ("rating.standard_marketing_percent", "50", "decimal", "growth", gettext_lazy("Блок «Маркетинг» для Standard, %"), False),
]
DEFAULTS = {k: {"value": v, "type": t, "group": g, "label": lbl, "is_finance": fin} for k, v, t, g, lbl, fin in _D}


def _coerce(type_, value):
    try:
        if type_ == "decimal":
            return str(Decimal(str(value)))
        if type_ == "int":
            if isinstance(value, bool):
                raise ValueError
            return int(value)
        if type_ == "bool":
            if not isinstance(value, bool):
                raise ValueError
            return value
        if type_ == "json":
            return value
        return str(value)
    except (ValueError, InvalidOperation, TypeError):
        raise BusinessError(gettext("Неверное значение настройки."))


def get_setting(key):
    from .models import SystemSetting
    row = SystemSetting.objects.filter(key=key).first()
    if row is not None:
        return row.value
    if key not in DEFAULTS:
        raise KeyError(key)
    return DEFAULTS[key]["value"]


def get_decimal(key):
    return Decimal(str(get_setting(key)))


def get_int(key):
    return int(get_setting(key))


def all_settings():
    from .models import SystemSetting
    rows = {r.key: r for r in SystemSetting.objects.all()}
    out = []
    for key, meta in DEFAULTS.items():
        row = rows.get(key)
        out.append({"key": key, "value": row.value if row else meta["value"], "type": meta["type"],
                    "label": meta["label"], "group": meta["group"], "is_finance": meta["is_finance"],
                    "is_default": row is None, "updated_at": row.updated_at if row else None,
                    "updated_by": row.updated_by_id if row else None})
    return out


def set_setting(key, value, *, user, reason):
    from .models import SystemSetting
    if key not in DEFAULTS:
        raise BusinessError(gettext("Неизвестная настройка."))
    if not (reason or "").strip():
        raise BusinessError(gettext("Укажите причину изменения."))
    meta = DEFAULTS[key]
    value = _coerce(meta["type"], value)
    validate_value(key, value)
    row = SystemSetting.objects.filter(key=key).first()
    if row is None:
        row = SystemSetting(key=key, value=meta["value"], type=meta["type"], label=str(meta["label"]),
                            group=meta["group"], is_finance=meta["is_finance"])
        row.save()  # фиксируем исходное значение, чтобы аудит показал [старое, новое]
    row.value = value
    row.updated_by = user
    row._audit_reason = reason.strip()
    row.save()
    _after_change(key, user, reason.strip())
    return row


def _after_change(key, user, reason):
    """Настройки, которые порождают данные: проценты лидов OZOK -> версии правил; суммы франшизы -> предложения."""
    if key.startswith("nbs_lead.") and key.endswith("_percent"):
        from network.services import apply_nbs_lead_rules
        apply_nbs_lead_rules(user=user, reason=reason)
    elif key in ("franchise.cash_amount", "franchise.installment_max_amount"):
        from network.services import apply_franchise_offers
        apply_franchise_offers()


PERCENT_KEYS = {"franchise.default_withhold_percent", "nbs_lead.subscription_percent", "nbs_lead.nbs_share_percent",
                "pricing.max_discount_percent", "rating.standard_marketing_percent"}
MIN_ONE_KEYS = {"nbs_lead.share_due_days", "withhold.due_days", "leads.overdue_hours",
                "subscriptions.restrict_after_days", "installations.default_deadline_days",
                "partner.second_branch_response_days", "funnel.stuck_days"}
NON_NEGATIVE_KEYS = {"subscriptions.grace_days", "payments.reminder_days_before", "payments.reminder_days_after"}
POSITIVE_AMOUNT_KEYS = {"franchise.cash_amount", "franchise.installment_max_amount", "recruiter.reward_amount"}


def validate_value(key, value):
    """M-5: значения настроек проверяются по ключам; BusinessError с русским текстом."""
    if key in PERCENT_KEYS and not 0 <= Decimal(str(value)) <= 100:
        raise BusinessError(gettext("Процент должен быть от 0 до 100."))
    if key in MIN_ONE_KEYS and int(value) < 1:
        raise BusinessError(gettext("Значение должно быть не меньше 1."))
    if key in NON_NEGATIVE_KEYS and int(value) < 0:
        raise BusinessError(gettext("Значение не может быть отрицательным."))
    if key in POSITIVE_AMOUNT_KEYS and Decimal(str(value)) <= 0:
        raise BusinessError(gettext("Сумма должна быть больше нуля."))
    if key == "franchise.installment_max_amount" and Decimal(str(value)) < get_decimal("franchise.cash_amount"):
        raise BusinessError(gettext("«Рассрочка до» не может быть меньше суммы «Сразу»."))
    if key == "approvals.roles":
        from . import permissions as P
        allowed = {r for r in P.NBS_ROLES if r in P.ACTION_ROLES[("deals", "approve")]}
        if not isinstance(value, list) or not value or not set(value) <= allowed:
            raise BusinessError(gettext("Одобрять могут только роли OZOK: ") + ", ".join(sorted(allowed)) + ".")
    if key == "cash.payment_source_account":
        from billing.models import CashAccount
        from billing.payments import AUTHORITY
        sources = {s for ss in AUTHORITY.values() for s in ss} | set(AUTHORITY)
        if not isinstance(value, dict) or not set(value) <= sources:
            raise BusinessError(gettext("Укажите счета по источникам: ") + ", ".join(sorted(sources)) + ".")
        for src, kind in value.items():
            if not CashAccount.objects.filter(kind=kind, is_active=True).exists():
                raise BusinessError(gettext('Для источника «{src}» нет активного счёта вида «{kind}».').format(src=src, kind=kind))
