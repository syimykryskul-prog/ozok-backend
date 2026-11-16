"""Бизнес-правила договоров: цены, составная оплата франшизы, активация (разделы 5В, 5Д)."""
from django.utils.translation import gettext
from decimal import ROUND_DOWN, Decimal

from django.db import transaction
from django.db.models import Sum

from billing.models import Charge, FranchiseePayment
from core.exceptions import BusinessError
from core.models import CENT, add_months, q2

from .models import Contract

DEFAULT_INSTALLMENTS = 3
ZERO = Decimal("0")


def price_quote(*, product=None, package=None, scheme):
    """Прайсовая сумма по схеме (5В). product/package — объекты; ровно один из двух. hybrid = цена «сразу»."""
    if (product is None) == (package is None):
        raise BusinessError(gettext("Укажите сферу или пакет (одно из двух)."))
    if scheme not in Contract.Scheme.values:
        raise BusinessError(gettext("Неизвестная схема оплаты."))
    if package is not None:
        price = {"cash": package.price_cash, "prepay": package.price_prepay, "per_sale": package.price_per_sale,
                 "hybrid": package.price_cash}[scheme]
        quote = {"list_price": q2(price)}
        if scheme == "prepay":
            quote.update(prepay_percent=q2(package.prepay_percent), installments_hint=DEFAULT_INSTALLMENTS)
        return quote
    if scheme == "prepay":
        raise BusinessError(gettext("Для одной сферы схема «Предоплата» недоступна."))
    price = product.price_per_sale if scheme == "per_sale" else product.price_cash
    return {"list_price": q2(price)}


def contract_products(contract):
    """Сферы договора: одна сфера или все сферы пакета."""
    if contract.product_id:
        return [contract.product]
    return list(contract.package.products.all())


# ---- составная оплата франшизы -----------------------------------------------------------------

def preset_parts(scheme, fee_total, prepay_percent=40, installments_count=DEFAULT_INSTALLMENTS):
    """Части оплаты по пресету: cash — всё сразу; prepay — предоплата + рассрочка; per_sale/hybrid — всё из продаж."""
    fee = q2(fee_total)
    if scheme == "cash":
        return {"upfront_amount": fee, "installments_total": ZERO, "installments_count": 0}
    if scheme == "prepay":
        upfront = q2(fee * Decimal(prepay_percent) / 100)
        rest = fee - upfront
        return {"upfront_amount": upfront, "installments_total": rest,
                "installments_count": (installments_count or DEFAULT_INSTALLMENTS) if rest > 0 else 0}
    return {"upfront_amount": ZERO, "installments_total": ZERO, "installments_count": 0}


def apply_franchise_offers():
    """Суммы предложений франшизы — из настроек franchise.cash_amount / installment_max_amount."""
    from core.settings_registry import get_decimal
    from .models import FranchiseOffer
    for code, name, total in (("cash", gettext("Сразу"), get_decimal("franchise.cash_amount")),
                              ("installment", gettext("Рассрочка до"), get_decimal("franchise.installment_max_amount"))):
        offer = FranchiseOffer.objects.filter(code=code).first() or FranchiseOffer.objects.filter(name=name).first()
        if offer is None:
            continue
        offer.code, offer.total = code, q2(total)
        offer.upfront = q2(total) if code == "cash" else min(offer.upfront, q2(total))
        offer.save()


def apply_nbs_lead_rules(*, user=None, reason=""):
    """Проценты лидов OZOK из настроек -> правила nbs_ads: новая версия с даты изменения (старая закрывается)."""
    from datetime import timedelta

    from core import audit
    from core.models import today
    from core.settings_registry import get_decimal
    from .models import CommissionRule
    sub_pct = q2(get_decimal("nbs_lead.subscription_percent"))
    keep = q2(100 - get_decimal("nbs_lead.nbs_share_percent"))
    on = today()
    base = CommissionRule.objects.filter(lead_source="nbs_ads", franchisee__isnull=True, product__isnull=True,
                                         tariff__isnull=True, partner_tier="", is_active=True)
    for kind in ("subscription", "setup", "training", "equipment", "other"):
        target = sub_pct if kind == "subscription" else keep
        cur = [r for r in base.filter(sale_kind=kind) if r.valid_from <= on and (r.valid_to is None or r.valid_to >= on)]
        rule = max(cur, key=lambda r: (r.valid_from, r.id), default=None)
        if rule is None:
            if kind == "training":
                continue
            continue
        if q2(rule.percent or 0) == target:
            continue
        if rule.valid_from >= on:
            rule.percent = target
            rule.save(update_fields=["percent", "updated_at"])
            continue
        new = CommissionRule.objects.create(
            name=rule.name, sale_kind=kind, lead_source="nbs_ads", percent=target, valid_from=on, basis=rule.basis,
            note=rule.note, fixed_per=rule.fixed_per, base=rule.base)
        rule.valid_to = on - timedelta(days=1)
        rule.save(update_fields=["valid_to", "updated_at"])
        audit.log_event("create", new, user=user, reason=gettext('Настройка лидов OZOK изменена: {reason}').format(reason=reason),
                        changes={"percent": [str(rule.percent), str(target)], "previous": [None, rule.pk]})


def check_parts(fee_total, upfront, installments_total, installments_count, withhold_percent):
    """Проверка составной оплаты; BusinessError с русским текстом."""
    fee, upfront, inst = q2(fee_total), q2(upfront), q2(installments_total)
    if min(fee, upfront, inst) < 0:
        raise BusinessError(gettext("Суммы не могут быть отрицательными."))
    if upfront + inst > fee:
        raise BusinessError(gettext("Сумма «сразу» и рассрочки не может превышать стоимость франшизы."))
    if inst > 0 and not installments_count:
        raise BusinessError(gettext("Для рассрочки укажите количество платежей."))
    if not 0 <= Decimal(withhold_percent) <= 100:
        raise BusinessError(gettext("Удержание — от 0 до 100 процентов."))
    if fee - upfront - inst > 0 and Decimal(withhold_percent) <= 0:
        raise BusinessError(gettext("Остаток франшизы погашается удержаниями из дохода: укажите удержание больше 0%."))


def _installment_amounts(remainder, n):
    """Равные части; последний платёж забирает копейки округления."""
    n = max(n, 1)
    base = (remainder / n).quantize(CENT, rounding=ROUND_DOWN)
    amounts = [base] * (n - 1)
    amounts.append(remainder - base * (n - 1))
    return amounts


def build_charges(contract):
    """Начисления из частей договора: (kind, amount, due_date, description)."""
    number = contract.number
    rows = []
    upfront = q2(contract.upfront_amount)
    if upfront > 0:
        rows.append(("fee", upfront, contract.upfront_due or contract.signed_at, gettext('Взнос (сразу) по договору {number}').format(number=number)))
    inst = q2(contract.installments_total)
    if inst > 0 and contract.installments_count:
        amounts = _installment_amounts(inst, contract.installments_count)
        step = contract.installment_step_months or 1
        for i, amount in enumerate(amounts, start=1):
            rows.append(("installment", amount, add_months(contract.signed_at, step * i),
                         gettext('Платёж рассрочки {i}/{v} по договору {number}').format(i=i, v=len(amounts), number=number)))
    rest = q2(contract.withhold_target)
    if rest > 0:  # без срока: гасится удержаниями из дохода франчайзи
        rows.append(("fee", rest, None, gettext('Остаток франшизы по договору {number} (погашается удержаниями из дохода)').format(number=number)))
    return rows


def parts_progress(contract, manual, offsets):
    """Прогресс погашения по трём частям. manual — ручные платежи по договору, offsets — зачёты удержаний."""
    manual, offsets = q2(manual), q2(offsets)
    up_total, inst_total = q2(contract.upfront_amount), q2(contract.installments_total)
    up_paid = min(manual, up_total)
    inst_paid = min(manual - up_paid, inst_total)
    wh_total = q2(contract.withhold_target)
    wh_paid = min(offsets + max(manual - up_paid - inst_paid, ZERO), wh_total)
    return {"upfront": {"paid": up_paid, "total": up_total},
            "installments": {"paid": inst_paid, "total": inst_total},
            "withholding": {"paid": wh_paid, "total": wh_total}}


def payment_sums(contract, exclude_sale=None):
    """(ручные платежи, зачёты) по договору."""
    qs = FranchiseePayment.objects.filter(contract=contract)
    if exclude_sale is not None:
        qs = qs.exclude(client_sale=exclude_sale)
    manual = qs.exclude(method="offset").aggregate(s=Sum("amount"))["s"] or ZERO
    offsets = qs.filter(method="offset").aggregate(s=Sum("amount"))["s"] or ZERO
    return q2(manual), q2(offsets)


def contract_progress(contract, exclude_sale=None):
    return parts_progress(contract, *payment_sums(contract, exclude_sale))


def withhold_remaining(contract, exclude_sale=None):
    """Сколько ещё можно удержать из дохода в счёт остатка франшизы."""
    p = contract_progress(contract, exclude_sale)["withholding"]
    return max(p["total"] - p["paid"], ZERO)


# ---- активация / закрытие ----------------------------------------------------------------------

def busy_products(franchisee, products, exclude_contract=None):
    """Сферы из products, уже входящие в действующий договор франчайзи."""
    qs = Contract.objects.filter(franchisee=franchisee, status=Contract.Status.ACTIVE, products__in=products)
    if exclude_contract is not None:
        qs = qs.exclude(pk=exclude_contract.pk)
    ids = set(qs.values_list("products", flat=True))
    return [p for p in products if p.id in ids]


def activate_contract(contract):
    with transaction.atomic():
        c = Contract.objects.select_for_update().get(pk=contract.pk)
        if c.status != Contract.Status.DRAFT:
            raise BusinessError(gettext("Активировать можно только договор в статусе «Черновик»."))
        products = list(c.products.all())
        if not products:
            raise BusinessError(gettext("В договоре не указаны продукты."))
        busy = busy_products(c.franchisee, products, exclude_contract=c)
        if busy:
            raise BusinessError(gettext("У франчайзи уже есть действующий договор на сферу: ")
                                + ", ".join(sorted(p.name for p in busy)) + ".")
        check_parts(c.fee_total, c.upfront_amount, c.installments_total, c.installments_count, c.withhold_percent)
        c.status = Contract.Status.ACTIVE
        c.save(update_fields=["status", "updated_at"])
        for kind, amount, due, desc in build_charges(c):
            if amount > 0:
                Charge.objects.create(franchisee=c.franchisee, contract=c, kind=kind, amount=amount, due_date=due,
                                      description=desc)
        from . import partners
        partners.activate_partner(c.franchisee)
        from billing import recruiters
        recruiters.ensure_reward(c.franchisee)
    contract.refresh_from_db()
    return contract


def close_contract(contract):
    with transaction.atomic():
        c = Contract.objects.select_for_update().get(pk=contract.pk)
        if c.status == Contract.Status.CLOSED:
            raise BusinessError(gettext("Договор уже закрыт."))
        c.status = Contract.Status.CLOSED
        c.save(update_fields=["status", "updated_at"])
    contract.refresh_from_db()
    return contract


def connected_product_ids(franchisee_id):
    return set(Contract.objects.filter(franchisee_id=franchisee_id, status=Contract.Status.ACTIVE)
               .values_list("products", flat=True))


def request_connection(user, *, product=None, package=None):
    """Франчайзи просит подключить сферу/пакет: заявка interest=franchise на самого франчайзи (5В)."""
    from leads.models import Lead
    from leads.services import create_lead

    fr = user.franchisee
    connected = connected_product_ids(fr.id)
    if product is not None:
        if not product.is_active:
            raise BusinessError(gettext("Сфера отключена."))
        if product.id in connected:
            raise BusinessError(gettext("Сфера уже подключена."))
        code, title = product.code, gettext('сферы «{v}»').format(v=product.sphere or product.name)
    else:
        if not package.is_active:
            raise BusinessError(gettext("Пакет отключён."))
        if connected >= set(package.products.values_list("id", flat=True)):
            raise BusinessError(gettext("Все сферы пакета уже подключены."))
        code, title = f"package:{package.id}", gettext('пакета «{name}»').format(name=package.name)
    return create_lead(franchisee=fr, source=Lead.Source.OTHER, interest=Lead.Interest.FRANCHISE, product=code,
                       name=fr.owner_name or user.full_name, phone=user.phone or fr.phone, city=fr.city,
                       comment=gettext('Запрос франчайзи «{name}» на подключение {title}.').format(name=fr.name, title=title))
