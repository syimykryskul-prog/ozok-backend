from django.utils.translation import gettext_lazy
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel


class IntegrationEvent(TimeStampedModel):
    """Журнал событий, пришедших из продуктов (раздел 5Б)."""

    class Status(models.TextChoices):
        PROCESSED = "processed", gettext_lazy("Обработано")
        DUPLICATE = "duplicate", gettext_lazy("Дубликат")
        UNMATCHED = "unmatched", gettext_lazy("Не распознано")
        ERROR = "error", gettext_lazy("Ошибка")

    provider = models.CharField(gettext_lazy("Источник"), max_length=12, default="product",
                                choices=[("product", gettext_lazy("Продукт")), ("elqr", gettext_lazy("Элкюар"))])
    product = models.ForeignKey("network.Product", on_delete=models.CASCADE, null=True, blank=True,
                                related_name="events")
    event_type = models.CharField(gettext_lazy("Тип события"), max_length=40)
    external_id = models.CharField(gettext_lazy("ID события"), max_length=120, blank=True)
    payload = models.JSONField(gettext_lazy("Данные"), default=dict)
    status = models.CharField(gettext_lazy("Статус"), max_length=12, choices=Status.choices)
    error = models.TextField(gettext_lazy("Ошибка"), blank=True)
    client = models.ForeignKey("clients.Client", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="integration_events")
    franchisee = models.ForeignKey("network.Franchisee", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="integration_events")
    received_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = gettext_lazy("Событие интеграции")
        verbose_name_plural = gettext_lazy("События интеграции")
        ordering = ("-received_at", "-id")
        indexes = [models.Index(fields=("product", "external_id"))]
