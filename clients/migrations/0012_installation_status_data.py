from django.db import migrations

MAP = {"contract": "assigned", "setup": "in_progress", "training": "training", "launched": "done",
       "cancelled": "cancelled"}


def forwards(apps, schema_editor):
    Impl = apps.get_model("clients", "Implementation")
    for stage, status in MAP.items():
        Impl.objects.filter(stage=stage).update(status=status)
    for impl in Impl.objects.filter(implementer__isnull=True, responsible__isnull=False):
        impl.implementer_id = impl.responsible_id
        impl.deadline = impl.planned_launch
        impl.save(update_fields=["implementer", "deadline"])


class Migration(migrations.Migration):
    dependencies = [("clients", "0011_contracttemplate_deal_branch_deal_contract_number_and_more")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
