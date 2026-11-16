"""Оплаты абонплаты (P9): Payment — каноническая запись; ClientSale выводится 1:1 (C6).
Источник оплат на продукт — Product.payment_source (C7): остальные источники только сверяют."""
from django.utils.translation import gettext
import csv
import hashlib
import hmac
import io
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from clients import branches as branch_svc
from clients import subscriptions as sub_svc
from clients.models import Branch, ClientSale, Subscription
from core.exceptions import BusinessError
from core.models import add_months, q2

from .models import Payment, PaymentImport

SUB_KINDS = ("subscription", "renewal")
AUTHORITY = {"elqr": {"elqr"}, "product": {"product"}, "manual": {"manual", "bank_import"}}


class NotAuthoritative(BusinessError):
    pass


class DuplicatePayment(BusinessError):
    pass


def is_authoritative(product, source):
    return source in AUTHORITY.get(product.payment_source, set())


def to_aware(value):
    if isinstance(value, datetime):
        dt = value
    else:
        dt = parse_datetime(str(value)) if value else None
        if dt is None:
            d = parse_date(str(value)) if value else None
            if d is None:
                raise BusinessError(gettext("Некорректная дата оплаты (paid_at)."))
            dt = datetime.combine(d, time(12, 0))
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


def resolve_subscription(branch, product=None, tariff=None):
    product = product or branch.product
    if product is None:
        subs = list(branch.subscriptions.exclude(status="closed"))
        if len(subs) == 1:
            return subs[0]
        raise BusinessError(gettext("У филиала не указан продукт: укажите подписку (subscription)."))
    sub = Subscription.objects.filter(branch=branch, product=product).exclude(status="closed").first()
    if sub is None:
        sub = Subscription.objects.create(
            client=branch.client, branch=branch, product=product, tariff=tariff or branch.tariff,
            status=Subscription.Status.AWAITING_PAYMENT, connected_at=timezone.localdate())
    return sub


def find_existing(source, external_id, product=None):
    if not external_id:
        return None
    qs = Payment.all_objects.filter(source=source, external_id=external_id)
    return (qs.filter(product=product) if product is not None else qs).first()


def reconcile(branch, *, external_id, amount, paid_date, source):
    """Сверка неавторитетного источника: находит уже принятую оплату и отмечает её сверенной (платёж не создаётся)."""
    qs = Payment.objects.filter(branch=branch, status=Payment.Status.CONFIRMED)
    pay = (qs.filter(external_id=external_id).first() if external_id else None)
    if pay is None:
        cand = [p for p in qs.filter(amount=q2(amount)) if abs((p.paid_date - paid_date).days) <= 1]
        pay = cand[0] if cand else None
    if pay is not None and pay.reconciled_at is None:
        pay.reconciled_at, pay.reconciled_source = timezone.now(), source
        pay.save(update_fields=["reconciled_at", "reconciled_source", "updated_at"])
    return pay


@transaction.atomic
def register_payment(*, branch, amount, paid_at, source, external_id="", months=1, period_from=None, period_to=None,
                     purpose="", raw=None, user=None, subscription=None, tariff=None, list_amount=None,
                     discount_amount=None, paid_until=None, elqr_account="", check_authority=True):
    """Принять оплату: Payment → ClientSale 1:1 → CommissionAccrual (получатель — на дату оплаты, C8)."""
    from clients.services import create_client_sale
    amount = q2(amount)
    if amount <= 0:
        raise BusinessError(gettext("Сумма оплаты должна быть больше нуля."))
    months = int(months or 1)
    if not 1 <= months <= 120:
        raise BusinessError(gettext("Количество месяцев должно быть от 1 до 120."))
    if source not in Payment.Source.values:
        raise BusinessError(gettext("Неизвестный источник оплаты."))
    paid_at = to_aware(paid_at)
    paid_date = timezone.localtime(paid_at).date()
    sub = subscription or resolve_subscription(branch, tariff=tariff)
    product = sub.product
    if sub.branch_id != branch.id:
        raise BusinessError(gettext("Подписка относится к другому филиалу."))
    if check_authority and not is_authoritative(product, source):
        raise NotAuthoritative(
            gettext('Источник абонплаты продукта «{name}» — {v}: остальные источники только сверяют и платёж не создают (C7).').format(name=product.name, v=product.get_payment_source_display()))
    external_id = (external_id or "").strip()
    if external_id and find_existing(source, external_id, product):
        raise DuplicatePayment(gettext("Оплата с таким идентификатором операции уже есть."))
    if paid_until is not None:
        period_to = paid_until
        period_from = period_from or add_months(paid_until, -months) + timedelta(days=1)
    elif period_from is None or period_to is None:
        if sub.paid_until and sub.paid_until >= paid_date:
            start = sub.paid_until + timedelta(days=1)
        else:
            start = paid_date
        period_from = period_from or start
        period_to = period_to or add_months(period_from, months) - timedelta(days=1)
    payee, why = branch_svc.payee_on(branch, paid_date)
    scope = branch_svc.scope_partner_on(branch, paid_date)
    payment = Payment.objects.create(
        branch=branch, subscription=sub, client=branch.client, product=product, deal=sub.deal, amount=amount,
        paid_at=paid_at, period_from=period_from, period_to=period_to, months=months, source=source,
        external_id=external_id, elqr_account=elqr_account or branch.elqr_account or "", purpose=purpose or "",
        raw=raw or {}, created_by=user)
    first = not Payment.objects.filter(subscription=sub, status=Payment.Status.CONFIRMED).exclude(
        pk=payment.pk).exists() and not ClientSale.objects.filter(
        client=branch.client, product=product, amount__gt=0, kind__in=SUB_KINDS).exists()
    sale, warning = create_client_sale(
        client=branch.client, product=product, kind="subscription" if first else "renewal", amount=amount,
        date=paid_date, paid_until=period_to, comment=purpose or gettext("Оплата абонплаты"),
        source=ClientSale.Source.INTEGRATION if source == "product" else ClientSale.Source.MANUAL,
        external_payment_id=external_id if source == "product" and external_id else None, months=months,
        created_by=user, tariff=tariff or sub.tariff, list_amount=list_amount, discount_amount=discount_amount,
        payment=payment, franchisee=scope, payee=payee, no_payee_reason=why, branch=branch,
        lead=branch.effective_lead, use_client_lead=False)
    if payment.sale_id != sale.id:
        payment.sale = sale
        payment.save(update_fields=["sale", "updated_at"])
    update_subscription(sub, payment, tariff=tariff, discount=sale)
    refresh_deal_payment_status(payment)
    payment._warning = warning
    return payment, warning


def update_subscription(sub, payment, tariff=None, discount=None):
    if payment.period_to and (sub.paid_until is None or payment.period_to > sub.paid_until):
        sub.paid_until = payment.period_to
    sub.monthly_fee = q2(payment.amount / max(payment.months, 1))
    if tariff is not None:
        sub.tariff = tariff
    if discount is not None and discount.list_amount > 0:
        sub.discount_percent = min(q2(discount.discount_amount * 100 / discount.list_amount), Decimal("100"))
    sub.started_at = sub.started_at or payment.paid_date
    if sub.status == Subscription.Status.CLOSED:
        sub.status = Subscription.Status.ACTIVE
        sub.closed_at = None
    sub.save()
    sub_svc.refresh_status(sub)
    branch = sub.branch
    if branch is not None and branch.status in (Branch.Status.PENDING, Branch.Status.SUSPENDED):
        branch.status = Branch.Status.ACTIVE
        branch.save(update_fields=["status", "updated_at"])
    return sub


def rebuild_paid_until(sub):
    """После отмены/полного возврата: paid_until = максимум period_to по оставшимся подтверждённым оплатам."""
    ends = [p.period_to for p in Payment.objects.filter(subscription=sub, status=Payment.Status.CONFIRMED)
            if p.period_to]
    sub.paid_until = max(ends) if ends else None
    sub.save(update_fields=["paid_until", "updated_at"])
    sub_svc.refresh_status(sub)


def on_sale_cancelled(sale, user, reason):
    """Продажа абонплаты отменена -> производная оплата отменяется, подписка пересчитывается."""
    payment = Payment.all_objects.filter(sale=sale).first()
    if payment is None or payment.cancelled_at is not None:
        return
    payment.status = Payment.Status.CANCELLED
    payment.mark_cancelled(user, reason)
    from . import cash
    cash.on_payment_cancelled(payment, reason, user)
    if payment.subscription_id:
        rebuild_paid_until(payment.subscription)
    refresh_deal_payment_status(payment)


def cancel_payment(payment, user, reason):
    from clients.services import cancel_sale
    reason = (reason or "").strip()
    if not reason:
        raise BusinessError(gettext("Укажите причину отмены."))
    if payment.cancelled_at is not None:
        raise BusinessError(gettext("Оплата уже отменена."))
    with transaction.atomic():
        if payment.sale_id:
            cancel_sale(payment.sale, user, reason)
            payment.refresh_from_db()
        if payment.cancelled_at is None:
            payment.status = Payment.Status.CANCELLED
            payment.mark_cancelled(user, reason)
    return payment


def apply_refund(original_sale, amount, refund_sale=None):
    """Возврат платежа клиента: учёт возвращённой суммы в Payment (полный возврат -> refunded)."""
    payment = Payment.all_objects.filter(sale=original_sale).first()
    if payment is None:
        return None
    from . import cash
    cash.on_payment_refunded(payment, amount, refund_sale.id if refund_sale is not None else None)
    payment.refunded_amount += q2(amount)
    if payment.refunded_amount >= payment.amount:
        payment.status = Payment.Status.REFUNDED
        payment.save()
        if payment.subscription_id:
            rebuild_paid_until(payment.subscription)
    else:
        payment.save(update_fields=["refunded_amount", "updated_at"])
    refresh_deal_payment_status(payment)
    return payment


def refresh_deal_payment_status(payment):
    if payment.deal_id:
        from clients.deals import refresh_payment_status
        refresh_payment_status(payment.deal)


# ---- импорт банковской выписки (CSV) -------------------------------------------------------------------------

COLUMNS = {
    "date": ("date", "дата", "дата операции", "paid_at"),
    "amount": ("amount", "сумма"),
    "purpose": ("purpose", "назначение", "назначение платежа"),
    "operation_id": ("operation_id", "идентификатор операции", "id операции", "operation", "id"),
    "account": ("account", "elqr_account", "лицевой счет", "лицевой счёт", "счет", "счёт"),
}


def parse_csv(text):
    text = (text or "").lstrip("﻿")
    if not text.strip():
        raise BusinessError(gettext("Файл пустой."))
    sample = text[:2048]
    delim = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    header = [h.strip().lower() for h in next(reader)]
    idx = {}
    for key, names in COLUMNS.items():
        for n in names:
            if n in header:
                idx[key] = header.index(n)
                break
    missing = [k for k in ("date", "amount", "operation_id", "account") if k not in idx]
    if missing:
        raise BusinessError(gettext("В файле нет колонок: ") + ", ".join(missing) + ".")
    rows = []
    for i, rec in enumerate(reader, start=2):
        if not any(c.strip() for c in rec):
            continue
        rows.append({"line": i, **{k: (rec[j].strip() if j < len(rec) else "") for k, j in idx.items()}})
    if not rows:
        raise BusinessError(gettext("В файле нет строк с операциями."))
    return rows


def _classify(row):
    """Сопоставление строки выписки: elqr_account + идентификатор операции."""
    out = {"line": row["line"], "account": row["account"], "operation_id": row["operation_id"],
           "purpose": row.get("purpose", ""), "branch": None, "status": "invalid", "message": ""}
    try:
        amount = q2(Decimal(row["amount"].replace(" ", "").replace(",", ".")))
        if amount <= 0:
            raise InvalidOperation
        d = parse_date(row["date"][:10]) or datetime.strptime(row["date"][:10], "%d.%m.%Y").date()
    except (InvalidOperation, ValueError, TypeError, AttributeError):
        out["message"] = gettext("Некорректная сумма или дата.")
        return out
    out.update(amount=str(amount), date=d.isoformat())
    if not row["operation_id"]:
        out["message"] = gettext("Нет идентификатора операции.")
        return out
    branch = Branch.objects.filter(elqr_account=row["account"]).select_related("product", "client").first()
    if branch is None:
        out.update(status="not_found", message=gettext("Лицевой счёт не найден."))
        return out
    out["branch"] = branch.id
    product = branch.product or (branch.subscriptions.first().product if branch.subscriptions.exists() else None)
    if product is None:
        out.update(status="invalid", message=gettext("У филиала не указан продукт."))
        return out
    dup = None if not is_authoritative(product, "bank_import") else         Payment.all_objects.filter(external_id=row["operation_id"], branch=branch).first()
    if dup is not None:
        out.update(status="duplicate", message=gettext('Уже есть оплата №{id}.').format(id=dup.id), payment=dup.id)
        return out
    if not is_authoritative(product, "bank_import"):
        pay = reconcile_preview(branch, row["operation_id"], amount, d)
        if pay is not None:
            out.update(status="reconcile", message=gettext('Совпадает с оплатой №{id} (источник {source}).').format(id=pay.id, source=pay.source),
                       payment=pay.id)
        else:
            out.update(status="not_authoritative", message=(
                gettext('Источник абонплаты продукта — {v}: выписка только сверяет, оплата не создаётся.').format(v=product.get_payment_source_display())))
        return out
    out.update(status="new", message=gettext("Будет создана оплата."), product=product.id)
    return out


def reconcile_preview(branch, external_id, amount, paid_date):
    qs = Payment.objects.filter(branch=branch, status=Payment.Status.CONFIRMED)
    pay = qs.filter(external_id=external_id).first()
    if pay is None:
        cand = [p for p in qs.filter(amount=amount) if abs((p.paid_date - paid_date).days) <= 1]
        pay = cand[0] if cand else None
    return pay


def import_preview(text):
    rows = [_classify(r) for r in parse_csv(text)]
    seen, summary = set(), {}
    for r in rows:
        key = (r["account"], r["operation_id"])
        if r["status"] == "new" and key in seen:
            r.update(status="duplicate", message=gettext("Дубль внутри файла."))
        seen.add(key)
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    return {"rows": rows, "summary": summary}


def months_for(branch, amount):
    """Сколько месяцев покрывает сумма: по цене тарифа на дату, иначе 1."""
    from network.commission import price_on
    tariff = branch.tariff
    price = price_on(tariff) if tariff else None
    if price and price > 0:
        n = int((amount / price).to_integral_value(rounding="ROUND_HALF_UP"))
        if n >= 1 and abs(price * n - amount) <= price / 2:
            return n
    return 1


@transaction.atomic
def import_confirm(text, user, filename=""):
    preview = import_preview(text)
    created = 0
    for r in preview["rows"]:
        if r["status"] == "new":
            branch = Branch.objects.get(pk=r["branch"])
            amount = q2(r["amount"])
            d = parse_date(r["date"])
            pay, _ = register_payment(
                branch=branch, amount=amount, paid_at=datetime.combine(d, time(12, 0)),
                source=Payment.Source.BANK_IMPORT, external_id=r["operation_id"], months=months_for(branch, amount),
                purpose=r["purpose"], raw={"line": r["line"], "import": True}, user=user,
                elqr_account=r["account"], check_authority=False)
            r.update(status="created", payment=pay.id)
            created += 1
        elif r["status"] == "reconcile":
            pay = Payment.objects.filter(pk=r["payment"]).first()
            if pay is not None and pay.reconciled_at is None:
                pay.reconciled_at, pay.reconciled_source = timezone.now(), "bank_import"
                pay.save(update_fields=["reconciled_at", "reconciled_source", "updated_at"])
            r["status"] = "reconciled"
    summary = {}
    for r in preview["rows"]:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    summary["matched"] = summary.get("created", 0) + summary.get("reconciled", 0)
    PaymentImport.objects.create(filename=filename, created_by=user, summary=summary, rows=preview["rows"])
    return {"summary": summary, "rows": preview["rows"]}


# ---- подпись вебхука Элкюар -------------------------------------------------------------------------------------

def sign(secret, body):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret, body, header):
    if not secret or not header:
        return False
    return hmac.compare_digest(sign(secret, body), header.strip().lower().replace("sha256=", ""))
