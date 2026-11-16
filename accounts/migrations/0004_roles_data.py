"""P1: старые роли -> роли ТЗ NBS (hq_admin→super_admin, hq_curator→network_head, hq_accountant→finance,
fr_owner→franchisee, fr_manager→franchisee_staff)."""
from django.db import migrations

MAP = {"hq_admin": "super_admin", "hq_curator": "network_head", "hq_accountant": "finance",
       "fr_owner": "franchisee", "fr_manager": "franchisee_staff"}


def forwards(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    for old, new in MAP.items():
        User.objects.filter(role=old).update(role=new)


def backwards(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    for old, new in MAP.items():
        User.objects.filter(role=new).update(role=old)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_user_otp_enabled_user_otp_last_step_user_otp_secret_and_more")]
    operations = [migrations.RunPython(forwards, backwards)]
