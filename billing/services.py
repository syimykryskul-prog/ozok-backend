"""Расчёты с франчайзи: балансы и просрочка (разделы 5, 5А; решения C2, C6)."""
from decimal import Decimal

from django.db.models import Sum

from core.models import q2, today as _today

from .models import Charge, CommissionAccrual, CommissionPayout, FranchiseePayment

ZERO = Decimal("0")
A = CommissionAccrual.Status


def _grouped(qs, field="amount"):
    return {r["franchisee_id"]: q2(r["s"] or ZERO) for r in qs.values("franchisee_id").annotate(s=Sum(field))}


def allocate_overdue(charges, offsets, manual, on):
    """C2: зачёты (offset) гасят только часть «из продаж» (начисления без срока); банк/касса — FIFO по сроку.
    Возвращает Σ неоплаченных остатков начислений со сроком < on (отрицательные начисления — кредит)."""
    rows = [{"amount": c.amount, "due": c.due_date, "left": c.amount} for c in charges]
    pos = [r for r in rows if r["amount"] > 0]
    off = offsets
    for r in sorted((r for r in pos if r["due"] is None), key=lambda r: 0):
        take = min(off, r["left"]) if off > 0 else ZERO
        r["left"] -= take
        off -= take
    pay = manual
    for r in sorted(pos, key=lambda r: (r["due"] is None, r["due"] or on)):
        take = min(pay, r["left"]) if pay > 0 else ZERO
        r["left"] -= take
        pay -= take
    due_sum = sum((r["left"] for r in pos if r["due"] is not None and r["due"] < on), ZERO)
    credit = sum((r["amount"] for r in rows if r["amount"] < 0 and r["due"] is not None and r["due"] < on), ZERO)
    return max(due_sum + credit, ZERO)


def commission_owed_map(partner_ids=None):
    """ЕДИНАЯ функция «ещё должны партнёру»: Σ (начислено − удержано − погашено) по начислениям accrued/in_payout."""
    flt = {} if partner_ids is None else {"partner_id__in": list(partner_ids)}
    out = {}
    for a in CommissionAccrual.objects.filter(status__in=(A.ACCRUED, A.IN_PAYOUT), **flt):
        out[a.partner_id] = out.get(a.partner_id, ZERO) + a.owed
    return out


def unallocated_credit(partner_ids=None):
    """Остаток старых ручных выплат, ещё не распределённый на начисления (D1/H-5)."""
    flt = {} if partner_ids is None else {"franchisee_id__in": list(partner_ids)}
    pflt = {} if partner_ids is None else {"partner_id__in": list(partner_ids)}
    legacy = _grouped(CommissionPayout.objects.filter(**flt).exclude(method="offset"))
    used = {r["partner_id"]: r["s"] or ZERO
            for r in CommissionAccrual.objects.filter(**pflt).values("partner_id").annotate(s=Sum("paid_amount"))}
    return {fid: max(legacy.get(fid, ZERO) - used.get(fid, ZERO), ZERO) for fid in legacy}


def commission_to_pay(partner_ids=None):
    """Итого к выплате = commission_owed − unallocated_credit; одна формула для карточки, сводки и дашборда."""
    owed, credit = commission_owed_map(partner_ids), unallocated_credit(partner_ids)
    return {pid: q2(owed.get(pid, ZERO)) - credit.get(pid, ZERO) for pid in set(owed) | set(credit)}


def finance_map(franchisee_ids=None, on=None):
    """{franchisee_id: {"balance", "overdue", "commission_due"}} — несколько запросов на всю сеть.

    balance = Σ начислений − Σ платежей (долг франчайзи центру);
    overdue = Σ неоплаченных остатков начислений со сроком < сегодня после распределения платежей (C2);
    commission_due = Σ (начислено − удержано − погашено) по начислениям accrued/in_payout − ещё не распределённый
    остаток старых ручных выплат (C6, D1; on_hold не в счёт).
    """
    on = on or _today()
    flt = {} if franchisee_ids is None else {"franchisee_id__in": list(franchisee_ids)}
    charges = _grouped(Charge.objects.filter(**flt))
    paid = _grouped(FranchiseePayment.objects.filter(**flt))
    pflt = {} if franchisee_ids is None else {"partner_id__in": list(franchisee_ids)}
    acc = commission_owed_map(franchisee_ids)
    payouts = unallocated_credit(franchisee_ids)
    if franchisee_ids is None:
        ids = set(charges) | set(paid) | set(acc) | set(payouts)
    else:
        ids = set(franchisee_ids)
    by_charges, by_off, by_manual = {}, {}, {}
    for c in Charge.objects.filter(**flt):
        by_charges.setdefault(c.franchisee_id, []).append(c)
    for p in FranchiseePayment.objects.filter(**flt):
        d = by_off if p.method == "offset" else by_manual
        d[p.franchisee_id] = d.get(p.franchisee_id, ZERO) + p.amount
    result = {}
    for fid in ids:
        result[fid] = {
            "balance": charges.get(fid, ZERO) - paid.get(fid, ZERO),
            "overdue": q2(allocate_overdue(by_charges.get(fid, []), by_off.get(fid, ZERO),
                                           by_manual.get(fid, ZERO), on)),
            "commission_due": q2(acc.get(fid, ZERO)) - payouts.get(fid, ZERO),
        }
    return result


def paid_commissions(first, last, partner_id=None):
    """Выплачено комиссий за период: пакеты выплат (paid_date) + старые ручные выплаты (date). Единая формула для
    дашборда, аналитики партнёра и Кассы."""
    from .models import Payout
    pays = Payout.objects.filter(status="paid", paid_date__gte=first, paid_date__lte=last)
    old = CommissionPayout.objects.filter(date__gte=first, date__lte=last).exclude(method="offset")
    if partner_id:
        pays, old = pays.filter(partner_id=partner_id), old.filter(franchisee_id=partner_id)
    total = (pays.aggregate(s=Sum("amount"))["s"] or ZERO) + (old.aggregate(s=Sum("amount"))["s"] or ZERO)
    return q2(total)


def franchisee_finance(franchisee_id, on=None):
    return finance_map([franchisee_id], on)[franchisee_id]
