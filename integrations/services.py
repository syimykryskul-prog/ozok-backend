"""Интеграция с продуктами (раздел 5Б): приём и обработка событий, привязка нераспознанных.

Формат события (плоский JSON): {"event_id", "type" (или "event_type"), ...поля типа}.
Вся логика — здесь; её используют и API (`POST integrations/events/`), и сидер, и simulate_integration.
Каждое событие изолировано: ошибка одного не прерывает пакет и попадает в журнал со статусом `error`.
"""
from django.utils.translation import gettext
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from clients.models import Client, ClientProductAccount, ClientSale
from clients.services import create_client_sale, is_fully_refunded, refund_sale
from core.exceptions import BusinessError
from core.models import q2
from network.commission import price_on
from network.models import Franchisee, Tariff

from .models import IntegrationEvent

MAX_BATCH = 100
EVENT_TYPES = ("client.upsert", "payment.succeeded", "payment.refunded")


class Unmatched(Exception):
    """Событие не удалось привязать (неизвестный партнёр/клиент/платёж)."""


class Duplicate(Exception):
    pass


class EventError(Exception):
    pass


# ---- разбор значений -------------------------------------------------------

def _required(p, name):
    value = p.get(name)
    if value in (None, ""):
        raise EventError(gettext('Не указано поле {name}.').format(name=name))
    return value


def _date(value, name):
    if isinstance(value, datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    if isinstance(value, date):
        return value
    try:
        d = parse_date(str(value)[:10]) if value else None
    except (ValueError, TypeError):
        d = None
    if d is None:
        raise EventError(gettext('Некорректная дата в поле {name}.').format(name=name))
    return d


def _datetime(value):
    if value in (None, ""):
        return timezone.now()
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = parse_datetime(str(value))
            if dt is None:
                d = parse_date(str(value))
                if d is None:
                    raise ValueError
                dt = datetime(d.year, d.month, d.day, 12, 0)
        except (ValueError, TypeError):
            raise EventError(gettext("Некорректная дата и время в поле paid_at."))
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


def _amount(value, name="amount"):
    try:
        return q2(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        raise EventError(gettext('Некорректная сумма в поле {name}.').format(name=name))


# ---- обработчики типов -----------------------------------------------------

def _account(product, external_id):
    return (ClientProductAccount.objects.select_related("client").filter(product=product, external_id=external_id)
            .first())


def handle_client_upsert(product, p, franchisee=None, client=None):
    ext = str(_required(p, "external_client_id"))
    account = _account(product, ext)
    fields = {k: str(p[k]) for k in ("business_name", "business_type", "contact_name", "phone", "city")
              if p.get(k) not in (None, "")}
    if fields.get("business_type") not in Client.BusinessType.values:
        fields.pop("business_type", None)
    if account is None:
        if client is None:
            franchisee = franchisee or Franchisee.objects.filter(
                partner_code__iexact=str(p.get("partner_code") or "").strip()).first()
            if franchisee is None:
                raise Unmatched(f"Партнёр с кодом «{p.get('partner_code') or ''}» не найден.")
            if franchisee.status in ("closed", "suspended"):
                # M-7: клиентов закрытого/приостановленного партнёра не создаём — передаёт OZOK вручную
                raise Unmatched(f"Партнёр «{franchisee.name}» {franchisee.get_status_display().lower()}: "
                                "клиент передан OZOK на ручное сопоставление.")
            if not fields.get("business_name"):
                raise EventError(gettext("Не указано поле business_name."))
            client = Client.objects.create(franchisee=franchisee, **fields)
        account = ClientProductAccount(client=client, product=product, external_id=ext)
    else:
        client = account.client
        for k, v in fields.items():
            setattr(client, k, v)
        client.save()
    status = {"blocked": "restricted"}.get(p.get("status"), p.get("status"))  # blocked — прежнее имя restricted
    if status in ClientProductAccount.Status.values and status != "awaiting_payment":
        account.status = status
    if p.get("connected_at"):
        account.connected_at = _date(p["connected_at"], "connected_at")
    account.save()
    return client


def handle_payment_succeeded(product, p, franchisee=None, client=None):
    from billing import payments
    ext_pay = str(_required(p, "external_payment_id"))
    ext_client = str(_required(p, "external_client_id"))
    if (ClientSale.objects.filter(product=product, external_payment_id=ext_pay).exists()
            or payments.find_existing("product", ext_pay, product)):
        raise Duplicate()
    amount = _amount(_required(p, "amount"))
    if amount <= 0:
        raise EventError(gettext("Сумма платежа должна быть больше нуля."))
    try:
        months = int(_required(p, "months"))
    except (TypeError, ValueError):
        raise EventError(gettext("Некорректное количество месяцев."))
    if not 1 <= months <= 120:
        raise EventError(gettext("Количество месяцев должно быть от 1 до 120."))
    paid_until = _date(_required(p, "paid_until"), "paid_until")
    paid_at = _datetime(p.get("paid_at"))
    paid_date = timezone.localtime(paid_at).date()
    list_amount = _amount(p["list_amount"], "list_amount") if p.get("list_amount") not in (None, "") else None
    discount_amount = (_amount(p["discount_amount"], "discount_amount")
                       if p.get("discount_amount") not in (None, "") else None)
    warnings = []
    tariff = None
    if p.get("tariff_code") not in (None, ""):
        tariff = Tariff.objects.filter(product=product, code=str(p["tariff_code"])).first()
        if tariff is None:
            warnings.append(f"Тариф «{p['tariff_code']}» не найден у продукта: платёж принят без тарифа.")
    account = _account(product, ext_client)
    if account is None:
        if client is None:
            raise Unmatched(f"Клиент {ext_client} неизвестен: сначала нужно событие client.upsert "
                            "или привязка клиента вручную.")
        account = ClientProductAccount.objects.create(client=client, product=product, external_id=ext_client)
    computed = (q2(price_on(tariff, paid_date) * months) if tariff is not None and price_on(tariff, paid_date)
                else None)
    if tariff is not None and list_amount is None and discount_amount is None and computed:
        list_amount = computed
    elif list_amount is not None and computed is not None and list_amount != computed:
        # M4: значение события сохраняем, расхождение с прайсом — предупреждение
        warnings.append(f"list_amount события ({list_amount}) отличается от расчётного ({computed}): "
                        "сохранено значение события.")
    branch = account.branch
    if not payments.is_authoritative(product, "product"):
        pay = payments.reconcile(branch, external_id=ext_pay, amount=amount, paid_date=paid_date, source="product")
        warnings.append("Оплата не создана: источник абонплаты продукта — "
                        f"{product.get_payment_source_display()}; событие продукта только сверяет"
                        + (f" (сверено с оплатой №{pay.id})." if pay else gettext(" (совпадений нет).")))
        return account.client, "; ".join(warnings)
    payment, warning = payments.register_payment(
        branch=branch, subscription=account, amount=amount, paid_at=paid_at, source="product", external_id=ext_pay,
        months=months, paid_until=paid_until, purpose=gettext("Оплата из продукта"), raw=p, tariff=tariff,
        list_amount=list_amount, discount_amount=discount_amount, check_authority=False)
    if warning:
        warnings.append(warning)
    return account.client, "; ".join(warnings)

def _rollback_paid_until(product, original):
    """Полный возврат: paid_until аккаунта = максимум по остальным не возвращённым оплатам."""
    account = ClientProductAccount.objects.filter(client=original.client, product=product).first()
    if account is None:
        return
    others = ClientSale.objects.filter(
        client=original.client, product=product, source=ClientSale.Source.INTEGRATION, amount__gt=0,
        kind__in=ClientSale.COMMISSION_KINDS, paid_until__isnull=False).exclude(pk=original.pk)
    dates = [s.paid_until for s in others if not is_fully_refunded(s)]
    account.paid_until = max(dates) if dates else None
    account.save(update_fields=["paid_until", "updated_at"])


def handle_payment_refunded(product, p, franchisee=None, client=None):
    """Возврат (возможно частичный). Ключ дедупликации — external_refund_id, иначе event_id."""
    ext_pay = str(_required(p, "external_payment_id"))
    amount = _amount(_required(p, "amount"))
    refund_id = str(p.get("external_refund_id") or p.get("event_id") or "")
    if not refund_id:
        raise EventError(gettext("Не указан event_id или external_refund_id."))
    key = f"{ext_pay}:refund:{refund_id}"
    if ClientSale.objects.filter(product=product, external_payment_id=key).exists():
        raise Duplicate()
    original = ClientSale.objects.filter(product=product, external_payment_id=ext_pay).first()
    if original is None and product.payment_source != "product":
        return None, gettext('Возврат не обработан: источник абонплаты продукта — {v}.').format(v=product.get_payment_source_display())
    if original is None:
        raise Unmatched(gettext('Платёж {ext_pay} не найден, возврат не к чему привязать.').format(ext_pay=ext_pay))
    try:
        refund_sale(original, amount, date=timezone.localdate(), external_payment_id=key)
    except BusinessError as e:
        raise EventError(str(e.detail))
    original = ClientSale.objects.get(pk=original.pk)
    if is_fully_refunded(original) and original.months:
        _rollback_paid_until(product, original)
    return original.client


HANDLERS = {
    "client.upsert": handle_client_upsert,
    "payment.succeeded": handle_payment_succeeded,
    "payment.refunded": handle_payment_refunded,
}


def _run(product, event_type, payload, franchisee=None, client=None):
    """Выполнить обработчик. Возвращает (status, error_text, client)."""
    handler = HANDLERS.get(event_type)
    if handler is None:
        return IntegrationEvent.Status.ERROR, gettext('Неизвестный тип события «{event_type}».').format(event_type=event_type), None
    try:
        with transaction.atomic():
            out = handler(product, payload, franchisee=franchisee, client=client)
    except Unmatched as e:
        return IntegrationEvent.Status.UNMATCHED, str(e), None
    except Duplicate:
        return IntegrationEvent.Status.DUPLICATE, "", None
    except (EventError, BusinessError) as e:
        return IntegrationEvent.Status.ERROR, str(getattr(e, "detail", e)), None
    except Exception as e:  # noqa: BLE001 — одно событие не должно ронять весь пакет
        return IntegrationEvent.Status.ERROR, f"Не удалось обработать событие: {type(e).__name__}", None
    warning = ""
    if isinstance(out, tuple):
        out, warning = out
    return IntegrationEvent.Status.PROCESSED, warning or "", out


def process_event(product, data):
    """Принять одно событие: журнал + обработка. Возвращает IntegrationEvent."""
    now = timezone.now()
    if not isinstance(data, dict):
        return IntegrationEvent.objects.create(product=product, event_type="", payload={"raw": str(data)[:500]},
                                               status="error", error=gettext("Событие должно быть JSON-объектом."),
                                               processed_at=now)
    payload = dict(data)
    if isinstance(payload.get("data"), dict):
        payload = {**payload, **payload["data"]}
    event_type = str(payload.get("type") or payload.get("event_type") or "")
    event_id = str(payload.get("event_id") or "")
    event = IntegrationEvent(product=product, event_type=event_type, external_id=event_id, payload=data,
                             received_at=now)
    if not event_id:
        status, error, client = "error", gettext("Не указан event_id."), None
    elif IntegrationEvent.objects.filter(product=product, external_id=event_id,
                                         status__in=("processed", "unmatched")).exists():
        status, error, client = "duplicate", "", None
    else:
        status, error, client = _run(product, event_type, payload)
    event.status, event.error, event.client = status, error, client
    event.franchisee_id = client.franchisee_id if client else None
    event.processed_at = timezone.now()
    event.save()
    return event


def process_events(product, events):
    """Пакет событий (список). Возвращает список результатов для ответа API."""
    results = []
    for data in events:
        event = process_event(product, data)
        # у processed в error может быть предупреждение (например, неизвестный tariff_code)
        results.append({"event_id": event.external_id or None, "status": event.status,
                        "error": event.error if event.status != "duplicate" and event.error else None})
    product.last_event_at = timezone.now()
    product.save(update_fields=["last_event_at", "updated_at"])
    return results


def resolve_event(event, franchisee, client=None):
    """Привязать нераспознанное событие к франчайзи/клиенту и обработать заново."""
    if event.status != IntegrationEvent.Status.UNMATCHED:
        raise BusinessError(gettext("Привязать можно только нераспознанное событие."))
    if client is not None and client.franchisee_id != franchisee.id:
        raise BusinessError(gettext("Клиент принадлежит другому франчайзи."))
    payload = dict(event.payload)
    if isinstance(payload.get("data"), dict):
        payload = {**payload, **payload["data"]}
    status, error, found = _run(event.product, event.event_type, payload, franchisee=franchisee, client=client)
    if status in (IntegrationEvent.Status.UNMATCHED, IntegrationEvent.Status.ERROR):
        message = error
        if status == IntegrationEvent.Status.UNMATCHED and event.event_type == "payment.succeeded":
            message = gettext("Для платежа укажите клиента франчайзи.")
        raise BusinessError(message)
    event.status, event.error = status, error
    event.client = found
    event.franchisee = franchisee
    event.processed_at = timezone.now()
    event.save()
    return event




# ---- Элкюар (каркас, P9) ---------------------------------------------------------------------------------------

def _elqr_succeeded(p):
    from billing import payments
    from clients.models import Branch
    account = str(_required(p, "account")).strip()
    op_id = str(_required(p, "operation_id"))
    amount = _amount(_required(p, "amount"))
    paid_at = _datetime(p.get("paid_at"))
    branch = Branch.objects.filter(elqr_account=account).select_related("client", "product").first()
    if branch is None:
        raise Unmatched(gettext('Лицевой счёт {account} не привязан ни к одному филиалу.').format(account=account))
    sub = payments.resolve_subscription(branch)
    product = sub.product
    months = int(p.get("months") or payments.months_for(branch, amount))
    paid_date = timezone.localtime(paid_at).date()
    if not payments.is_authoritative(product, "elqr"):
        pay = payments.reconcile(branch, external_id=op_id, amount=amount, paid_date=paid_date, source="elqr")
        return branch.client, ("Оплата не создана: источник абонплаты продукта — "
                               f"{product.get_payment_source_display()}; Элкюар только сверяет"
                               + (f" (сверено с оплатой №{pay.id})." if pay else gettext(" (совпадений нет).")))
    try:
        payments.register_payment(branch=branch, subscription=sub, amount=amount, paid_at=paid_at, source="elqr",
                                  external_id=op_id, months=months, purpose=str(p.get("purpose") or gettext("Элкюар")),
                                  raw=p, elqr_account=account, check_authority=False)
    except payments.DuplicatePayment:
        raise Duplicate()
    return branch.client, ""


def _elqr_refunded(p):
    from billing.models import Payment
    op_id = str(_required(p, "operation_id"))
    amount = _amount(_required(p, "amount"))
    pay = Payment.objects.filter(source="elqr", external_id=op_id).first()
    if pay is None or pay.sale_id is None:
        raise Unmatched(gettext('Оплата Элкюар {op_id} не найдена: возврат не к чему привязать.').format(op_id=op_id))
    key = f"refund:{p.get('refund_id') or p.get('event_id')}"
    if ClientSale.objects.filter(refund_of=pay.sale, external_payment_id=key).exists():
        raise Duplicate()
    try:
        refund_sale(pay.sale, amount, date=timezone.localdate(), external_payment_id=key)
    except BusinessError as e:
        raise EventError(str(e.detail))
    return pay.client, ""


ELQR_HANDLERS = {"payment.succeeded": _elqr_succeeded, "payment.refunded": _elqr_refunded}


def _run_elqr(payload):
    handler = ELQR_HANDLERS.get(str(payload.get("type") or ""))
    if handler is None:
        return IntegrationEvent.Status.ERROR, gettext('Неизвестный тип события «{v}».').format(v=payload.get('type')), None
    try:
        with transaction.atomic():
            client, warning = handler(payload)
    except Unmatched as e:
        return IntegrationEvent.Status.UNMATCHED, str(e), None
    except Duplicate:
        return IntegrationEvent.Status.DUPLICATE, "", None
    except (EventError, BusinessError) as e:
        return IntegrationEvent.Status.ERROR, str(getattr(e, "detail", e)), None
    except Exception as e:  # noqa: BLE001
        return IntegrationEvent.Status.ERROR, f"Не удалось обработать событие: {type(e).__name__}", None
    return IntegrationEvent.Status.PROCESSED, warning, client


def process_elqr_event(data):
    """Вебхук Элкюар: журнал + дедупликация по event_id; платёж — по operation_id."""
    from clients.models import Branch
    now = timezone.now()
    payload = dict(data) if isinstance(data, dict) else {}
    event_id = str(payload.get("event_id") or "")
    event = IntegrationEvent(provider="elqr", event_type=str(payload.get("type") or ""), external_id=event_id,
                             payload=data if isinstance(data, dict) else {"raw": str(data)[:500]}, received_at=now)
    branch = Branch.objects.filter(elqr_account=str(payload.get("account") or "")).select_related("product").first()
    event.product = branch.product if branch is not None else None
    if not event_id:
        status, error, client = "error", gettext("Не указан event_id."), None
    elif IntegrationEvent.objects.filter(provider="elqr", external_id=event_id,
                                         status__in=("processed", "unmatched")).exists():
        status, error, client = "duplicate", "", None
    else:
        status, error, client = _run_elqr(payload)
    event.status, event.error, event.client = status, error, client
    event.franchisee_id = client.franchisee_id if client else None
    event.processed_at = timezone.now()
    event.save()
    return event


def retry_event(event):
    """Повторная обработка события (error/unmatched) — после исправления данных (привязка счёта и т. п.)."""
    if event.status not in (IntegrationEvent.Status.ERROR, IntegrationEvent.Status.UNMATCHED):
        raise BusinessError(gettext("Повторить можно только событие со статусом «Ошибка» или «Не распознано»."))
    payload = dict(event.payload)
    if isinstance(payload.get("data"), dict):
        payload = {**payload, **payload["data"]}
    if event.provider == "elqr":
        status, error, client = _run_elqr(payload)
    else:
        if event.product is None:
            raise BusinessError(gettext("У события нет продукта."))
        status, error, client = _run(event.product, event.event_type, payload)
    event.status, event.error = status, error
    event.client = client or event.client
    event.franchisee_id = (client.franchisee_id if client else event.franchisee_id)
    event.processed_at = timezone.now()
    event.save()
    return event
