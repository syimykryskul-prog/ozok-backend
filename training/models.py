from django.utils.translation import gettext_lazy
from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel, today


class Course(TimeStampedModel):
    title = models.CharField(gettext_lazy("Название"), max_length=200)
    title_ky = models.CharField(gettext_lazy("Перевод (кыргызча)"), max_length=200, blank=True, default="")
    product = models.ForeignKey("network.Product", on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="courses")
    description = models.TextField(gettext_lazy("Описание"), blank=True)
    description_ky = models.TextField(gettext_lazy("Перевод (кыргызча)"), blank=True, default="")
    materials_url = models.URLField(gettext_lazy("Материалы"), blank=True)
    pass_score = models.PositiveSmallIntegerField(gettext_lazy("Проходной балл"), default=80)
    validity_months = models.PositiveSmallIntegerField(gettext_lazy("Срок действия, мес."), default=12)
    is_active = models.BooleanField(gettext_lazy("Активен"), default=True)

    class Meta:
        verbose_name = gettext_lazy("Курс")
        verbose_name_plural = gettext_lazy("Курсы")
        ordering = ("id",)

    def __str__(self):
        return self.title


class Enrollment(TimeStampedModel):
    class Status(models.TextChoices):
        ASSIGNED = "assigned", gettext_lazy("Назначено")
        IN_PROGRESS = "in_progress", gettext_lazy("В процессе")
        PASSED = "passed", gettext_lazy("Пройдено")
        FAILED = "failed", gettext_lazy("Не пройдено")

    franchisee = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, related_name="enrollments")
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="enrollments")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="enrollments")
    status = models.CharField(gettext_lazy("Статус"), max_length=12, choices=Status.choices, default=Status.ASSIGNED)
    score = models.PositiveSmallIntegerField(gettext_lazy("Балл"), null=True, blank=True)
    assigned_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateField(gettext_lazy("Действует до"), null=True, blank=True)

    class Meta:
        verbose_name = gettext_lazy("Назначение обучения")
        verbose_name_plural = gettext_lazy("Назначения обучения")
        ordering = ("-assigned_at", "-id")
        constraints = [models.UniqueConstraint(fields=("course", "user"), name="uniq_enrollment_course_user")]

    @property
    def expired(self):
        return self.status == self.Status.PASSED and self.expires_at is not None and self.expires_at < today()
