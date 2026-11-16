"""Отчёт «Продажи и доход» (5А, 5Г): по сферам, видам, франчайзи и месяцам."""
from core.l10n import loc
from django.utils.translation import gettext_lazy
from decimal import Decimal

from django.db.models import Count, Q, Sum

from core.models import add_months, month_first, month_label, money
from network.models import Franchisee, Product

from .models import ClientSale

ZERO = Decimal("0")
SUB = Q(kind__in=ClientSale.COMMISSION_KINDS)
KIND_GROUPS = (("setup", gettext_lazy("Установка и обучение")), ("training", gettext_lazy("Обучение")), ("equipment", gettext_lazy("Оборудование")),
               ("subscription", gettext_lazy("Абонплата")), ("other", gettext_lazy("Прочее")))


def _sums(qs, *group):
    return qs.values(*group).annotate(
        n=Count("id"), s_amount=Sum("amount"), s_sub=Sum("amount", filter=SUB),
        s_comm=Sum("commission_amount"), s_income=Sum("franchisee_income"), s_withheld=Sum("withheld_amount"))


def _money_fields(r):
    income, withheld = r.get("s_income") or ZERO, r.get("s_withheld") or ZERO
    return {"income": money(income), "withheld": money(withheld), "net_income": money(income - withheld)}


def _product_rows(by_product_id, products):
    rows = []
    for p in products:
        r = by_product_id.get(p.id, {})
        rows.append({"product": p.code, "sphere": loc(p, "sphere"), "name": loc(p, "name"), "color": p.color, "icon": p.icon,
                     "count": r.get("n", 0), "amount": money(r.get("s_amount")),
                     "subscription_amount": money(r.get("s_sub")), "commission": money(r.get("s_comm")),
                     **_money_fields(r)})
    return rows


def _products_for(used_ids):
    return [p for p in Product.objects.all() if p.is_active or p.id in used_ids]


def _by_kind(qs):
    acc = {k: {"count": 0, "amount": ZERO, "income": ZERO} for k, _ in KIND_GROUPS}
    for kind, n, amount, income in qs.values_list("kind").annotate(
            n=Count("id"), a=Sum("amount"), i=Sum("franchisee_income")):
        key = "subscription" if kind in ClientSale.COMMISSION_KINDS else kind
        acc[key]["count"] += n
        acc[key]["amount"] += amount or ZERO
        acc[key]["income"] += income or ZERO
    return [{"kind": k, "label": label, "count": acc[k]["count"], "amount": money(acc[k]["amount"]),
             "income": money(acc[k]["income"])} for k, label in KIND_GROUPS]


def sales_breakdown(date_from, date_to, franchisee_id=None, product_code=None, with_franchisees=True):
    base = ClientSale.objects.filter(date__gte=date_from, date__lte=date_to)
    if franchisee_id:
        base = base.filter(franchisee_id=franchisee_id)
    # карточки сфер — без фильтра по сфере (вкладки сфер строятся из них)
    grouped = {r["product_id"]: r for r in _sums(base, "product_id")}
    by_product = _product_rows(grouped, _products_for(set(grouped)))

    filtered = base.filter(product__code=product_code) if product_code else base
    t = filtered.aggregate(n=Count("id"), s_amount=Sum("amount"), s_sub=Sum("amount", filter=SUB),
                           s_comm=Sum("commission_amount"), s_income=Sum("franchisee_income"),
                           s_withheld=Sum("withheld_amount"))
    totals = {"count": t["n"], "amount": money(t["s_amount"]), "subscription_amount": money(t["s_sub"]),
              "commission": money(t["s_comm"]), **_money_fields(t), "by_kind": _by_kind(filtered)}

    by_franchisee = []
    if with_franchisees:
        names = dict(Franchisee.objects.values_list("id", "name"))
        for r in _sums(filtered, "franchisee_id").order_by("-s_amount"):
            by_franchisee.append({"franchisee_id": r["franchisee_id"], "name": names.get(r["franchisee_id"], ""),
                                  "count": r["n"], "amount": money(r["s_amount"]),
                                  "commission": money(r["s_comm"]), **_money_fields(r)})

    last_month = month_first(date_to)
    first_month = add_months(last_month, -5)
    monthly = {}
    for d, amount, commission, income, withheld in filtered.filter(
            date__gte=first_month, date__lte=date_to).values_list(
            "date", "amount", "commission_amount", "franchisee_income", "withheld_amount"):
        key = month_label(d)
        a, c, i, w = monthly.get(key, (ZERO, ZERO, ZERO, ZERO))
        monthly[key] = (a + amount, c + commission, i + income, w + withheld)
    by_month = []
    for i in range(6):
        key = month_label(add_months(first_month, i))
        a, c, inc, w = monthly.get(key, (ZERO, ZERO, ZERO, ZERO))
        by_month.append({"month": key, "amount": money(a), "commission": money(c), "income": money(inc),
                         "withheld": money(w), "net_income": money(inc - w)})
    return {"totals": totals, "by_product": by_product, "by_franchisee": by_franchisee, "by_month": by_month}


def sales_by_product(date_from, date_to, franchisee_id=None):
    return sales_breakdown(date_from, date_to, franchisee_id, with_franchisees=False)["by_product"]


def sales_by_product_bulk(date_from, date_to, franchisee_ids):
    """{franchisee_id: by_product rows} одним запросом — для списков франчайзи."""
    qs = ClientSale.objects.filter(date__gte=date_from, date__lte=date_to, franchisee_id__in=list(franchisee_ids))
    grouped = {}
    for r in _sums(qs, "franchisee_id", "product_id"):
        grouped.setdefault(r["franchisee_id"], {})[r["product_id"]] = r
    used = {pid for g in grouped.values() for pid in g}
    products = _products_for(used)
    return {fid: _product_rows(grouped.get(fid, {}), products) for fid in franchisee_ids}


def month_commission_map(date_from, date_to):
    """{franchisee_id: Σ commission_amount} за период."""
    qs = ClientSale.objects.filter(date__gte=date_from, date__lte=date_to)
    return {r["franchisee_id"]: r["s"] or ZERO
            for r in qs.values("franchisee_id").annotate(s=Sum("commission_amount"))}
