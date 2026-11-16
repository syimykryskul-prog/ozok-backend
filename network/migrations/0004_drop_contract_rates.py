from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("network", "0003_rules_data")]
    operations = [
        migrations.RemoveField(model_name="contract", name="commission_percent"),
        migrations.RemoveField(model_name="contract", name="installments"),
    ]

