"""Продажа одной формой (P0+): расчёт по строкам, скидки и согласование, подтверждение, отмена."""
from django.utils.translation import gettext
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core import change_requests
from core.exceptions import BusinessError
from core.models import add_months, q2, today
from core.settings_registry import get_decimal
from network.commission import compute_partner_income, price_on
from network.models import Contract, Franchisee
from network.partners import status_on, tier_on
from network.services import withhold_remaining

from .models import Client, Deal, DealLine

ZERO = Decimal("0.00")
SALE_KIND = {"setup": "setup", "training": "training", "equipment": "equipment", "subscription": "subscription"}


def _pct_amount(base, percent, amount):
    percent, amount = Decimal(percent or 0), Decimal(amount or 0)
    if percent and amount:
        raise BusinessError(gettext("Скидка задаётся процентом или суммой, не обоими сразу."))
    if percent < 0 or amount < 0 or percent > 100:
        raise BusinessError(gettext("Некорректная скидка."))
    return min(q2(base * percent / 100) if percent else q2(amount), base)


def _max_discount(deal_product, line):
    if line["kind"] == "subscription" and line["tariff"].max_discount_percent is not None:
        return Decimal(line["tariff"].max_discount_percent)
    if deal_product.max_discount_percent is not None:
        return Decimal(deal_product.max_discount_percent)
    return get_decimal("pricing.max_discount_percent")


def _line_price(product, line, on):
    kind = line["kind"]
    if kind == "setup":
        return q2(product.setup_price), Decimal(1)
    if kind == "training":
        return q2(product.training_price), Decimal(1)
    if kind == "equipment":
        eq = line.get("equipment")
        if eq is None:
            raise BusinessError(gettext("Для оборудования укажите модель."))
        if not eq.is_active:
            raise BusinessError(gettext("Оборудование отключено."))
        qty = Decimal(line.get("quantity") or 1)
        if qty <= 0:
            raise BusinessError(gettext("Количество должно быть больше нуля."))
        return q2(eq.price), qty
    if kind == "subscription":
        tariff = line.get("tariff")
        months = line.get("months")
        if tariff is None or not months or int(months) < 1:
            raise BusinessError(gettext("Для абонплаты укажите тариф и количество месяцев."))
        if tariff.product_id != product.id:
            raise BusinessError(gettext("Тариф относится к другой сфере."))
        price = price_on(tariff, on)
        if price is None:
            raise BusinessError(gettext("На дату продажи у тарифа нет цены."))
        return q2(price), Decimal(int(months))
    raise BusinessError(gettext("Неизвестный вид строки."))


def compute_deal(*, product, partner, date, lead, lines, deal_discount_percent=0, deal_discount_amount=0):
    """Чистый расчёт (без записи): по каждой строке прайс, скидки, итог, доход партнёра, доля OZOK, удержание."""
    on = date or today()
    if not lines:
        raise BusinessError(gettext("Добавьте хотя бы одну строку продажи."))
    rows = []
    for ln in lines:
        price, qty = _line_price(product, ln, on)
        list_amount = q2(price * qty)
        line_disc = _pct_amount(list_amount, ln.get("discount_percent"), ln.get("discount_amount"))
        rows.append({**ln, "list_price": price, "qty": qty, "list_amount": list_amount, "line_discount": line_disc,
                     "after": list_amount - line_disc, "share": ZERO})
    base = sum((r["after"] for r in rows), ZERO)
    deal_disc = _pct_amount(base, deal_discount_percent, deal_discount_amount)
    left = deal_disc
    positive = [r for r in rows if r["after"] > 0]
    for i, r in enumerate(positive):
        if i == len(positive) - 1:
            r["share"] = left
        else:
            r["share"] = q2(deal_disc * r["after"] / base)
            left -= r["share"]
    # доход партнёра
    p_status = status_on(partner, on) if partner else None
    tier = tier_on(partner, on) if partner else ""
    lead_source = lead.source if lead is not None else ""
    contract = (Contract.objects.filter(franchisee=partner, status=Contract.Status.ACTIVE, products=product)
                .order_by("-signed_at", "-id").first()) if partner else None
    remaining = withhold_remaining(contract) if contract is not None and contract.withhold_target > 0 else ZERO
    violations = []
    for r in rows:
        r["total"] = r["after"] - r["share"]
        if r["total"] <= 0:
            raise BusinessError(gettext("Итог по строке должен быть больше нуля."))
        r["partner_income"] = r["nbs_share"] = r["withheld"] = r["nbs_income"] = ZERO
        kind = SALE_KIND[r["kind"]]
        if partner is None:
            r["nbs_income"] = r["total"]
        else:
            if kind == "subscription" and p_status != Franchisee.Status.FRANCHISEE:
                income = ZERO  # абонентская комиссия — только у партнёра со статусом «Франчайзи»
                r["note"] = gettext("Абонентская комиссия не начисляется: статус партнёра не «Франчайзи».")
            else:
                calc = compute_partner_income(
                    franchisee_id=partner.id, product_id=product.id,
                    tariff_id=r["tariff"].id if r.get("tariff") else None, kind=kind, amount=r["total"], on=on,
                    partner_tier=tier, lead_source=lead_source, list_amount=r["list_amount"],
                    months=int(r["qty"]) if kind == "subscription" else None)
                income, r["nbs_share"] = calc["income"], calc["nbs_share"]
            r["partner_income"] = income
            wp = Decimal(contract.withhold_percent) if contract is not None else Decimal(0)
            if income > 0 and wp > 0 and remaining > 0:
                r["withheld"] = min(q2(income * wp / 100), remaining)
                remaining -= r["withheld"]
            r["nbs_income"] = (r["total"] - income) if kind == "subscription" else r["nbs_share"]
        # ограничения цены и скидки (M1)
        r["violation"] = ""
        eff = (r["list_amount"] - r["total"]) * 100 / r["list_amount"] if r["list_amount"] > 0 else Decimal(0)
        problems = []
        if eff > _max_discount(product, r):
            problems.append(gettext('скидка {v}% выше допустимой {v1}%').format(v=q2(eff), v1=_max_discount(product, r)))
        unit = q2(r["total"] / r["qty"])
        floor = {"subscription": r["tariff"].min_price if r.get("tariff") else None,
                 "setup": product.min_setup_price, "training": product.min_training_price,  # H-1: без заданного минимума пола нет (не пол установки)
                 "equipment": r["equipment"].min_price if r.get("equipment") else None}[r["kind"]]
        if floor is not None and unit < floor:
            problems.append(gettext('цена {unit} ниже минимальной {v}').format(unit=unit, v=q2(floor)))
        if problems:
            r["violation"] = "; ".join(problems)
            violations.append(f"{dict(DealLine.Kind.choices)[r['kind']]}: {r['violation']}")
        r["partner_net"] = r["partner_income"] - r["withheld"] - r["nbs_share"]
    totals = {
        "list_total": sum((r["list_amount"] for r in rows), ZERO),
        "discount_total": sum((r["list_amount"] - r["total"] for r in rows), ZERO),
        "total": sum((r["total"] for r in rows), ZERO),
        "partner_income_total": sum((r["partner_income"] for r in rows), ZERO),
        "withheld_total": sum((r["withheld"] for r in rows), ZERO),
        "nbs_share_total": sum((r["nbs_share"] for r in rows), ZERO),
        "nbs_income_total": sum((r["nbs_income"] for r in rows), ZERO),
    }
    totals["partner_net_total"] = totals["partner_income_total"] - totals["withheld_total"] - totals["nbs_share_total"]
    return {"lines": rows, "totals": totals, "violations": violations, "partner_status": p_status,
            "partner_tier": tier}


def _apply_to_lines(deal, computed):
    deal.lines.all().delete()
    for r in computed["lines"]:
        DealLine.objects.create(
            deal=deal, kind=r["kind"], equipment=r.get("equipment"), tariff=r.get("tariff"),
            quantity=r["qty"] if r["kind"] == "equipment" else 1,
            months=int(r["qty"]) if r["kind"] == "subscription" else None,
            serial_numbers=r.get("serial_numbers") or [], list_price=r["list_price"],
            discount_percent=r.get("discount_percent") or 0, discount_amount=r.get("discount_amount") or 0,
            discount_reason=r.get("discount_reason") or "", list_amount=r["list_amount"],
            line_discount=r["line_discount"], deal_discount_share=r["share"], total=r["total"],
            partner_income=r["partner_income"], nbs_income=r["nbs_income"], withheld=r["withheld"],
            violation=r["violation"], subscription_status="awaiting_payment" if r["kind"] == "subscription" else "")
    t = computed["totals"]
    deal.list_total, deal.discount_total, deal.total = t["list_total"], t["discount_total"], t["total"]
    deal.partner_income_total, deal.nbs_income_total = t["partner_income_total"], t["nbs_income_total"]
    deal.save()


@transaction.atomic
def save_deal(deal, lines):
    """Рассчитать и записать строки черновика; deal уже содержит product/partner/lead/date/скидки."""
    computed = compute_deal(
        product=deal.product, partner=deal.partner, date=deal.date, lead=deal.lead or (deal.client.lead if deal.client else None),
        lines=lines, deal_discount_percent=deal.deal_discount_percent, deal_discount_amount=deal.deal_discount_amount)
    deal.price_list_snapshot = _snapshot_prices(deal, computed)
    deal.save()
    _apply_to_lines(deal, computed)
    return deal, computed


def _snapshot_prices(deal, computed):
    """Прайс на дату продажи (P6): цены тарифа/установки/обучения/оборудования на момент сохранения."""
    on = deal.date or today()
    snap = {"date": on.isoformat(), "setup_price": str(q2(deal.product.setup_price)),
            "training_price": str(q2(deal.product.training_price)), "lines": []}
    for r in computed["lines"]:
        row = {"kind": r["kind"], "list_price": str(r["list_price"])}
        if r["kind"] == "subscription":
            snap.setdefault("monthly_price", str(r["list_price"]))
            row["tariff"] = r["tariff"].id
        if r["kind"] == "equipment" and r.get("equipment"):
            row["equipment"] = r["equipment"].id
        snap["lines"].append(row)
    return snap


def refresh_payment_status(deal):
    """unpaid | partial | paid: оплаты по продаже (ClientSale установки/оборудования + Payment по подписке)."""
    from billing.models import Payment
    from .models import ClientSale
    paid = sum((s.amount for s in ClientSale.objects.filter(deal=deal, amount__gt=0, refund_of__isnull=True)
                .exclude(kind__in=ClientSale.COMMISSION_KINDS)), ZERO)
    paid += sum((p.amount - p.refunded_amount for p in Payment.objects.filter(deal=deal, status="confirmed")), ZERO)
    status = "unpaid" if paid <= 0 else ("paid" if paid >= deal.total else "partial")
    if deal.marked_paid_at is not None and status != "paid":
        status = "paid"  # финансист отметил оплату вручную; возвраты статус/этап воронки не откатывают
    if status != deal.payment_status:
        deal.payment_status = status
        deal.save(update_fields=["payment_status", "updated_at"])
    if status == "paid":
        advance_funnel(deal)
        if deal.status == Deal.Status.CONFIRMED:
            _lead_paid(deal)
    return status


def advance_funnel(deal):
    """AC1: 1-я/2-я/3-я продажа менеджера — только по оплаченной (paid) сделке."""
    if deal.partner_id is None or deal.status != Deal.Status.CONFIRMED:
        return
    from network.partners import register_paid_sale
    from .services import _paid_deals_count
    register_paid_sale(deal.partner, _paid_deals_count(deal.partner))


@transaction.atomic
def mark_paid(deal, user):
    """Финансист/владелец вручную отмечает сделку оплаченной (деньги получены вне системы)."""
    deal = Deal.objects.select_for_update().get(pk=deal.pk)
    if deal.status != Deal.Status.CONFIRMED:
        raise BusinessError(gettext("Отметить оплату можно только у подтверждённой сделки."))
    if deal.payment_status == "paid":
        raise BusinessError(gettext("Сделка уже оплачена."))
    from core import audit
    old = deal.payment_status
    deal.marked_paid_at, deal.marked_paid_by = timezone.now(), user
    deal.save(update_fields=["marked_paid_at", "marked_paid_by", "updated_at"])
    refresh_payment_status(deal)
    audit.log_event("status", deal, changes={"payment_status": [old, "paid"]}, reason=gettext("Отмечена оплаченной вручную"),
                    user=user)
    return deal


def _violations(deal):
    return [f"{l.get_kind_display()}: {l.violation}" for l in deal.lines.all() if l.violation]


@transaction.atomic
def confirm_deal(deal, user, approved=False):
    """Подтвердить продажу. Нарушение мин. цены/макс. скидки → pending_approval + ChangeRequest."""
    deal = Deal.objects.select_for_update().get(pk=deal.pk)
    if deal.status not in (Deal.Status.DRAFT, Deal.Status.PENDING_APPROVAL):
        raise BusinessError(gettext("Подтвердить можно только черновик."))
    if deal.status == Deal.Status.PENDING_APPROVAL and not approved:
        raise BusinessError(gettext("Продажа ожидает согласования OZOK."))
    if deal.partner_id is not None and deal.client_id is None:
        raise BusinessError(gettext("Укажите клиента."))
    if deal.partner_id is None and not (deal.client_id or deal.client_name):
        raise BusinessError(gettext("Укажите клиента (карточку или название)."))
    if deal.partner_id is None and deal.client_id is None:
        deal.client = _direct_client(deal)  # H-4: прямая продажа OZOK всегда с карточкой клиента
    lines = list(deal.lines.select_related("equipment", "tariff"))
    if not lines:
        raise BusinessError(gettext("В продаже нет строк."))
    problems = _violations(deal)
    if problems and not approved:
        deal.status = Deal.Status.PENDING_APPROVAL
        deal.approval_note = "; ".join(problems)[:500]
        deal.save()
        change_requests.create_request(
            user=user, entity="clients.Deal", entity_id=deal.pk, field="approval", old_value=Deal.Status.DRAFT,
            new_value=Deal.Status.CONFIRMED, reason=deal.approval_note, franchisee=deal.partner)
        return deal
    from .services import create_client_sale
    _apply_lead(deal)
    for line in lines:
        if line.kind == "subscription" or deal.partner_id is None:
            continue
        sale, _ = create_client_sale(
            client=deal.client, product=deal.product, kind=SALE_KIND[line.kind], amount=line.total, date=deal.date,
            comment=gettext('Продажа №{pk}').format(pk=deal.pk), created_by=user, equipment=line.equipment, quantity=line.quantity,
            list_amount=line.list_amount, discount_amount=line.list_amount - line.total, lead=deal.lead, deal=deal,
            branch=deal.branch, serial_numbers=line.serial_numbers if line.kind == "equipment" else None,
            sold_by=deal.partner if line.kind == "equipment" else None,
            warranty_until=(add_months(deal.date, line.equipment.warranty_months)
                            if line.kind == "equipment" and line.equipment and line.equipment.warranty_months
                            else None))
        line.sale = sale
        line.partner_income, line.withheld, line.nbs_income = sale.franchisee_income, sale.withheld_amount, \
            sale.nbs_share_amount
        line.save()
    for line in deal.lines.filter(kind="subscription"):  # комиссия с абонплаты — только при поступлении денег
        line.partner_income = line.nbs_income = line.withheld = ZERO
        line.save()
        if deal.partner_id is None:
            _paid_direct_subscription(deal, line)
        else:
            _ensure_subscription(deal, line)
    deal.status, deal.confirmed_at = Deal.Status.CONFIRMED, timezone.now()
    if approved:
        deal.approved_by, deal.approved_at = user, timezone.now()
    deal.partner_income_total = sum((l.partner_income for l in deal.lines.all()), ZERO)
    deal.nbs_income_total = sum((l.nbs_income for l in deal.lines.all()), ZERO)
    deal.save()
    refresh_payment_status(deal)
    from .installations import create_for_deal
    create_for_deal(deal, user)
    if deal.partner_id is None:  # прямая продажа OZOK: деньги у OZOK, оплачена при подтверждении
        from billing import cash
        deal.payment_status = "paid"
        deal.save(update_fields=["payment_status", "updated_at"])
        cash.on_direct_deal(deal)
    return deal


def subscription_income_on_payment(line):
    """Прогноз дохода партнёра с абонплаты при оплате клиентом (по правилам на дату сделки)."""
    deal = line.deal
    if deal.partner_id is None or line.tariff_id is None or not line.months:
        return ZERO
    on = deal.date or today()
    if status_on(deal.partner, on) != Franchisee.Status.FRANCHISEE:
        return ZERO
    calc = compute_partner_income(
        franchisee_id=deal.partner_id, product_id=deal.product_id, tariff_id=line.tariff_id, kind="subscription",
        amount=line.total, on=on, partner_tier=tier_on(deal.partner, on),
        lead_source=deal.lead.source if deal.lead_id else "", list_amount=line.list_amount, months=int(line.months))
    return calc["income"]


def _direct_client(deal):
    """Карточка клиента прямой продажи OZOK: origin=ozok_direct, без партнёра (повторное имя — та же карточка)."""
    client = Client.objects.filter(origin=Client.Origin.OZOK_DIRECT, business_name__iexact=deal.client_name).first()
    if client is None:
        client = Client.objects.create(franchisee=None, origin=Client.Origin.OZOK_DIRECT,
                                       business_name=deal.client_name)
    return client


def _paid_direct_subscription(deal, line):
    """Прямая продажа: деньги получены при подтверждении -> подписка с paid_until на оплаченные месяцы."""
    sub = _ensure_subscription(deal, line)
    if sub is None or not line.months:
        return sub
    from datetime import timedelta

    from . import subscriptions as sub_svc
    sub.started_at = sub.started_at or deal.date
    sub.paid_until = max(sub.paid_until or deal.date, add_months(deal.date, line.months) - timedelta(days=1))
    sub_svc.refresh_status(sub, save=False)
    sub.save()
    line.subscription_status = sub.status
    line.save(update_fields=["subscription_status", "updated_at"])
    return sub


def _apply_lead(deal):
    """C-2: лид сделки закрепляется за клиентом и филиалом (в т.ч. основным): от него зависит корректировка по
    лидам OZOK. Лид переходит в «Договор» при подтверждении сделки."""
    lead = deal.lead
    if lead is None or deal.client_id is None:
        return
    from .branches import ensure_main_branch
    client = deal.client
    if client.lead_id is None:
        client.lead = lead
        client.save(update_fields=["lead", "updated_at"])
    branch = deal.branch or ensure_main_branch(client)
    if branch.lead_id is None:
        branch.lead, branch.lead_source = lead, lead.source
        branch.save(update_fields=["lead", "lead_source", "updated_at"])
    from leads.models import Lead
    if lead.status in Lead.OPEN_STATUSES and lead.status != Lead.Status.CONTRACT:
        lead.status = Lead.Status.CONTRACT
        lead.save(update_fields=["status", "updated_at"])


def _lead_paid(deal):
    lead = deal.lead
    if lead is None or lead.status in ("paid", "lost"):
        return
    from django.utils import timezone as tz
    lead.status, lead.closed_at = "paid", tz.now()
    if deal.client_id and lead.client_id is None:
        lead.client = deal.client
    lead.save()


def _ensure_subscription(deal, line):
    """P9: подтверждённая продажа со строкой абонплаты создаёт подписку филиала (awaiting_payment)."""
    if deal.client_id is None:
        return None
    from .branches import ensure_main_branch
    from .models import Subscription
    branch = deal.branch or ensure_main_branch(deal.client)
    if branch.lead_id is None and deal.lead_id:
        branch.lead, branch.lead_source = deal.lead, deal.lead.source  # H4/C-2: лид копируется и на основной филиал
    if branch.product_id is None:
        branch.product = deal.product
    if branch.tariff_id is None:
        branch.tariff = line.tariff
    branch.save()
    sub = Subscription.objects.filter(branch=branch, product=deal.product).exclude(status="closed").first()
    price = q2(line.total / line.months) if line.months else None
    if sub is None:
        sub = Subscription.objects.create(
            client=deal.client, branch=branch, product=deal.product, tariff=line.tariff, monthly_fee=price,
            status=Subscription.Status.AWAITING_PAYMENT, deal=deal)
    else:
        if sub.deal_id is None:
            sub.deal = deal
        sub.tariff = sub.tariff or line.tariff
        sub.monthly_fee = sub.monthly_fee or price
        sub.save()
    line.subscription_status = sub.status
    line.save(update_fields=["subscription_status", "updated_at"])
    return sub


def _unwind_deal(deal, user, reason):
    """M-1: отмена подтверждённой сделки закрывает её неоплаченные подписки и отменяет не начатые установки."""
    from billing.models import Payment
    from core import audit

    from .models import Implementation, Subscription
    for sub in Subscription.objects.filter(deal=deal).exclude(status=Subscription.Status.CLOSED):
        if Payment.objects.filter(subscription=sub, status="confirmed").exists():
            continue
        sub.status, sub.closed_at, sub.paid_until = Subscription.Status.CLOSED, today(), None
        sub.save(update_fields=["status", "closed_at", "paid_until", "updated_at"])
        audit.log_event("status", sub, changes={"status": [None, "closed"]},
                        reason=gettext('Отмена продажи №{pk}: {reason}').format(pk=deal.pk, reason=reason), user=user)
    for inst in Implementation.objects.filter(deal=deal, status="assigned"):
        inst.status, inst.stage = "cancelled", "cancelled"
        inst.save(update_fields=["status", "stage", "updated_at"])
        audit.log_event("status", inst, changes={"status": ["assigned", "cancelled"]},
                        reason=gettext('Отмена продажи №{pk}: {reason}').format(pk=deal.pk, reason=reason), user=user)


@transaction.atomic
def cancel_deal(deal, user, reason):
    reason = (reason or "").strip()
    if not reason:
        raise BusinessError(gettext("Укажите причину отмены."))
    if deal.status == Deal.Status.CANCELLED:
        raise BusinessError(gettext("Продажа уже отменена."))
    if deal.status == Deal.Status.CONFIRMED and user.franchisee_id:
        raise BusinessError(gettext("Подтверждённую продажу отменяет OZOK."))
    from .services import cancel_sale
    for sale in list(deal.sales.all()):
        if sale.cancelled_at is None and not sale.refund_of_id:
            cancel_sale(sale, user, gettext('Отмена продажи №{pk}: {reason}').format(pk=deal.pk, reason=reason))
    if deal.status == Deal.Status.CONFIRMED:
        _unwind_deal(deal, user, reason)
    pending = change_requests.ChangeRequest.objects.filter(entity="clients.Deal", entity_id=deal.pk, status="pending")
    for cr in pending:
        cr.status, cr.decision_comment, cr.decided_at, cr.decided_by = "rejected", gettext("Продажа отменена"), \
            timezone.now(), user
        cr.save()
    was_confirmed = deal.status == Deal.Status.CONFIRMED
    deal.status, deal.cancelled_at, deal.cancelled_by, deal.cancel_reason = Deal.Status.CANCELLED, \
        timezone.now(), user, reason
    deal.save()
    if was_confirmed and deal.partner_id is None:
        from billing import cash
        cash.on_direct_deal_cancelled(deal, reason, user)
    return deal


def _on_approve(cr, user):
    deal = Deal.objects.get(pk=cr.entity_id)
    confirm_deal(deal, user, approved=True)


def _on_reject(cr, user):
    deal = Deal.objects.get(pk=cr.entity_id)
    deal.status = Deal.Status.DRAFT
    deal.approval_note = gettext('Отклонено OZOK: {v}').format(v=cr.decision_comment or gettext('без комментария'))[:500]
    deal.save()


change_requests.register("clients.Deal", "approval", _on_approve, _on_reject)
