"""P0+: категории доходов и расходов партнёра по умолчанию."""
from django.db import migrations

EXPENSES = [("Аренда", False), ("Зарплата", False), ("Реклама/маркетинг", True), ("Транспорт", False),
            ("Закупка оборудования", False), ("Связь и интернет", False), ("Налоги", False), ("Прочее", False)]
INCOMES = [("Прочий доход", False)]


def forwards(apps, schema_editor):
    Cat = apps.get_model("billing", "LedgerCategory")
    for kind, rows in (("expense", EXPENSES), ("income", INCOMES)):
        for i, (name, marketing) in enumerate(rows, start=1):
            Cat.objects.get_or_create(type=kind, name=name, defaults={"sort_order": i, "is_marketing": marketing})


class Migration(migrations.Migration):
    dependencies = [("billing", "0004_charge_cancel_reason_charge_cancelled_at_and_more")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
