"""Правила дохода франчайзи и цены тарифов (раздел 5Д): выбор правила, расчёт, цена тарифа на дату."""
from django.utils.translation import gettext
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q

from core.exceptions import BusinessError
from core.models import q2, today

from .models import CommissionRule, Tariff, TariffPrice

SUB_KINDS = ("subscription", "renewal")


def rule_kind(kind):
    """Вид продажи -> вид в правиле (subscription объединяет подписку и продление)."""
    return "subscription" if kind in SUB_KINDS else kind


def rule_weight(rule):
    """Конкретность правила: franchisee +4, tariff +2, product +1, sale_kind +1, partner_tier +1."""
    return (4 * bool(rule.franchisee_id) + 2 * bool(rule.tariff_id) + bool(rule.product_id) + bool(rule.sale_kind)
            + bool(rule.partner_tier))


def active_rules(on):
    return (CommissionRule.objects.filter(is_active=True, valid_from__lte=on)
            .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=on)))


def select_rule(*, franchisee_id, product_id, tariff_id, kind, on=None, partner_tier="", lead_source=None):
    """Шаг 1 (базовая ставка): самое конкретное активное правило на дату среди правил без lead_source.

    Правило с partner_tier применяется только к партнёру этого уровня на дату операции (C11): персональное правило
    «30%» с tier=growth перестаёт действовать после понижения до Standard.
    lead_source задан — ищем корректирующее правило шага 2 (правила с этим источником лида).
    При равенстве — позднее valid_from, затем больший id."""
    on = on or today()
    qs = active_rules(on).filter(
        Q(franchisee__isnull=True) | Q(franchisee_id=franchisee_id),
        Q(product__isnull=True) | Q(product_id=product_id),
        Q(sale_kind="") | Q(sale_kind=rule_kind(kind)),
        Q(partner_tier="") | Q(partner_tier=partner_tier or "__none__"))
    qs = qs.filter(lead_source=lead_source) if lead_source else qs.filter(lead_source="")
    qs = qs.filter(Q(tariff__isnull=True) | Q(tariff_id=tariff_id)) if tariff_id else qs.filter(tariff__isnull=True)
    return max(qs, key=lambda r: (rule_weight(r), r.valid_from, r.id), default=None)


def compute_income(rule, *, amount, list_amount=None, months=None):
    """Доход франчайзи по правилу: база × percent/100 + фикс (за продажу или за оплаченный месяц)."""
    zero = Decimal("0.00")
    if rule is None:
        return {"percent": zero, "fixed": zero, "base": q2(amount), "income": zero}
    use_list = rule.base == CommissionRule.Base.LIST and list_amount and Decimal(list_amount) > 0
    base = q2(list_amount) if use_list else q2(amount)
    percent = Decimal(rule.percent or 0)
    fixed = q2(rule.fixed_amount or 0)
    if rule.fixed_per == CommissionRule.FixedPer.MONTH:
        fixed = q2(fixed * (months or 1))
    return {"percent": q2(percent), "fixed": fixed, "base": base, "income": q2(base * percent / 100) + fixed}


def split_rule_for(product_id, on):
    from .models import SetupSplitRule
    if not product_id:
        return None
    return (SetupSplitRule.objects.filter(product_id=product_id, is_active=True, valid_from__lte=on)
            .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=on)).order_by("-valid_from", "-id").first())


def split_amounts(rule, sold_amount):
    """Доли партнёра и внедренца: пропорционально, если продано дешевле rule.total (C10); внедренец — округление half-up,
    партнёр — остаток (M3)."""
    sold = q2(sold_amount)
    ratio = min(Decimal(1), sold / rule.total) if rule.total > 0 else Decimal(1)
    impl = q2(rule.implementer_amount * ratio)
    paid_total = min(sold, rule.total)
    partner = q2(rule.partner_amount * ratio)
    if ratio < 1:
        partner = q2(paid_total - impl)
    return {"partner": partner, "implementer": impl, "ratio": ratio}


def compute_partner_income(*, franchisee_id, product_id, tariff_id, kind, amount, on, partner_tier="",
                           lead_source="", list_amount=None, months=None):
    """Двухшаговый расчёт (C11): 1) базовая ставка по продукту × уровню; 2) корректировка по лиду OZOK.

    Абонплата: корректирующее правило заменяет ставку (напр. 15% вместо 20/30%); разница пишется как
    adjustment_amount. Установка/оборудование/прочее: деньги у франчайзи — правило задаёт его долю (70%), остаток
    (30%) — доля OZOK (nbs_share), оформляется Charge(nbs_lead_share)."""
    zero = Decimal("0.00")
    rule = select_rule(franchisee_id=franchisee_id, product_id=product_id, tariff_id=tariff_id, kind=kind, on=on,
                       partner_tier=partner_tier)
    base_calc = compute_income(rule, amount=amount, list_amount=list_amount or amount, months=months)
    split = split_rule_for(product_id, on) if kind == "setup" else None
    if split is not None:
        # C10: SetupSplitRule перекрывает CommissionRule; доход партнёра = его доля (пропорционально при цене ниже)
        part = split_amounts(split, amount)["partner"]
        base_calc = {"percent": zero, "fixed": zero, "base": q2(amount), "income": part}
        rule = None
    if rule is None and kind == "training":
        # H-2: обучение — отдельный вид, вне SetupSplitRule; без правила по умолчанию 100% партнёру, как установка
        base_calc = {"percent": Decimal("100.00"), "fixed": zero, "base": q2(amount), "income": q2(amount)}
    res = {"split_rule": split, "rule": rule, "percent": base_calc["percent"], "fixed": base_calc["fixed"], "base": base_calc["base"],
           "base_income": base_calc["income"], "income": base_calc["income"], "adjustment_rule": None,
           "adjustment_amount": zero, "nbs_share": zero}
    adj = None
    if lead_source:
        adj = select_rule(franchisee_id=franchisee_id, product_id=product_id, tariff_id=tariff_id, kind=kind, on=on,
                          partner_tier=partner_tier, lead_source=lead_source)
    if adj is not None:
        res["adjustment_rule"] = adj
        if kind in SUB_KINDS:
            c = compute_income(adj, amount=amount, list_amount=list_amount or amount, months=months)
            res.update(percent=c["percent"], fixed=c["fixed"], base=c["base"], income=c["income"],
                       adjustment_amount=c["income"] - base_calc["income"])
        else:
            keep = Decimal(adj.percent if adj.percent is not None else 100)
            share = q2(base_calc["income"] * (100 - keep) / 100)
            res.update(nbs_share=share, adjustment_amount=-share)
    return res


def preview(*, franchisee_id, product_id, tariff_id, kind, amount, months=None, on=None, list_amount=None,
            partner_tier="", lead_source=""):
    on = on or today()
    r = compute_partner_income(franchisee_id=franchisee_id, product_id=product_id, tariff_id=tariff_id, kind=kind,
                               amount=amount, on=on, partner_tier=partner_tier, lead_source=lead_source,
                               list_amount=list_amount or amount, months=months)
    return r["rule"], r


# ---- тарифы ------------------------------------------------------------------------------------

def price_on(tariff, on=None):
    """Цена тарифа в месяц на дату (None, если на эту дату цены нет)."""
    on = on or today()
    row = (TariffPrice.objects.filter(tariff=tariff, valid_from__lte=on)
           .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=on)).order_by("-valid_from", "-id").first())
    return row.monthly_price if row else None


def set_price(tariff, monthly_price, valid_from):
    """Новая цена с даты: закрывает открытую запись (valid_to = день до новой даты) и открывает новую.
    История не переписывается: дата должна быть позже начала текущей цены."""
    monthly_price = q2(monthly_price)
    if monthly_price < 0:
        raise BusinessError(gettext("Цена не может быть отрицательной."))
    current = TariffPrice.objects.filter(tariff=tariff, valid_to__isnull=True).order_by("-valid_from", "-id").first()
    if TariffPrice.objects.filter(tariff=tariff, valid_from__gte=valid_from).exists():
        raise BusinessError(gettext("Новая цена должна действовать позже всех существующих записей истории."))
    if current is not None:
        current.valid_to = valid_from - timedelta(days=1)
        current.save(update_fields=["valid_to", "updated_at"])
    return TariffPrice.objects.create(tariff=tariff, monthly_price=monthly_price, valid_from=valid_from)


def create_tariff(*, product, code, name, monthly_price, valid_from=None, **extra):
    tariff = Tariff.objects.create(product=product, code=code, name=name, **extra)
    TariffPrice.objects.create(tariff=tariff, monthly_price=q2(monthly_price), valid_from=valid_from or today())
    return tariff


def min_current_price(product, on=None):
    prices = [price_on(t, on) for t in product.tariffs.filter(is_active=True)]
    prices = [p for p in prices if p is not None]
    return min(prices) if prices else None
