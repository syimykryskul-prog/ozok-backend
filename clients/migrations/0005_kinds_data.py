"""Данные 5Г: license -> setup, service -> other; доход франчайзи у старых продаж."""
from django.db import migrations
from django.db.models import F


def forwards(apps, schema_editor):
    Sale = apps.get_model("clients", "ClientSale")
    Sale.objects.filter(kind="license").update(kind="setup")
    Sale.objects.filter(kind="service").update(kind="other")
    Sale.objects.filter(kind__in=["setup", "other", "equipment"]).update(franchisee_income=F("amount"))
    Sale.objects.filter(kind__in=["subscription", "renewal"]).update(franchisee_income=F("commission_amount"))
    Sale.objects.filter(list_amount=0).update(list_amount=F("amount"))


class Migration(migrations.Migration):
    dependencies = [("clients", "0004_clientproductaccount_discount_percent_and_more")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]

