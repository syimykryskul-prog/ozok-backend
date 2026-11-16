from django.utils.translation import gettext, gettext_lazy
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError(gettext("Укажите email"))
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("role", User.Role.SUPER_ADMIN)
        return self.create_user(email, password, **extra)


class AccessRole(models.Model):
    """Роль доступа (P0+ п.8): матрица «раздел → none|read|write», особые права и область видимости.
    Системные роли (code = код роли ТЗ OZOK) создаются сидером и повторяют прежнюю матрицу core/permissions.py."""

    class Scope(models.TextChoices):
        ALL = "all", gettext_lazy("Все данные")
        OWN_REGION = "own_region", gettext_lazy("Свой регион")
        OWN_PARTNERS = "own_partners", gettext_lazy("Свои партнёры (привлечённые)")
        ASSIGNED = "assigned", gettext_lazy("Только назначенное")

    code = models.SlugField(gettext_lazy("Код"), max_length=60, unique=True)
    name = models.CharField(gettext_lazy("Название"), max_length=150)
    name_ky = models.CharField(gettext_lazy("Перевод (кыргызча)"), max_length=150, blank=True, default="")
    description = models.CharField(gettext_lazy("Описание"), max_length=500, blank=True)
    description_ky = models.CharField(gettext_lazy("Перевод (кыргызча)"), max_length=500, blank=True, default="")
    is_system = models.BooleanField(gettext_lazy("Системная"), default=False)
    base_role = models.CharField(gettext_lazy("Базовая роль (тип пользователя)"), max_length=20, default="franchisee_staff")
    permissions = models.JSONField(gettext_lazy("Права по разделам"), default=dict, blank=True)
    actions = models.JSONField(gettext_lazy("Особые права"), default=list, blank=True)
    scope = models.CharField(gettext_lazy("Область видимости"), max_length=14, choices=Scope.choices, default=Scope.ALL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = gettext_lazy("Роль доступа")
        verbose_name_plural = gettext_lazy("Роли доступа")
        ordering = ("id",)

    def __str__(self):
        return self.name


class User(AbstractBaseUser, PermissionsMixin):
    class Role(models.TextChoices):
        SUPER_ADMIN = "super_admin", "Super Admin OZOK"
        NETWORK_HEAD = "network_head", gettext_lazy("Руководитель партнёрской сети")
        RECRUITER = "recruiter", gettext_lazy("Рекрутер")
        REGIONAL_SUPPORT = "regional_support", gettext_lazy("Региональная техподдержка")
        IMPLEMENTER = "implementer", gettext_lazy("Разработчик / внедренец")
        FINANCE = "finance", gettext_lazy("Финансист OZOK")
        QUALITY = "quality", gettext_lazy("Контроль качества")
        FRANCHISEE = "franchisee", gettext_lazy("Франчайзи")
        FRANCHISEE_STAFF = "franchisee_staff", gettext_lazy("Сотрудник франчайзи")
        MANAGER = "manager", gettext_lazy("Менеджер")

    class Language(models.TextChoices):
        RU = "ru", gettext_lazy("Русский")
        KY = "ky", gettext_lazy("Кыргызча")

    class Theme(models.TextChoices):
        LIGHT = "light", gettext_lazy("Светлая")
        DARK = "dark", gettext_lazy("Тёмная")
        SYSTEM = "system", gettext_lazy("Как в системе")

    email = models.EmailField("Email", unique=True)
    first_name = models.CharField(gettext_lazy("Имя"), max_length=100, blank=True)
    last_name = models.CharField(gettext_lazy("Фамилия"), max_length=100, blank=True)
    phone = models.CharField(gettext_lazy("Телефон"), max_length=32, blank=True)
    role = models.CharField(gettext_lazy("Роль"), max_length=20, choices=Role.choices, default=Role.FRANCHISEE_STAFF)
    franchisee = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="users", verbose_name=gettext_lazy("Франчайзи"))
    region = models.CharField(gettext_lazy("Регион (для региональной техподдержки)"), max_length=100, blank=True)
    recruiter_reward_enabled = models.BooleanField(gettext_lazy("Получает вознаграждение рекрутера"), default=False)
    access_role = models.ForeignKey(AccessRole, on_delete=models.PROTECT, null=True, blank=True,
                                    related_name="users", verbose_name=gettext_lazy("Роль доступа"))
    permission_overrides = models.JSONField(gettext_lazy("Индивидуальные отличия от роли"), default=dict, blank=True)
    must_change_password = models.BooleanField(gettext_lazy("Сменить пароль при входе"), default=False)
    invited_by = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="invited")
    otp_secret = models.CharField(max_length=64, blank=True)
    language = models.CharField(gettext_lazy("Язык интерфейса"), max_length=2, choices=Language.choices,
                                default=Language.RU)
    theme = models.CharField(gettext_lazy("Тема оформления"), max_length=6, choices=Theme.choices,
                             default=Theme.SYSTEM)
    otp_enabled = models.BooleanField(gettext_lazy("2FA включена"), default=False)
    otp_last_step = models.BigIntegerField(default=0)
    is_active = models.BooleanField(gettext_lazy("Активен"), default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True)

    objects = UserManager()
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = gettext_lazy("Пользователь")
        verbose_name_plural = gettext_lazy("Пользователи")

    def save(self, *args, **kwargs):
        from core.access import before_user_save
        before_user_save(self)
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip() or self.email

    @property
    def is_hq(self):
        """Сотрудник OZOK (не привязан к партнёру)."""
        return self.franchisee_id is None

    @property
    def partner(self):
        return self.franchisee

    @property
    def partner_id(self):
        return self.franchisee_id

    def __str__(self):
        return self.email

