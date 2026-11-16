from django.utils.translation import gettext_lazy
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel, today

SLA_HOURS = {"critical": 1, "high": 4, "normal": 24, "low": 72}


class Ticket(TimeStampedModel):
    class Priority(models.TextChoices):
        LOW = "low", gettext_lazy("Низкий")
        NORMAL = "normal", gettext_lazy("Обычный")
        HIGH = "high", gettext_lazy("Высокий")
        CRITICAL = "critical", gettext_lazy("Критичный")

    class Status(models.TextChoices):
        OPEN = "open", gettext_lazy("Открыто")
        IN_PROGRESS = "in_progress", gettext_lazy("В работе")
        RESOLVED = "resolved", gettext_lazy("Решено")
        CLOSED = "closed", gettext_lazy("Закрыто")

    franchisee = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, related_name="tickets")
    client = models.ForeignKey("clients.Client", on_delete=models.CASCADE, related_name="tickets")
    subject = models.CharField(gettext_lazy("Тема"), max_length=200)
    description = models.TextField(gettext_lazy("Описание"), blank=True)
    priority = models.CharField(gettext_lazy("Приоритет"), max_length=10, choices=Priority.choices, default=Priority.NORMAL)
    status = models.CharField(gettext_lazy("Статус"), max_length=12, choices=Status.choices, default=Status.OPEN)
    opened_at = models.DateTimeField(default=timezone.now)
    first_response_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    rating = models.PositiveSmallIntegerField(null=True, blank=True)
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="assigned_tickets")

    class Meta:
        verbose_name = gettext_lazy("Обращение")
        verbose_name_plural = gettext_lazy("Обращения")
        ordering = ("-opened_at", "-id")

    @property
    def sla_hours(self):
        return SLA_HOURS[self.priority]

    def is_sla_breached(self, now=None):
        end = self.first_response_at or now or timezone.now()
        return end - self.opened_at > timedelta(hours=self.sla_hours)

    @property
    def sla_breached(self):
        return self.is_sla_breached()


class Audit(TimeStampedModel):
    franchisee = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, related_name="audits")
    date = models.DateField(gettext_lazy("Дата"), default=today)
    inspector = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="audits")
    items = models.JSONField(gettext_lazy("Чек-лист"), default=list)
    score = models.PositiveSmallIntegerField(gettext_lazy("Результат, %"), default=0)
    summary = models.TextField(gettext_lazy("Итог"), blank=True)

    class Meta:
        verbose_name = gettext_lazy("Проверка")
        verbose_name_plural = gettext_lazy("Проверки")
        ordering = ("-date", "-id")
