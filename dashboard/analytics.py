"""Отчёт «Доходы и расходы» партнёра и аналитика по партнёру / сфере (P0+)."""
from core.l10n import loc
from django.utils.translation import gettext, gettext_lazy
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum

from billing.models import LedgerCategory, PartnerLedgerEntry
from billing.services import paid_commissions
from clients.models import Client, ClientProductAccount, ClientSale, Deal
from core.exceptions import BusinessError
from core.models import add_months, money, month_first, month_label, today
from leads.models import Lead
from network.models import Franchisee, Product

ZERO = Decimal("0")
NBS_DIRECT = gettext_lazy("OZOK (прямые продажи)")
KINDS = (("setup", gettext_lazy("Установка и обучение")), ("training", gettext_lazy("Обучение")), ("equipment", gettext_lazy("Оборудование")), ("subscription", gettext_lazy("Абонплата")),
         ("other", gettext_lazy("Прочее")))


def _kind_key(kind):
    return "subscription" if kind in ClientSale.COMMISSION_KINDS else kind


def _period(date_from, date_to):
    first = month_first(today())
    return date_from or first, date_to or today()


def _sales(partner_ids, d1, d2, product=None, lead_source=None):
    qs = ClientSale.objects.filter(date__gte=d1, date__lte=d2)
    if partner_ids is not None:
        qs = qs.filter(franchisee_id__in=partner_ids)
    if product is not None:
        qs = qs.filter(product=product)
    if lead_source:
        qs = qs.filter(lead_source=lead_source)
    return qs


def _by_kind(sales):
    acc = {k: {"count": 0, "amount": ZERO, "income": ZERO} for k, _ in KINDS}
    for kind, n, a, i in sales.values_list("kind").annotate(n=Count("id"), a=Sum("amount"), i=Sum("franchisee_income")):
        row = acc[_kind_key(kind)]
        row["count"] += n
        row["amount"] += a or ZERO
        row["income"] += i or ZERO
    return [{"kind": k, "label": label, "count": acc[k]["count"], "amount": money(acc[k]["amount"]),
             "income": money(acc[k]["income"])} for k, label in KINDS]


# ---- доходы и расходы -----------------------------------------------------------------------------

def ledger_report(partner_ids, d1, d2):
    """partner_ids — список id или None (все партнёры). Доходы от продаж берутся из продаж автоматически."""
    d1, d2 = _period(d1, d2)
    sales = _sales(partner_ids, d1, d2)
    entries = PartnerLedgerEntry.objects.filter(date__gte=d1, date__lte=d2)
    if partner_ids is not None:
        entries = entries.filter(partner_id__in=partner_ids)
    agg = sales.aggregate(income=Sum("franchisee_income"), withheld=Sum("withheld_amount"),
                          nbs=Sum("nbs_share_amount"))
    sales_income, withheld, nbs_share = agg["income"] or ZERO, agg["withheld"] or ZERO, agg["nbs"] or ZERO
    by_product = []
    names = {p.id: loc(p, "name") for p in Product.objects.all()}
    for r in sales.values("product_id").annotate(i=Sum("franchisee_income"), a=Sum("amount")):
        by_product.append({"product": r["product_id"], "name": names.get(r["product_id"], ""),
                           "amount": money(r["a"]), "income": money(r["i"])})
    other_income = entries.filter(type="income")
    expenses = entries.filter(type="expense")
    cats = {c.id: loc(c, "name") for c in LedgerCategory.objects.all()}
    marketing_ids = set(LedgerCategory.objects.filter(is_marketing=True).values_list("id", flat=True))

    def group(qs):
        return [{"category": r["category_id"], "name": cats.get(r["category_id"], ""), "amount": money(r["s"])}
                for r in qs.values("category_id").annotate(s=Sum("amount")).order_by("-s")]
    other_total = other_income.aggregate(s=Sum("amount"))["s"] or ZERO
    exp_total = expenses.aggregate(s=Sum("amount"))["s"] or ZERO
    marketing = expenses.filter(category_id__in=marketing_ids).aggregate(s=Sum("amount"))["s"] or ZERO
    profit = sales_income + other_total - exp_total - withheld - nbs_share
    months = {}
    start = month_first(d1)
    n_months = (d2.year - start.year) * 12 + d2.month - start.month + 1
    for i in range(min(max(n_months, 1), 24)):
        months[month_label(add_months(start, i))] = [ZERO, ZERO, ZERO]
    for d, inc, wh, nb in sales.values_list("date", "franchisee_income", "withheld_amount", "nbs_share_amount"):
        row = months.setdefault(month_label(d), [ZERO, ZERO, ZERO])
        row[0] += inc
        row[2] += wh + nb
    for d, t, amount in entries.values_list("date", "type", "amount"):
        row = months.setdefault(month_label(d), [ZERO, ZERO, ZERO])
        if t == "income":
            row[0] += amount
        else:
            row[1] += amount
    return {
        "period": {"date_from": d1.isoformat(), "date_to": d2.isoformat()},
        "sales_income": {"total": money(sales_income), "by_kind": _by_kind(sales), "by_product": by_product},
        "other_income": {"total": money(other_total), "by_category": group(other_income)},
        "expenses": {"total": money(exp_total), "by_category": group(expenses), "marketing": money(marketing)},
        "deductions": {"withheld": money(withheld), "nbs_share": money(nbs_share),
                       "total": money(withheld + nbs_share)},
        "profit": money(profit),
        "by_month": [{"month": m, "income": money(v[0]), "expenses": money(v[1]), "deductions": money(v[2]),
                      "profit": money(v[0] - v[1] - v[2])} for m, v in sorted(months.items())],
    }


# ---- аналитика партнёра по сферам -----------------------------------------------------------------

def _lead_stats(partner_id, product_code, d1, d2):
    qs = Lead.objects.filter(franchisee_id=partner_id, created_at__date__gte=d1, created_at__date__lte=d2)
    if product_code is not None:
        qs = qs.filter(product=product_code)
    by_status = {r["status"]: r["n"] for r in qs.values("status").annotate(n=Count("id"))}
    total = sum(by_status.values())
    paid = by_status.get("paid", 0)
    return {"total": total, "by_status": by_status, "paid": paid,
            "conversion_percent": money(Decimal(paid) * 100 / total) if total else money(0)}


def _subscriptions(partner_id, product):
    qs = ClientProductAccount.objects.filter(client__franchisee_id=partner_id, status="active")
    if product is not None:
        qs = qs.filter(product=product)
    on = today()
    return {"active": qs.count(), "overdue": qs.filter(paid_until__lt=on).count(),
            "expiring": qs.filter(paid_until__gte=on, paid_until__lte=on + timedelta(days=14)).count()}


def _sphere_block(partner_id, product, d1, d2):
    sales = _sales([partner_id], d1, d2, product)
    pos = sales.filter(amount__gt=0)
    agg = sales.aggregate(amt=Sum("amount"), income=Sum("franchisee_income"), comm=Sum("commission_amount"),
                          withheld=Sum("withheld_amount"), nbs=Sum("nbs_share_amount"),
                          discount=Sum("discount_amount"), n=Count("id", filter=Q(amount__gt=0)))
    sub_collected = sales.filter(kind__in=ClientSale.COMMISSION_KINDS).aggregate(s=Sum("amount"))["s"] or ZERO
    paid_out = paid_commissions(d1, d2, partner_id) if product is None else ZERO
    entries = PartnerLedgerEntry.objects.filter(partner_id=partner_id, date__gte=d1, date__lte=d2, product=product)
    exp = entries.filter(type="expense").aggregate(s=Sum("amount"))["s"] or ZERO
    oth = entries.filter(type="income").aggregate(s=Sum("amount"))["s"] or ZERO
    n = agg["n"] or 0
    revenue = agg["amt"] or ZERO
    return {
        "product": product.id, "code": product.code, "sphere": loc(product, "sphere"), "name": loc(product, "name"),
        "sales": {"count": n, "amount": money(revenue), "avg_check": money(revenue / n) if n else money(0),
                  "by_kind": _by_kind(sales), "discounts": money(agg["discount"])},
        "leads": _lead_stats(partner_id, product.code, d1, d2),
        "clients": Client.objects.filter(franchisee_id=partner_id).filter(
            Q(sales__product=product) | Q(accounts__product=product)).distinct().count(),
        "subscriptions": _subscriptions(partner_id, product),
        "subscription_collected": money(sub_collected),
        "commission": {"accrued": money(agg["comm"]), "withheld": money(agg["withheld"]),
                       "nbs_share": money(agg["nbs"])},
        "ledger": {"income": money(oth), "expenses": money(exp)},
        "income": money(agg["income"]),
        "_pos": pos.count(),
    }


def partner_analytics(partner, d1, d2):
    d1, d2 = _period(d1, d2)
    products = list(Product.objects.filter(is_active=True))
    blocks = []
    for p in products:
        b = _sphere_block(partner.id, p, d1, d2)
        b.pop("_pos")
        blocks.append(b)
    sales = _sales([partner.id], d1, d2)
    total = sales.aggregate(amt=Sum("amount"), n=Count("id", filter=Q(amount__gt=0)),
                            income=Sum("franchisee_income"), withheld=Sum("withheld_amount"),
                            nbs=Sum("nbs_share_amount"), discount=Sum("discount_amount"))
    deals = Deal.objects.filter(partner=partner, status="confirmed", date__gte=d1, date__lte=d2)
    ledger = ledger_report([partner.id], d1, d2)
    monthly = {}
    start = add_months(month_first(d2), -5)
    for d, amount in _sales([partner.id], start, d2).values_list("date", "amount"):
        monthly[month_label(d)] = monthly.get(month_label(d), ZERO) + amount
    net_ids = list(Franchisee.objects.filter(status="franchisee").values_list("id", flat=True))
    net = _sales(net_ids, d1, d2).aggregate(amt=Sum("amount"), n=Count("id", filter=Q(amount__gt=0)))
    k = len(net_ids) or 1
    from planning.services import rating_map
    from quality.models import Ticket
    rating = rating_map(month_first(d2)).get(partner.id)
    tickets = Ticket.objects.filter(franchisee=partner)
    return {
        "partner": {"id": partner.id, "name": partner.name, "status": partner.status, "tier": partner.tier,
                    "region": partner.region},
        "period": {"date_from": d1.isoformat(), "date_to": d2.isoformat()},
        "totals": {"sales_count": total["n"] or 0, "sales_amount": money(total["amt"]),
                   "income": money(total["income"]), "discounts": money(total["discount"]),
                   "withheld": money(total["withheld"]), "nbs_share": money(total["nbs"]),
                   "commission_accrued": money(_sales([partner.id], d1, d2).aggregate(s=Sum("commission_amount"))["s"]),
                   "commission_paid": money(paid_commissions(d1, d2, partner.id)),
                   "deals_confirmed": deals.count(), "deals_discount": money(deals.aggregate(s=Sum("discount_total"))["s"]),
                   "leads": _lead_stats(partner.id, None, d1, d2), "subscriptions": _subscriptions(partner.id, None),
                   "clients": Client.objects.filter(franchisee=partner, is_active=True).count(),
                   "profit": ledger["profit"], "expenses": ledger["expenses"]["total"]},
        "spheres": blocks,
        "by_month": [{"month": month_label(add_months(start, i)),
                      "revenue": money(monthly.get(month_label(add_months(start, i))))} for i in range(6)],
        "network_average": {"sales_count": money(Decimal(net["n"] or 0) / k), "sales_amount": money((net["amt"] or ZERO) / k),
                            "partners": len(net_ids)},
        "quality": {"rating": rating["rating"] if rating else None, "place": rating["place"] if rating else None,
                    "sla_breached": sum(1 for t in tickets.filter(status__in=("open", "in_progress"))
                                        if t.sla_breached),
                    "tickets_open": tickets.filter(status__in=("open", "in_progress")).count()},
    }


# ---- аналитика сферы: все партнёры + строка OZOK ------------------------------------------------------

def sphere_analytics(product, d1, d2, *, only_partner=None, region=None, status=None, tier=None,
                     lead_source=None):
    d1, d2 = _period(d1, d2)
    partners = Franchisee.objects.filter(status__in=("franchisee", "manager", "suspended"))
    if region:
        partners = partners.filter(region__iexact=region)
    if status:
        partners = partners.filter(status=status)
    if tier:
        partners = partners.filter(tier=tier)
    from planning.services import rating_map
    ratings = rating_map(month_first(d2))
    rows = []
    for p in partners.order_by("id"):
        sales = _sales([p.id], d1, d2, product, lead_source)
        agg = sales.aggregate(amt=Sum("amount"), n=Count("id", filter=Q(amount__gt=0)),
                              discount=Sum("discount_amount"))
        n, amount = agg["n"] or 0, agg["amt"] or ZERO
        sub = sales.filter(kind__in=ClientSale.COMMISSION_KINDS).aggregate(s=Sum("amount"))["s"] or ZERO
        subs = _subscriptions(p.id, product)
        leads = _lead_stats(p.id, product.code, d1, d2)
        r = ratings.get(p.id)
        rows.append({"partner": p.id, "name": p.name, "region": p.region, "status": p.status, "tier": p.tier,
                     "sales_count": n, "revenue": amount, "subscription_collected": sub,
                     "active_subscriptions": subs["active"], "overdue_subscriptions": subs["overdue"],
                     "conversion_percent": leads["conversion_percent"], "avg_check": (amount / n) if n else ZERO,
                     "discounts": agg["discount"] or ZERO, "rating": r["rating"] if r else None})
    rows.sort(key=lambda x: (-x["revenue"], x["name"]))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    direct = Deal.objects.filter(partner__isnull=True, status="confirmed", product=product, date__gte=d1,
                                 date__lte=d2)
    nbs_row = {"name": NBS_DIRECT, "sales_count": direct.count(),
               "revenue": direct.aggregate(s=Sum("total"))["s"] or ZERO,
               "discounts": direct.aggregate(s=Sum("discount_total"))["s"] or ZERO}

    def fmt(r):
        out = dict(r)
        for key in ("revenue", "subscription_collected", "avg_check", "discounts"):
            if key in out:
                out[key] = money(out[key])
        return out
    if only_partner is not None:  # партнёр видит только свою строку, место и число участников
        mine = next((r for r in rows if r["partner"] == only_partner), None)
        return {"product": {"id": product.id, "code": product.code, "name": loc(product, "name")},
                "period": {"date_from": d1.isoformat(), "date_to": d2.isoformat()},
                "participants": len(rows), "me": fmt(mine) if mine else None, "rows": [fmt(mine)] if mine else []}
    total_rev = sum((r["revenue"] for r in rows), ZERO) + nbs_row["revenue"]
    seg = ozok_clients_segment(d1, d2, product)  # собственные клиенты OZOK по сфере и доля OZOK vs партнёров
    return {"ozok_clients": seg, "ozok_share": seg["share"],
            **_sphere_payload(product, d1, d2, rows, fmt, nbs_row, total_rev, seg)}


def _sphere_payload(product, d1, d2, rows, fmt, nbs_row, total_rev, seg):
    return {"product": {"id": product.id, "code": product.code, "name": loc(product, "name")},
            "period": {"date_from": d1.isoformat(), "date_to": d2.isoformat()},
            "participants": len(rows), "rows": [fmt(r) for r in rows],
            "nbs_direct": {**nbs_row, "revenue": money(nbs_row["revenue"]), "discounts": money(nbs_row["discounts"])},
            "top": [fmt(r) for r in rows[:3]], "lagging": [fmt(r) for r in rows[-3:][::-1]] if len(rows) > 3 else [],
            "totals": {"revenue": money(total_rev), "partners_revenue": money(total_rev - nbs_row["revenue"]),
                       "nbs_direct_share_percent": money(nbs_row["revenue"] * 100 / total_rev) if total_rev else money(0)}}


def _own_payments(d1, d2, product=None):
    """Подтверждённые оплаты абонплаты клиентов OZOK (legacy + direct) за период: деньги 100% OZOK."""
    from billing.models import Payment
    qs = Payment.objects.filter(status="confirmed", client__origin__in=Client.OZOK_ORIGINS,
                                paid_at__date__gte=d1, paid_at__date__lte=d2)
    return qs.filter(product=product) if product is not None else qs


def own_collected(d1, d2, product=None):
    return sum((p.amount - p.refunded_amount for p in _own_payments(d1, d2, product)), ZERO)


def ozok_clients_segment(d1, d2, product=None):
    """«Собственные клиенты OZOK» (legacy + direct): число клиентов, подписки по статусам, MRR и собранная абонплата
    по сферам; плюс доля OZOK против партнёров по абонплате."""
    from billing.models import Payment
    from network.commission import price_on
    d1, d2 = _period(d1, d2)
    clients = Client.objects.filter(origin__in=Client.OZOK_ORIGINS, is_active=True)
    subs = ClientProductAccount.objects.filter(client__in=clients).select_related("product", "tariff")
    if product is not None:
        subs = subs.filter(product=product)
    by_status, by_sphere = {}, {}
    on = today()
    for s in subs:
        by_status[s.status] = by_status.get(s.status, 0) + 1
        row = by_sphere.setdefault(s.product_id, {"product": s.product_id, "code": s.product.code,
                                                  "name": loc(s.product, "name"), "sphere": loc(s.product, "sphere"),
                                                  "subscriptions": 0, "mrr": ZERO, "collected": ZERO})
        row["subscriptions"] += 1
        if s.status in ("active", "grace"):
            fee = s.monthly_fee if s.monthly_fee is not None else (price_on(s.tariff, on) if s.tariff_id else None)
            row["mrr"] += fee or ZERO
    for p in _own_payments(d1, d2, product):
        row = by_sphere.setdefault(p.product_id, {"product": p.product_id, "code": p.product.code,
                                                  "name": loc(p.product, "name"), "sphere": loc(p.product, "sphere"),
                                                  "subscriptions": 0, "mrr": ZERO, "collected": ZERO})
        row["collected"] += p.amount - p.refunded_amount
    spheres = sorted(by_sphere.values(), key=lambda r: r["name"])
    own = own_collected(d1, d2, product)
    partners_qs = Payment.objects.filter(status="confirmed", paid_at__date__gte=d1, paid_at__date__lte=d2) \
        .exclude(client__origin__in=Client.OZOK_ORIGINS)
    if product is not None:
        partners_qs = partners_qs.filter(product=product)
    partners = sum((p.amount - p.refunded_amount for p in partners_qs), ZERO)
    total = own + partners
    return {
        "period": {"date_from": d1.isoformat(), "date_to": d2.isoformat()},
        "clients": {"total": clients.count(), "legacy": clients.filter(origin="ozok_legacy").count(),
                    "direct": clients.filter(origin="ozok_direct").count()},
        "subscriptions_by_status": by_status,
        "mrr": money(sum((r["mrr"] for r in spheres), ZERO)),
        "collected": money(own),
        "spheres": [{**r, "mrr": money(r["mrr"]), "collected": money(r["collected"])} for r in spheres],
        "share": {"ozok": money(own), "partners": money(partners),
                  "ozok_percent": money(own * 100 / total) if total else money(0),
                  "partners_percent": money(partners * 100 / total) if total else money(0)},
    }


def network_summary(d1, d2):
    d1, d2 = _period(d1, d2)
    res = []
    for p in Product.objects.filter(is_active=True):
        s = _sales(None, d1, d2, p).aggregate(a=Sum("amount"))["a"] or ZERO
        direct = Deal.objects.filter(partner__isnull=True, status="confirmed", product=p, date__gte=d1,
                                     date__lte=d2).aggregate(s=Sum("total"))["s"] or ZERO
        res.append({"product": p.id, "name": loc(p, "name"), "partners_revenue": money(s), "nbs_direct_revenue": money(direct)})
    return res


def parse_range(params):
    from django.utils.dateparse import parse_date
    try:
        d1 = parse_date(params["date_from"]) if params.get("date_from") else None
        d2 = parse_date(params["date_to"]) if params.get("date_to") else None
    except ValueError:
        raise BusinessError(gettext("Неверный период: укажите date_from и date_to в формате YYYY-MM-DD."))
    if (params.get("date_from") and d1 is None) or (params.get("date_to") and d2 is None):
        raise BusinessError(gettext("Неверный период: укажите date_from и date_to в формате YYYY-MM-DD."))
    if d1 and d2 and d1 > d2:
        raise BusinessError(gettext("Неверный период: дата начала позже даты окончания."))
    return d1, d2
