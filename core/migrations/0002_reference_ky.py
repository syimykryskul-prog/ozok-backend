from django.db import migrations

from core import reference_ky


def forwards(apps, schema_editor):
    reference_ky.fill(apps.get_model)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
        ("accounts", "0011_reference_ky"),
        ("billing", "0014_reference_ky"),
        ("network", "0011_reference_ky"),
        ("training", "0002_reference_ky"),
        ("clients", "0016_qa_round2"),
        ("quality", "0001_initial"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
