"""Данные 5Д: правила по умолчанию, ставки договоров -> правила франчайзи, части оплаты из схем."""
from decimal import ROUND_HALF_UP, Decimal

from django.db import migrations

CENT = Decimal("0.01")

DEFAULT_RULES = [
    ("Абонплата — 20%", "subscription", Decimal("20")),
    ("Установка и обучение — 100%", "setup", Decimal("100")),
    ("Оборудование — 100%", "equipment", Decimal("100")),
    ("Прочее — 100%", "other", Decimal("100")),
]


def forwards(apps, schema_editor):
    Rule = apps.get_model("network", "CommissionRule")
    Contract = apps.get_model("network", "Contract")
    import datetime
    start = datetime.date(2020, 1, 1)
    for name, kind, pct in DEFAULT_RULES:
        Rule.objects.get_or_create(name=name, franchisee=None, product=None, tariff=None, sale_kind=kind,
                                   defaults={"percent": pct, "valid_from": start})
    for c in Contract.objects.all():
        fee = c.fee_total
        if c.payment_scheme == "cash":
            c.upfront_amount = fee
        elif c.payment_scheme == "prepay":
            up = (fee * c.prepay_percent / 100).quantize(CENT, rounding=ROUND_HALF_UP)
            c.upfront_amount, c.installments_total, c.installments_count = up, fee - up, c.installments
        c.save()
        if c.commission_percent != Decimal("20"):
            Rule.objects.get_or_create(
                name=f"{c.franchisee.name} — {c.commission_percent.normalize():f}% с абонплаты", franchisee=c.franchisee,
                product=c.product, sale_kind="subscription",
                defaults={"percent": c.commission_percent, "valid_from": c.signed_at})


class Migration(migrations.Migration):
    dependencies = [("network", "0002_hybrid_prices")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]

