"""Справочники и имена пользователей для ролей (M-10): дополнить системные роли."""
from django.db import migrations


def merge(apps, schema_editor):
    from core.access import seed_definitions
    AccessRole = apps.get_model("accounts", "AccessRole")
    for code, d in seed_definitions().items():
        role = AccessRole.objects.filter(code=code, is_system=True).first()
        if role is None:
            continue
        actions = sorted(set(role.actions or []) | set(d["actions"]))
        perms = {**d["permissions"], **(role.permissions or {})}
        if actions != role.actions or perms != role.permissions:
            role.actions, role.permissions = actions, perms
            role.save(update_fields=["actions", "permissions"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0008_system_roles_new_actions")]
    operations = [migrations.RunPython(merge, migrations.RunPython.noop)]
