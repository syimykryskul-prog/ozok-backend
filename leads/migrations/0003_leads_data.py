"""P4: источники и статусы заявок по ТЗ NBS (phone→inbound_call; assigned→new; won→paid)."""
from django.db import migrations


def forwards(apps, schema_editor):
    Lead = apps.get_model("leads", "Lead")
    Lead.objects.filter(source="phone").update(source="inbound_call")
    Lead.objects.filter(status="assigned").update(status="new")
    Lead.objects.filter(status="won").update(status="paid")
    Lead.objects.filter(franchisee__isnull=False).update(owner_confirmed=True)


def backwards(apps, schema_editor):
    Lead = apps.get_model("leads", "Lead")
    Lead.objects.filter(source="inbound_call").update(source="phone")
    Lead.objects.filter(status="paid").update(status="won")
    Lead.objects.filter(status="new", franchisee__isnull=False).update(status="assigned")


class Migration(migrations.Migration):
    dependencies = [("leads", "0002_lead_company_name_lead_contact_name_lead_implementer_and_more")]
    operations = [migrations.RunPython(forwards, backwards)]
