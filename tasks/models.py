from django.utils.translation import gettext_lazy
from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel, today


class Task(TimeStampedModel):
    class Priority(models.TextChoices):
        LOW = "low", gettext_lazy("Низкий")
        NORMAL = "normal", gettext_lazy("Обычный")
        HIGH = "high", gettext_lazy("Высокий")

    class Status(models.TextChoices):
        NEW = "new", gettext_lazy("Новая")
        IN_PROGRESS = "in_progress", gettext_lazy("В работе")
        DONE = "done", gettext_lazy("Выполнена")
        REJECTED = "rejected", gettext_lazy("Отклонена")
        CANCELLED = "cancelled", gettext_lazy("Отменена")

    ACTIVE_STATUSES = ("new", "in_progress")

    franchisee = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, related_name="tasks")
    title = models.CharField(gettext_lazy("Заголовок"), max_length=200)
    description = models.TextField(gettext_lazy("Описание"), blank=True)
    priority = models.CharField(gettext_lazy("Приоритет"), max_length=8, choices=Priority.choices, default=Priority.NORMAL)
    status = models.CharField(gettext_lazy("Статус"), max_length=12, choices=Status.choices, default=Status.NEW)
    due_date = models.DateField(gettext_lazy("Срок"), null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="created_tasks")
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="tasks")
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = gettext_lazy("Задача")
        verbose_name_plural = gettext_lazy("Задачи")
        ordering = ("-created_at", "-id")

    @property
    def overdue(self):
        return self.due_date is not None and self.due_date < today() and self.status in self.ACTIVE_STATUSES


class TaskComment(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    text = models.TextField(gettext_lazy("Текст"))
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = gettext_lazy("Комментарий к задаче")
        verbose_name_plural = gettext_lazy("Комментарии к задачам")
        ordering = ("created_at", "id")
