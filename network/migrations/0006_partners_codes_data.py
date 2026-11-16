"""P1/P3/H6: статусы партнёров, коды продуктов NBS §2.1, предложения франшизы, правила Growth.

Коды продуктов переименовываются на месте (те же id и ключи API); новые продукты (OnAI, El-bilim, Kechikpe)
добавляются только в БД, где уже есть данные (в пустой БД их создаёт seed_demo)."""
import datetime
import secrets
from decimal import Decimal

from django.db import migrations

RENAME = {"market": "nur_retail", "opt": "nur_wholesale"}
NEW_PRODUCTS = [  # code, name, sphere, icon
    ("onai", "OnAI CRM", "Клиентские коммуникации", "briefcase"),
    ("el_bilim", "El-bilim", "Образование", "graduation-cap"),
    ("kechikpe", "Kechikpe", "Отсрочка платежей", "box"),
]
STATUS_MAP = {"onboarding": "trainee", "active": "franchisee", "terminated": "closed"}
STAGE_BY_STATUS = {"candidate": "new_application", "trainee": "training", "franchisee": "activated",
                   "suspended": "activated", "closed": "activated"}


def forwards(apps, schema_editor):
    Product = apps.get_model("network", "Product")
    Franchisee = apps.get_model("network", "Franchisee")
    Lead = apps.get_model("leads", "Lead")
    Rule = apps.get_model("network", "CommissionRule")
    Offer = apps.get_model("network", "FranchiseOffer")
    had_data = Product.objects.exists()
    for old, new in RENAME.items():
        p = Product.objects.filter(code=old).first()
        if p and not Product.objects.filter(code=new).exists():
            p.code = new
            p.save(update_fields=["code"])
            Lead.objects.filter(product=old).update(product=new)
    if had_data:
        for i, (code, name, sphere, icon) in enumerate(NEW_PRODUCTS, start=Product.objects.count()):
            if not Product.objects.filter(code=code).exists():
                Product.objects.create(code=code, name=name, sphere=sphere, icon=icon, sort_order=i + 1,
                                       color="#00BEBA", api_key="ozk_" + secrets.token_urlsafe(32))
    for old, new in STATUS_MAP.items():
        Franchisee.objects.filter(status=old).update(status=new)
    for status, stage in STAGE_BY_STATUS.items():
        Franchisee.objects.filter(status=status).update(funnel_stage=stage)
    start = datetime.date(2020, 1, 1)
    Rule.objects.get_or_create(
        name="Абонплата Growth — 30%", franchisee=None, product=None, tariff=None, sale_kind="subscription",
        partner_tier="growth", lead_source="",
        defaults={"percent": Decimal("30"), "valid_from": start, "basis": "ТЗ NBS §2.1"})
    if had_data:
        for code in ("el_bilim", "kechikpe"):
            product = Product.objects.filter(code=code).first()
            for tier in ("standard", "growth"):
                Rule.objects.get_or_create(
                    name=f"{product.name} — 10% ({tier})", product=product, franchisee=None, tariff=None,
                    sale_kind="subscription", partner_tier=tier, lead_source="",
                    defaults={"percent": Decimal("10"), "valid_from": start, "basis": "ТЗ NBS §2.1"})
    Offer.objects.get_or_create(name="Сразу", defaults={
        "total": Decimal("87500"), "upfront": Decimal("87500"), "installments_count": 0, "step_months": 1,
        "sort_order": 1})
    Offer.objects.get_or_create(name="Рассрочка до", defaults={
        "total": Decimal("175000"), "upfront": Decimal("8750"), "installments_count": 4, "step_months": 1,
        "sort_order": 2})


def backwards(apps, schema_editor):
    Product = apps.get_model("network", "Product")
    Franchisee = apps.get_model("network", "Franchisee")
    for old, new in RENAME.items():
        Product.objects.filter(code=new).update(code=old)
    for old, new in STATUS_MAP.items():
        Franchisee.objects.filter(status=new).update(status=old)


class Migration(migrations.Migration):
    dependencies = [("network", "0005_franchiseoffer_alter_franchisee_options_and_more"),
                    ("leads", "0003_leads_data")]
    operations = [migrations.RunPython(forwards, backwards)]
