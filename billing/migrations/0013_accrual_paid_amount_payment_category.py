"""D1: погашение начислений старыми ручными выплатами (FIFO); D6: явная категория/начисление платежа франчайзи."""
import django.db.models.deletion
from django.db import migrations, models


def allocate_legacy(apps, schema_editor):
    from billing.legacy import apply_legacy_credit
    Accrual = apps.get_model("billing", "CommissionAccrual")
    Payout = apps.get_model("billing", "CommissionPayout")
    partner_ids = set(Payout.objects.filter(cancelled_at__isnull=True).exclude(method="offset")
                      .values_list("franchisee_id", flat=True))
    for pid in sorted(partner_ids):
        apply_legacy_credit(pid, Accrual, Payout)


class Migration(migrations.Migration):
    dependencies = [("billing", "0012_nbs_to_ozok_labels")]

    operations = [
        migrations.AddField(
            model_name="commissionaccrual", name="paid_amount",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14,
                                      verbose_name="Погашено ручными выплатами (до пакетов выплат)")),
        migrations.AddField(
            model_name="franchiseepayment", name="charge",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                    related_name="payments", to="billing.charge",
                                    verbose_name="Оплачиваемое начисление")),
        migrations.AddField(
            model_name="franchiseepayment", name="income_category",
            field=models.CharField(blank=True, max_length=40, verbose_name="Статья кассы (код)")),
        migrations.RunPython(allocate_legacy, migrations.RunPython.noop),
    ]
