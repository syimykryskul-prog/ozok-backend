"""C6: существующие продажи абонплаты -> Payment (1:1) + CommissionAccrual; CommissionPayout(offset) -> withheld_amount."""
from datetime import datetime, time

from django.db import migrations
from django.utils import timezone


def forwards(apps, schema_editor):
    Sale = apps.get_model("clients", "ClientSale")
    Branch = apps.get_model("clients", "Branch")
    Sub = apps.get_model("clients", "Subscription")
    Payment = apps.get_model("billing", "Payment")
    Accrual = apps.get_model("billing", "CommissionAccrual")
    Payout = apps.get_model("billing", "CommissionPayout")
    kinds = ("subscription", "renewal")
    sales = Sale._base_manager.filter(kind__in=kinds).order_by("date", "id")
    acc_by_sale = {}
    for sale in sales:
        branch = (Branch.objects.filter(client_id=sale.client_id, is_main=True).first()
                  or Branch.objects.filter(client_id=sale.client_id).first())
        if branch is None:
            branch = Branch.objects.create(
                client_id=sale.client_id, name="Основной", is_main=True, seller_partner_id=sale.franchisee_id,
                implementer_partner_id=sale.franchisee_id, responsible_partner_id=sale.franchisee_id, status="active")
        if sale.refund_of_id:
            orig = acc_by_sale.get(sale.refund_of_id)
            if orig is not None and sale.commission_amount:
                a = Accrual.objects.create(
                    payment_id=orig.payment_id, sale_id=sale.id, partner_id=orig.partner_id, branch_id=branch.id,
                    client_id=sale.client_id, product_id=sale.product_id, date=sale.date,
                    amount=sale.commission_amount, withheld_amount=0, status="accrued", reversal_of_id=orig.id,
                    reason="Возврат платежа клиента (миграция)", cancelled_at=sale.cancelled_at)
            continue
        sub = Sub.objects.filter(client_id=sale.client_id, product_id=sale.product_id).first()
        paid_at = timezone.make_aware(datetime.combine(sale.date, time(12, 0)))
        src = "product" if sale.source == "integration" else "manual"
        ext = sale.external_payment_id or ""
        pay = Payment.objects.create(
            branch_id=branch.id, subscription_id=sub.id if sub else None, client_id=sale.client_id,
            product_id=sale.product_id, deal_id=sale.deal_id, sale_id=sale.id, amount=sale.amount, paid_at=paid_at,
            period_to=sale.paid_until, months=sale.months or 1, source=src, external_id=ext,
            purpose=sale.comment or "", status="cancelled" if sale.cancelled_at else "confirmed",
            cancelled_at=sale.cancelled_at, cancel_reason=sale.cancel_reason, created_by_id=sale.created_by_id)
        if sale.commission_amount:
            offset = sum(p.amount for p in Payout._base_manager.filter(client_sale_id=sale.id, method="offset",
                                                                       cancelled_at__isnull=True))
            a = Accrual.objects.create(
                payment_id=pay.id, sale_id=sale.id, partner_id=sale.franchisee_id, branch_id=branch.id,
                client_id=sale.client_id, product_id=sale.product_id, date=sale.date, rule_id=sale.commission_rule_id,
                adjustment_rule_id=sale.adjustment_rule_id, rate=sale.commission_percent,
                fixed_amount=sale.commission_fixed, base_amount=sale.commission_base_amount,
                adjustment_amount=sale.adjustment_amount, partner_tier=sale.partner_tier,
                lead_source=sale.lead_source, amount=sale.commission_amount, withheld_amount=offset,
                status="cancelled" if sale.cancelled_at else "accrued", cancelled_at=sale.cancelled_at)
            acc_by_sale[sale.id] = a
    # сторно удержаний (отрицательные зачёты) тоже уходят в withheld_amount возвратов
    for p in Payout._base_manager.filter(method="offset", amount__lt=0, cancelled_at__isnull=True):
        a = Accrual.objects.filter(sale_id=p.client_sale_id, reversal_of__isnull=False).first()
        if a is not None:
            a.withheld_amount += p.amount
            a.save(update_fields=["withheld_amount"])
    # зачёты по абонплате больше не хранятся как выплаты: архивируем (не удаляем)
    now = timezone.now()
    Payout._base_manager.filter(method="offset", cancelled_at__isnull=True).update(
        cancelled_at=now, cancel_reason="Перенесено в withheld_amount начисления (миграция C6)")


class Migration(migrations.Migration):
    dependencies = [("billing", "0006_payment_paymentimport_payout_commissionaccrual_and_more"),
                    ("clients", "0010_clientsale_delivery_status_clientsale_installed_by_and_more")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
