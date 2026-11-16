"""Клиентское решение: во всех видимых текстах «NBS» -> «OZOK» (данные: статьи кассы, роли, настройки, правила)."""
import re

from django.db import migrations

PAT = re.compile(r"\bNBS\b")


def forwards(apps, schema_editor):
    for app, model, fields in (("billing", "CashCategory", ("name",)), ("billing", "CashAccount", ("name",)),
                               ("accounts", "AccessRole", ("name", "description")),
                               ("core", "SystemSetting", ("label",)),
                               ("network", "CommissionRule", ("name", "basis", "note")),
                               ("network", "FranchiseOffer", ("name",))):
        try:
            M = apps.get_model(app, model)
        except LookupError:
            continue
        for obj in M.objects.all():
            changed = False
            for f in fields:
                v = getattr(obj, f, None)
                if isinstance(v, str) and PAT.search(v):
                    setattr(obj, f, PAT.sub("OZOK", v))
                    changed = True
            if changed:
                obj.save()


class Migration(migrations.Migration):
    dependencies = [("billing", "0011_alter_cashaccount_options_alter_nbsexpense_options_and_more"),
                    ("accounts", "0006_access_roles_data"), ("core", "0001_initial"),
                    ("network", "0009_alter_commissionrule_lead_source_and_more")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
