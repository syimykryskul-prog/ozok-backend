from django.db import migrations

IN = [("subscription", "Абонплата клиентов", True, False), ("franchise_fee", "Взносы за франшизу", True, False),
      ("direct_sales", "Прямые продажи OZOK", True, False), ("nbs_lead_share", "Доля OZOK по лидам OZOK", True, False),
      ("withholdings", "Удержания с продаж", True, False), ("other_income", "Прочие поступления", False, True)]
OUT = [("commission_payouts", "Выплаты комиссий франчайзи", True, False),
       ("recruiter_rewards", "Вознаграждения рекрутерам", True, False),
       ("implementer_pay", "Оплата внедренцам", True, False), ("rent", "Аренда", False, True),
       ("salary", "Зарплата", False, True), ("nbs_ads", "Реклама OZOK", False, True),
       ("equipment_purchase", "Закупка оборудования", False, True), ("taxes", "Налоги", False, True),
       ("other_expense", "Прочие расходы", False, True)]
ACCOUNTS = [("Наличная касса", "cash"), ("Банк Элдик", "bank"), ("Элкюар", "elqr")]


def forwards(apps, schema_editor):
    Cat = apps.get_model("billing", "CashCategory")
    Acc = apps.get_model("billing", "CashAccount")
    for direction, rows in (("in", IN), ("out", OUT)):
        for i, (code, name, system, manual) in enumerate(rows):
            Cat.objects.get_or_create(code=code, defaults=dict(direction=direction, name=name, is_system=system,
                                                               manual_allowed=manual, sort_order=i))
    for name, kind in ACCOUNTS:
        Acc.objects.get_or_create(name=name, defaults={"kind": kind})


class Migration(migrations.Migration):
    dependencies = [("billing", "0009_cashaccount_cashcategory_cashtransaction_nbsexpense_and_more")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
