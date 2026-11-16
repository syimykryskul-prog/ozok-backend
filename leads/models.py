from django.utils.translation import gettext_lazy
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel

OVERDUE_HOURS = 24  # значение по умолчанию; действующее — настройка leads.overdue_hours


def overdue_hours():
    from core.settings_registry import get_int
    return get_int("leads.overdue_hours")


class Lead(TimeStampedModel):
    class Source(models.TextChoices):
        NBS_ADS = "nbs_ads", gettext_lazy("Реклама OZOK")
        PARTNER_ADS = "partner_ads", gettext_lazy("Реклама франчайзи")
        INSTAGRAM = "instagram", "Instagram"
        WHATSAPP = "whatsapp", "WhatsApp"
        REFERRAL = "referral", gettext_lazy("Рекомендация")
        SITE = "site", gettext_lazy("Сайт")
        INBOUND_CALL = "inbound_call", gettext_lazy("Входящий звонок")
        OFFLINE = "offline", gettext_lazy("Офлайн")
        OTHER = "other", gettext_lazy("Другое")

    class Interest(models.TextChoices):
        PROGRAM = "program", gettext_lazy("Программа")
        FRANCHISE = "franchise", gettext_lazy("Франшиза")
        CONSULT = "consult", gettext_lazy("Консультация")

    class Status(models.TextChoices):
        NEW = "new", gettext_lazy("Новая")
        IN_PROGRESS = "in_progress", gettext_lazy("В работе")
        DEMO = "demo", gettext_lazy("Демо")
        PROPOSAL = "proposal", gettext_lazy("КП")
        CONTRACT = "contract", gettext_lazy("Договор")
        PAID = "paid", gettext_lazy("Оплачено")
        LOST = "lost", gettext_lazy("Отказ")

    OPEN_STATUSES = ("in_progress", "demo", "proposal", "contract")
    PIPELINE = ("in_progress", "demo", "proposal", "contract")

    source = models.CharField(gettext_lazy("Источник"), max_length=14, choices=Source.choices, default=Source.OTHER)
    interest = models.CharField(gettext_lazy("Интерес"), max_length=10, choices=Interest.choices, default=Interest.PROGRAM)
    product = models.CharField(gettext_lazy("Сфера (код), package:<id>, help"), max_length=60, blank=True, default="")
    name = models.CharField(gettext_lazy("Имя"), max_length=150)
    company_name = models.CharField(gettext_lazy("Компания"), max_length=200, blank=True)
    contact_name = models.CharField(gettext_lazy("Контактное лицо"), max_length=150, blank=True)
    phone = models.CharField(gettext_lazy("Телефон"), max_length=32)
    city = models.CharField(gettext_lazy("Город"), max_length=100, blank=True)
    region = models.CharField(gettext_lazy("Регион"), max_length=100, blank=True)
    comment = models.TextField(gettext_lazy("Комментарий"), blank=True)
    status = models.CharField(gettext_lazy("Статус"), max_length=12, choices=Status.choices, default=Status.NEW)
    franchisee = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="leads")
    implementer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="implementer_leads", verbose_name=gettext_lazy("Ответственный за внедрение"))
    next_contact_at = models.DateTimeField(gettext_lazy("Следующий контакт"), null=True, blank=True)
    owner_confirmed = models.BooleanField(gettext_lazy("Владелец лида подтверждён OZOK"), default=True)
    owner_confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                           blank=True, related_name="+")
    owner_confirmed_at = models.DateTimeField(null=True, blank=True)
    assigned_at = models.DateTimeField(null=True, blank=True)
    taken_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    lost_reason = models.CharField(gettext_lazy("Причина отказа"), max_length=255, blank=True)
    client = models.ForeignKey("clients.Client", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="won_leads")

    class Meta:
        verbose_name = gettext_lazy("Заявка")
        verbose_name_plural = gettext_lazy("Заявки")
        ordering = ("-created_at", "-id")

    @property
    def overdue(self):
        """SLA заявки (M2): назначена партнёру и не взята в работу дольше N часов, либо пропущен next_contact_at."""
        now = timezone.now()
        if (self.status == self.Status.NEW and self.franchisee_id and self.assigned_at is not None
                and self.assigned_at < now - timedelta(hours=overdue_hours())):
            return True
        return (self.status in self.OPEN_STATUSES and self.next_contact_at is not None
                and self.next_contact_at < now)

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"


class LeadActivity(models.Model):
    class Type(models.TextChoices):
        CALL = "call", gettext_lazy("Звонок")
        MEETING = "meeting", gettext_lazy("Встреча")
        DEMO = "demo", gettext_lazy("Демо")
        PROPOSAL = "proposal", gettext_lazy("КП")
        NOTE = "note", gettext_lazy("Заметка")
        STATUS = "status", gettext_lazy("Смена статуса")
        REASSIGN = "reassign", gettext_lazy("Переназначение")
        CHANGE_REQUEST = "change_request", gettext_lazy("Запрос на изменение")

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="activities")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="+")
    type = models.CharField(max_length=16, choices=Type.choices, default=Type.NOTE)
    text = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
