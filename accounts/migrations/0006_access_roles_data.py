"""Сидер системных ролей доступа (матрица core/permissions.py) и привязка существующих пользователей."""
from django.db import migrations


def forwards(apps, schema_editor):
    from core.access import seed_definitions
    AccessRole = apps.get_model("accounts", "AccessRole")
    User = apps.get_model("accounts", "User")
    for code, d in seed_definitions().items():
        AccessRole.objects.get_or_create(code=code, defaults={**d, "is_system": True,
                                                              "description": "Системная роль ТЗ NBS §3"})
    for role in AccessRole.objects.filter(is_system=True):
        User.objects.filter(role=role.code, access_role__isnull=True).update(access_role=role)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0005_accessrole_user_invited_by_user_must_change_password_and_more")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
