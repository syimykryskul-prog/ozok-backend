"""Общие базовые вещи: базовая модель, деньги, даты."""
from django.utils.translation import gettext, gettext_lazy
import calendar
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import models
from django.utils import timezone

CENT = Decimal("0.01")


def q2(value):
    """Число -> Decimal с 2 знаками (half-up)."""
    if value is None:
        value = 0
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def money(value):
    """Число -> строка '35000.00' для JSON."""
    return str(q2(value))


def today():
    return timezone.localdate()


def add_months(d, n):
    """Прибавить n месяцев к дате (день обрезается до конца месяца)."""
    idx = d.year * 12 + (d.month - 1) + n
    year, month = divmod(idx, 12)
    month += 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def month_first(d):
    return d.replace(day=1)


def month_last(d):
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def month_label(d):
    return f"{d.year:04d}-{d.month:02d}"


def day_bounds(first, last):
    """[first 00:00, last+1 00:00) в локальном часовом поясе (aware)."""
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(first, time.min), tz)
    end = timezone.make_aware(datetime.combine(last + timedelta(days=1), time.min), tz)
    return start, end


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(gettext_lazy("Создано"), auto_now_add=True)
    updated_at = models.DateTimeField(gettext_lazy("Изменено"), auto_now=True)

    class Meta:
        abstract = True


class ActiveManager(models.Manager):
    """Менеджер по умолчанию: скрывает отменённые записи (финансовые данные не удаляются — H7)."""

    def get_queryset(self):
        return super().get_queryset().filter(cancelled_at__isnull=True)


class CancellableModel(TimeStampedModel):
    """Финансовая запись: только отмена с причиной, без физического удаления."""

    cancelled_at = models.DateTimeField(gettext_lazy("Отменено"), null=True, blank=True)
    cancelled_by = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")
    cancel_reason = models.CharField(gettext_lazy("Причина отмены"), max_length=500, blank=True)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True

    @property
    def is_cancelled(self):
        return self.cancelled_at is not None

    def mark_cancelled(self, user, reason):
        from django.utils import timezone

        from .exceptions import BusinessError
        reason = (reason or "").strip()
        if not reason:
            raise BusinessError(gettext("Укажите причину отмены."))
        if self.cancelled_at is not None:
            raise BusinessError(gettext("Запись уже отменена."))
        self.cancelled_at, self.cancelled_by, self.cancel_reason = timezone.now(), user, reason
        self.save()
        return self


def parse_month(value, default=None):
    """'YYYY-MM' (или 'YYYY-MM-DD') -> дата 1-го числа; пусто -> default или текущий месяц."""
    from .exceptions import BusinessError
    if not value:
        return default or month_first(today())
    try:
        parts = str(value).split("-")
        return date(int(parts[0]), int(parts[1]), 1)
    except (ValueError, IndexError):
        raise BusinessError(gettext("Неверный формат месяца, нужен YYYY-MM."))


class SystemSetting(models.Model):
    """Настройка OZOK (P0): простые параметры; читать только через core.settings_registry.get_setting."""

    key = models.CharField(gettext_lazy("Ключ"), max_length=100, unique=True)
    value = models.JSONField(gettext_lazy("Значение"), default=None, null=True)
    type = models.CharField(gettext_lazy("Тип"), max_length=10, default="str")
    label = models.CharField(gettext_lazy("Название"), max_length=200, blank=True)
    group = models.CharField(gettext_lazy("Группа"), max_length=40, blank=True)
    is_finance = models.BooleanField(gettext_lazy("Финансовый параметр"), default=False)
    updated_by = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = gettext_lazy("Настройка")
        verbose_name_plural = gettext_lazy("Настройки")
        ordering = ("group", "key")

    def __str__(self):
        return self.key


class AuditLog(models.Model):
    """Журнал аудита (P1). Записи не изменяются и не удаляются."""

    class Action(models.TextChoices):
        CREATE = "create", gettext_lazy("Создание")
        UPDATE = "update", gettext_lazy("Изменение")
        CANCEL = "cancel", gettext_lazy("Отмена")
        STATUS = "status", gettext_lazy("Смена статуса")
        LOGIN = "login", gettext_lazy("Вход")
        LOGIN_FAILED = "login_failed", gettext_lazy("Неудачный вход")
        EXPORT = "export", gettext_lazy("Экспорт")
        APPROVE = "approve", gettext_lazy("Одобрение")
        REJECT = "reject", gettext_lazy("Отклонение")

    user = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    user_email = models.CharField(max_length=254, blank=True)
    action = models.CharField(max_length=14, choices=Action.choices)
    entity = models.CharField(gettext_lazy("Сущность"), max_length=80, blank=True)
    entity_id = models.CharField(max_length=40, blank=True)
    changes = models.JSONField(default=dict, blank=True)
    reason = models.CharField(max_length=500, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = gettext_lazy("Запись аудита")
        verbose_name_plural = gettext_lazy("Журнал аудита")
        ordering = ("-created_at", "-id")
        indexes = [models.Index(fields=("entity", "entity_id"))]

    def save(self, *args, **kwargs):
        if self.pk:
            raise RuntimeError(gettext("Записи журнала аудита не изменяются."))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError(gettext("Записи журнала аудита не удаляются."))


class ChangeRequest(models.Model):
    """Запрос на подтверждение OZOK (источник/ответственный лида, скидка ниже минимума и т. п.)."""

    class Status(models.TextChoices):
        PENDING = "pending", gettext_lazy("Ожидает")
        APPROVED = "approved", gettext_lazy("Одобрен")
        REJECTED = "rejected", gettext_lazy("Отклонён")

    requested_by = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, related_name="+")
    entity = models.CharField(gettext_lazy("Сущность"), max_length=80)
    entity_id = models.PositiveIntegerField()
    field = models.CharField(gettext_lazy("Поле"), max_length=60)
    old_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField(null=True, blank=True)
    reason = models.CharField(gettext_lazy("Причина"), max_length=500, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_comment = models.CharField(max_length=500, blank=True)
    franchisee = models.ForeignKey("network.Franchisee", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = gettext_lazy("Запрос на изменение")
        verbose_name_plural = gettext_lazy("Запросы на изменение")
        ordering = ("-created_at", "-id")
