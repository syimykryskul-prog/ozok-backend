from django.utils.translation import gettext_lazy
from django.db import models

from core.models import TimeStampedModel


class SalesPlan(TimeStampedModel):
    franchisee = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, related_name="plans")
    month = models.DateField(gettext_lazy("Месяц (1-е число)"))
    target_clients = models.PositiveIntegerField(gettext_lazy("План: новые клиенты"), default=0)
    target_revenue = models.DecimalField(gettext_lazy("План: сумма продаж"), max_digits=14, decimal_places=2, default=0)

    class Meta:
        verbose_name = gettext_lazy("План продаж")
        verbose_name_plural = gettext_lazy("Планы продаж")
        ordering = ("-month", "franchisee_id")
        constraints = [models.UniqueConstraint(fields=("franchisee", "month"), name="uniq_plan_franchisee_month")]
