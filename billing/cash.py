"""Касса OZOK (P0+ п.6): автопроводки по денежным событиям, ручные записи, остатки, отчёт."""
from core.l10n import loc
from django.utils.translation import gettext
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from core.exceptions import BusinessError
from core.models import add_months, month_first, month_label, month_last, q2, today
from core.settings_registry import get_setting

from .models import CashAccount, CashCategory, CashTransaction, NbsExpense

ZERO = Decimal("0.00")
T = CashTransaction


def category(code):
    return CashCategory.objects.filter(code=code).first()


def pick_account(kind):
    qs = CashAccount.objects.filter(is_active=True)
    return qs.filter(kind=kind).order_by("id").first() or qs.order_by("id").first()


def account_for_payment_source(source):
    kind = (get_setting("cash.payment_source_account") or {}).get(source, "cash")
    return pick_account(kind)


def existing(source_type, source_id, reversal=False):
    qs = T.objects.filter(source_type=source_type, source_id=source_id)
    return qs.filter(reversal_of__isnull=not reversal).first()


def post(*, source_type, source_id, direction, amount, category_code, account, date=None, partner_id=None,
         client_id=None, comment="", user=None, attachment=""):
    """Автопроводка по событию; идемпотентна по (source_type, source_id). Нет счёта — проводка пропускается."""
    amount = q2(amount)
    if amount <= 0 or account is None:
        return None
    found = existing(source_type, source_id)
    if found is not None:
        return found
    cat = category(category_code)
    return T.objects.create(date=date or today(), account=account, direction=direction, amount=amount, category=cat,
                            partner_id=partner_id, client_id=client_id, source_type=source_type,
                            source_id=source_id, comment=comment, is_auto=True, created_by=user,
                            attachment=attachment)


def reverse(orig, *, reason="", user=None, amount=None, date=None, source_type=None, source_id=None):
    """Сторнирующая проводка: обратное направление; для частичного возврата — amount."""
    if orig is None:
        return None
    if orig.reversal_of_id:
        raise BusinessError(gettext("Нельзя сторнировать сторнирующую проводку."))
    if orig.direction == T.Direction.TRANSFER:
        # обратный перевод
        acc, to = orig.transfer_account, orig.account
        direction = T.Direction.TRANSFER
    else:
        acc, to = orig.account, None
        direction = T.Direction.OUT if orig.direction == T.Direction.IN else T.Direction.IN
    kwargs = {}
    if amount is not None:
        # частичный возврат — отдельная запись без связи reversal_of (связь one-to-one только для полной отмены)
        return T.objects.create(
            date=date or today(), account=acc, transfer_account=to, direction=direction, amount=q2(amount),
            category=orig.category, partner=orig.partner, client=orig.client, source_type=source_type or "refund",
            source_id=source_id, comment=(gettext('Возврат: {reason}').format(reason=reason) if reason else gettext("Возврат"))[:500], is_auto=True,
            created_by=user)
    return T.objects.create(
        date=date or orig.date, account=acc, transfer_account=to, direction=direction, amount=orig.amount,
        category=orig.category, partner=orig.partner, client=orig.client, source_type=orig.source_type,
        source_id=orig.source_id, reversal_of=orig, comment=(gettext('Сторно: {reason}').format(reason=reason) if reason else gettext("Сторно"))[:500],
        is_auto=orig.is_auto, created_by=user, **kwargs)


def reverse_source(source_type, source_id, *, reason="", user=None):
    orig = existing(source_type, source_id)
    if orig is None or existing(source_type, source_id, reversal=True) is not None:
        return None
    return reverse(orig, reason=reason, user=user)


# ---- события ---------------------------------------------------------------------------------------------------

def on_payment_confirmed(payment):
    return post(source_type="payment", source_id=payment.id, direction="in", amount=payment.amount,
                category_code="subscription", account=account_for_payment_source(payment.source),
                date=payment.paid_date, client_id=payment.client_id, user=payment.created_by,
                comment=gettext('Абонплата {business_name}').format(business_name=payment.client.business_name))


def on_payment_cancelled(payment, reason, user):
    return reverse_source("payment", payment.id, reason=reason, user=user)


def on_payment_refunded(payment, amount, refund_sale_id, user=None):
    """Возврат клиенту (полный или частичный): отдельная проводка-возврат на сумму возврата."""
    orig = existing("payment", payment.id)
    if orig is None or T.objects.filter(source_type="payment_refund", source_id=refund_sale_id).exists():
        return None
    return reverse(orig, reason=gettext("Возврат платежа клиента"), user=user, amount=amount,
                   source_type="payment_refund", source_id=refund_sale_id)


CHARGE_CATEGORY = {"fee": "franchise_fee", "installment": "franchise_fee", "nbs_lead_share": "nbs_lead_share",
                   "withhold": "withholdings", "other": "other_income"}


def franchisee_payment_category(p):
    """Статья кассы платежа франчайзи — не угадывается (D6): явная статья платежа → оплачиваемое начисление →
    договор → «Прочие поступления»."""
    if p.income_category:
        return p.income_category
    if p.charge_id:
        return CHARGE_CATEGORY.get(p.charge.kind, "other_income")
    if p.contract_id:
        return "franchise_fee"
    return "other_income"


def on_franchisee_payment(p):
    if p.method == "offset" or p.amount <= 0 or p.cancelled_at is not None:
        return None
    account = pick_account("cash" if p.method == "cash" else "bank")
    return post(source_type="franchisee_payment", source_id=p.id, direction="in", amount=p.amount,
                category_code=franchisee_payment_category(p), account=account, date=p.date,
                partner_id=p.franchisee_id, comment=p.comment or gettext("Платёж франчайзи"), user=p.created_by)


def on_franchisee_payment_cancelled(p):
    return reverse_source("franchisee_payment", p.id, reason=p.cancel_reason, user=p.cancelled_by)


def on_legacy_payout(po):
    if po.method == "offset" or po.amount <= 0:
        return None
    return post(source_type="commission_payout_manual", source_id=po.id, direction="out", amount=po.amount,
                category_code="commission_payouts", account=pick_account("cash" if po.method == "cash" else "bank"),
                date=po.date, partner_id=po.franchisee_id, comment=po.comment or gettext("Выплата вознаграждения"),
                user=po.created_by)


def on_legacy_payout_cancelled(po):
    return reverse_source("commission_payout_manual", po.id, reason=po.cancel_reason, user=po.cancelled_by)


def on_direct_deal(deal):
    if deal.partner_id is not None or deal.total <= 0:
        return None
    return post(source_type="deal", source_id=deal.id, direction="in", amount=deal.total,
                category_code="direct_sales", account=pick_account("cash"), date=deal.date,
                client_id=deal.client_id, comment=gettext('Прямая продажа OZOK №{id}').format(id=deal.id), user=deal.created_by)


def on_direct_deal_cancelled(deal, reason, user):
    return reverse_source("deal", deal.id, reason=reason, user=user)


def on_payout_paid(payout):
    return post(source_type="payout", source_id=payout.id, direction="out", amount=payout.amount,
                category_code="commission_payouts", account=pick_account("bank"), date=payout.paid_date,
                partner_id=payout.partner_id, comment=payout.payment_ref or gettext("Выплата комиссий"), user=payout.paid_by)


def on_reward_paid(reward):
    return post(source_type="recruiter_reward", source_id=reward.id, direction="out", amount=reward.paid_amount,
                category_code="recruiter_rewards", account=pick_account("bank"), date=reward.paid_date,
                partner_id=reward.partner_id, comment=reward.payment_ref or gettext("Вознаграждение рекрутера"),
                user=reward.paid_by)


def on_implementer_paid(acc):
    return post(source_type="implementer_accrual", source_id=acc.id, direction="out", amount=acc.amount,
                category_code="implementer_pay", account=pick_account("bank"), comment=acc.payment_ref or
                gettext("Оплата внедренцу"), user=acc.paid_by)


# ---- ручные записи ---------------------------------------------------------------------------------------------

@transaction.atomic
def manual_entry(*, user, direction, amount, account, date=None, category_obj=None, transfer_account=None,
                 partner=None, comment="", attachment="", source_type="manual"):
    amount = q2(amount)
    if amount <= 0:
        raise BusinessError(gettext("Сумма должна быть больше нуля."))
    if not account.is_active:
        raise BusinessError(gettext("Счёт отключён."))
    when = date or today()
    if when > today():
        raise BusinessError(gettext("Дата проводки не может быть в будущем."))
    for acc in (account, transfer_account):
        if acc is not None and when < acc.opening_date:
            raise BusinessError(gettext('Дата раньше даты начального остатка счёта «{name}» ({opening_date}).').format(name=acc.name, opening_date=acc.opening_date))
    if direction in ("out", "transfer") and not get_setting("cash.allow_negative"):
        # остаток не должен уходить в минус ни на дату проводки, ни на сегодня (M-2)
        if balance(account, on=when) - amount < 0 or balance(account) - amount < 0:
            raise BusinessError(gettext('Недостаточно средств на счёте «{name}»: остаток уйдёт в минус.').format(name=account.name))
    if direction == "transfer":
        if transfer_account is None or transfer_account.id == account.id:
            raise BusinessError(gettext("Для перевода укажите другой счёт назначения."))
        category_obj = None
    else:
        if category_obj is None:
            raise BusinessError(gettext("Укажите статью."))
        if category_obj.direction != direction:
            raise BusinessError(gettext("Статья не соответствует направлению проводки."))
        if not category_obj.manual_allowed or not category_obj.is_active:
            raise BusinessError(gettext("Эта статья ведётся автоматически: вручную доступны «Прочие поступления», "
                                "расходы OZOK и переводы."))
    return T.objects.create(
        date=date or today(), account=account, transfer_account=transfer_account, direction=direction,
        amount=amount, category=category_obj, partner=partner, comment=comment, attachment=attachment,
        source_type=source_type, created_by=user, is_auto=False)


@transaction.atomic
def add_expense(*, user, amount, account, category_obj, date=None, description="", supplier="", attachment=""):
    tx = manual_entry(user=user, direction="out", amount=amount, account=account, date=date,
                      category_obj=category_obj, comment=description or supplier, attachment=attachment,
                      source_type="nbs_expense")
    exp = NbsExpense.objects.create(transaction=tx, description=description, supplier=supplier)
    return exp


@transaction.atomic
def cancel_entry(tx, user, reason):
    reason = (reason or "").strip()
    if not reason:
        raise BusinessError(gettext("Укажите причину отмены."))
    if tx.reversal_of_id:
        raise BusinessError(gettext("Это сторнирующая проводка: отменять её нельзя."))
    if T.objects.filter(reversal_of=tx).exists():
        raise BusinessError(gettext("Проводка уже отменена."))
    return reverse(tx, reason=reason, user=user)


# ---- остатки и отчёт -------------------------------------------------------------------------------------------

def _delta(tx, account_id):
    """Влияние проводки на остаток счёта."""
    if tx.direction == T.Direction.IN:
        return tx.amount if tx.account_id == account_id else ZERO
    if tx.direction == T.Direction.OUT:
        return -tx.amount if tx.account_id == account_id else ZERO
    d = ZERO
    if tx.account_id == account_id:
        d -= tx.amount
    if tx.transfer_account_id == account_id:
        d += tx.amount
    return d


def balance(account, on=None):
    qs = T.objects.filter(Q(account=account) | Q(transfer_account=account), date__gte=account.opening_date)
    if on is not None:
        qs = qs.filter(date__lte=on)
    return q2(account.opening_balance + sum((_delta(t, account.id) for t in qs), ZERO))


def balance_before(account, first):
    from datetime import timedelta
    return balance(account, first - timedelta(days=1)) if first > account.opening_date else q2(account.opening_balance)


REFUND_SOURCES = ("payment_refund", "refund")


def effective(t):
    """(направление, сумма со знаком) для отчёта (D2): сторно и возврат клиенту гасят исходный поток — в его
    направлении и статье (а не считаются новым встречным потоком)."""
    if t.direction not in ("in", "out"):
        return t.direction, t.amount
    if t.reversal_of_id or t.source_type in REFUND_SOURCES:
        return ("out" if t.direction == "in" else "in"), -t.amount
    return t.direction, t.amount


def cash_report(date_from, date_to):
    txs = [t for t in T.objects.filter(date__gte=date_from, date__lte=date_to).select_related("category", "account")
           if t.date >= t.account.opening_date]  # как и остатки: до даты начального остатка движения не считаются
    eff = {t.id: effective(t) for t in txs}
    accounts = []
    for a in CashAccount.objects.all():
        mine = [t for t in txs if t.account_id == a.id or t.transfer_account_id == a.id]
        inn = sum((eff[t.id][1] for t in mine if eff[t.id][0] == "in" and t.account_id == a.id), ZERO)
        out = sum((eff[t.id][1] for t in mine if eff[t.id][0] == "out" and t.account_id == a.id), ZERO)
        tin = sum((t.amount for t in mine if t.direction == "transfer" and t.transfer_account_id == a.id), ZERO)
        tout = sum((t.amount for t in mine if t.direction == "transfer" and t.account_id == a.id), ZERO)
        accounts.append({"id": a.id, "name": a.name, "kind": a.kind, "is_active": a.is_active,
                         "opening": str(balance_before(a, date_from)), "closing": str(balance(a, date_to)),
                         "in": str(q2(inn)), "out": str(q2(out)), "transfer_in": str(q2(tin)),
                         "transfer_out": str(q2(tout))})
    by_cat = {"in": {}, "out": {}}
    for t in txs:
        d, amt = eff[t.id]
        if d in ("in", "out") and t.category_id:
            row = by_cat[d].setdefault(t.category_id, {"category": t.category_id, "code": t.category.code,
                                                       "name": loc(t.category, "name"), "amount": ZERO})
            row["amount"] += amt
    for d in by_cat:  # статья, полностью погашённая сторно, в отчёте не показывается
        by_cat[d] = {k: v for k, v in by_cat[d].items() if v["amount"] != 0}
    months, cur = {}, month_first(date_from)
    while cur <= date_to:
        months[month_label(cur)] = {"month": month_label(cur), "in": ZERO, "out": ZERO}
        cur = add_months(cur, 1)
    for t in txs:
        d, amt = eff[t.id]
        if d in ("in", "out"):
            months[month_label(t.date)][d] += amt
    total_in = sum((eff[t.id][1] for t in txs if eff[t.id][0] == "in"), ZERO)
    total_out = sum((eff[t.id][1] for t in txs if eff[t.id][0] == "out"), ZERO)
    fmt = lambda rows: [{**r, "amount": str(q2(r["amount"]))} for r in sorted(rows, key=lambda r: -r["amount"])]  # noqa: E731
    return {
        "period": {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}, "accounts": accounts,
        "opening_total": str(q2(sum((q2(a["opening"]) for a in accounts), ZERO))),
        "closing_total": str(q2(sum((q2(a["closing"]) for a in accounts), ZERO))),
        "total_in": str(q2(total_in)), "total_out": str(q2(total_out)), "net": str(q2(total_in - total_out)),
        "in_by_category": fmt(by_cat["in"].values()), "out_by_category": fmt(by_cat["out"].values()),
        "by_month": [{"month": m["month"], "in": str(q2(m["in"])), "out": str(q2(m["out"])),
                      "net": str(q2(m["in"] - m["out"]))} for m in months.values()],
    }
