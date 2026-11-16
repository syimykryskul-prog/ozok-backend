from django.utils.translation import gettext_lazy
import secrets

from django.conf import settings
from django.db import models

from core.models import TimeStampedModel, today


PALETTE = ["#00BEBA", "#2F6FDE", "#C98A0B", "#D64545", "#1E9E6A", "#7A4FD6", "#E0663A", "#091C35"]
ICONS = ["shopping-cart", "truck", "calendar-check", "coffee", "store", "briefcase", "scissors",
         "stethoscope", "graduation-cap", "car", "box"]


class Product(TimeStampedModel):
    """Продукт = сфера. Полностью управляется центром (hq_admin)."""

    code = models.SlugField(gettext_lazy("Код"), max_length=40, unique=True)
    name = models.CharField(gettext_lazy("Название"), max_length=120)
    name_ky = models.CharField(gettext_lazy("Перевод (кыргызча)"), max_length=120, blank=True, default="")
    sphere = models.CharField(gettext_lazy("Сфера"), max_length=80, blank=True)
    sphere_ky = models.CharField(gettext_lazy("Перевод (кыргызча)"), max_length=80, blank=True, default="")
    description = models.TextField(gettext_lazy("Описание"), blank=True)
    description_ky = models.TextField(gettext_lazy("Перевод (кыргызча)"), blank=True, default="")
    icon = models.CharField(gettext_lazy("Иконка"), max_length=30, choices=[(i, i) for i in ICONS], default="box")
    color = models.CharField(gettext_lazy("Цвет"), max_length=7, blank=True)
    price_cash = models.DecimalField(gettext_lazy("Цена франшизы (оплата сразу)"), max_digits=14, decimal_places=2, default=0)
    price_per_sale = models.DecimalField(gettext_lazy("Цена франшизы (из продаж)"), max_digits=14, decimal_places=2, default=0)
    setup_price = models.DecimalField(gettext_lazy("Установка и обучение (рекомендуемая)"), max_digits=14, decimal_places=2,
                                      default=0)
    training_price = models.DecimalField(gettext_lazy("Обучение (рекомендуемая)"), max_digits=14, decimal_places=2, default=0)
    min_setup_price = models.DecimalField(gettext_lazy("Минимальная цена установки/обучения"), max_digits=14, decimal_places=2,
                                          null=True, blank=True)
    min_training_price = models.DecimalField(gettext_lazy("Минимальная цена обучения (пусто — без минимума)"), max_digits=14,
                                             decimal_places=2, null=True, blank=True)
    max_discount_percent = models.DecimalField(gettext_lazy("Макс. скидка, % (пусто — из настроек)"), max_digits=5,
                                               decimal_places=2, null=True, blank=True)
    grace_days = models.PositiveSmallIntegerField(gettext_lazy("Льготный период, дней (пусто — из настроек)"), null=True,
                                                  blank=True)
    restrict_after_days = models.PositiveSmallIntegerField(gettext_lazy("Ограничение через N дней просрочки (пусто — из настроек)"),
                                                           null=True, blank=True)
    payment_source = models.CharField(gettext_lazy("Источник оплат абонплаты"), max_length=8, default="product",
                                      choices=[("elqr", gettext_lazy("Элкюар")), ("product", gettext_lazy("Продукт")), ("manual", gettext_lazy("Вручную / банк"))])
    default_commission_percent = models.DecimalField(gettext_lazy("Вознаграждение по умолчанию, %"), max_digits=5,
                                                     decimal_places=2, default=20)
    sort_order = models.PositiveIntegerField(gettext_lazy("Порядок"), default=0)
    is_active = models.BooleanField(gettext_lazy("Активен"), default=True)
    integration_enabled = models.BooleanField(gettext_lazy("Интеграция включена"), default=False)
    api_key = models.CharField(gettext_lazy("API-ключ интеграции"), max_length=80, blank=True, editable=False)
    last_event_at = models.DateTimeField(gettext_lazy("Последнее событие"), null=True, blank=True)

    class Meta:
        verbose_name = gettext_lazy("Продукт (сфера)")
        verbose_name_plural = gettext_lazy("Продукты (сферы)")
        ordering = ("sort_order", "id")

    @staticmethod
    def generate_key():
        return "ozk_" + secrets.token_urlsafe(32)

    def save(self, *args, **kwargs):
        if not self.api_key:
            self.api_key = self.generate_key()
        if not self.color:
            self.color = PALETTE[Product.objects.count() % len(PALETTE)]
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Package(TimeStampedModel):
    """Пакет нескольких сфер."""

    name = models.CharField(gettext_lazy("Название"), max_length=150)
    name_ky = models.CharField(gettext_lazy("Перевод (кыргызча)"), max_length=150, blank=True, default="")
    products = models.ManyToManyField(Product, related_name="packages", verbose_name=gettext_lazy("Сферы"))
    price_cash = models.DecimalField(gettext_lazy("Цена (оплата сразу)"), max_digits=14, decimal_places=2, default=0)
    price_prepay = models.DecimalField(gettext_lazy("Цена (с предоплатой)"), max_digits=14, decimal_places=2, default=0)
    price_per_sale = models.DecimalField(gettext_lazy("Цена (из продаж)"), max_digits=14, decimal_places=2, default=0)
    prepay_percent = models.DecimalField(gettext_lazy("Предоплата, %"), max_digits=5, decimal_places=2, default=40)
    is_active = models.BooleanField(gettext_lazy("Активен"), default=True)

    class Meta:
        verbose_name = gettext_lazy("Пакет")
        verbose_name_plural = gettext_lazy("Пакеты")
        ordering = ("id",)

    def __str__(self):
        return self.name

class Franchisee(TimeStampedModel):
    """Партнёр OZOK: кандидат / стажёр / менеджер / франчайзи / приостановлен / закрыт (P1)."""

    class Status(models.TextChoices):
        CANDIDATE = "candidate", gettext_lazy("Кандидат")
        TRAINEE = "trainee", gettext_lazy("Стажёр")
        MANAGER = "manager", gettext_lazy("Менеджер")
        FRANCHISEE = "franchisee", gettext_lazy("Франчайзи")
        SUSPENDED = "suspended", gettext_lazy("Приостановлен")
        CLOSED = "closed", gettext_lazy("Закрыт")

    class Tier(models.TextChoices):
        STANDARD = "standard", "Standard"
        GROWTH = "growth", "Growth"

    class Stage(models.TextChoices):
        NEW_APPLICATION = "new_application", gettext_lazy("Новая заявка")
        INTERVIEW = "interview", gettext_lazy("Собеседование")
        TRAINING = "training", gettext_lazy("Обучение")
        EXAM = "exam", gettext_lazy("Экзамен")
        MANAGER = "manager", gettext_lazy("Менеджер")
        FIRST_SALE = "first_sale", gettext_lazy("1-я продажа")
        SECOND_SALE = "second_sale", gettext_lazy("2-я продажа")
        THIRD_SALE = "third_sale", gettext_lazy("3-я продажа")
        FRANCHISE_CONTRACT = "franchise_contract", gettext_lazy("Договор франшизы")
        ACTIVATED = "activated", gettext_lazy("Активирован")

    class Certification(models.TextChoices):
        PASSED = "passed", gettext_lazy("Сдал")
        FAILED = "failed", gettext_lazy("Не сдал")

    STAGE_ORDER = tuple(v for v, _ in Stage.choices)
    # статус партнёра, соответствующий этапу воронки
    STAGE_STATUS = {"new_application": "candidate", "interview": "candidate", "training": "trainee",
                    "exam": "trainee", "manager": "manager", "first_sale": "manager", "second_sale": "manager",
                    "third_sale": "manager", "franchise_contract": "manager", "activated": "franchisee"}
    PROTECTED_FIELDS = ("inn", "passport")

    name = models.CharField(gettext_lazy("Название"), max_length=150)
    partner_code = models.CharField(gettext_lazy("Код партнёра"), max_length=40, unique=True, null=True, blank=True)
    legal_name = models.CharField(gettext_lazy("Юрлицо"), max_length=255, blank=True)
    full_name = models.CharField(gettext_lazy("ФИО"), max_length=200, blank=True)
    inn = models.CharField(gettext_lazy("ИНН (защищённое)"), max_length=32, blank=True)
    passport = models.CharField(gettext_lazy("Паспорт (защищённое)"), max_length=64, blank=True)
    city = models.CharField(gettext_lazy("Город"), max_length=100)
    region = models.CharField(gettext_lazy("Регион"), max_length=100, blank=True)
    cities = models.JSONField(gettext_lazy("Города покрытия"), default=list, blank=True)
    address = models.CharField(gettext_lazy("Адрес"), max_length=255, blank=True)
    phone = models.CharField(gettext_lazy("Телефон"), max_length=32, blank=True)
    email = models.EmailField("Email", blank=True)
    owner_name = models.CharField(gettext_lazy("Владелец"), max_length=150, blank=True)
    specialization = models.ManyToManyField(Product, blank=True, related_name="specialists",
                                            verbose_name=gettext_lazy("Специализация"))
    candidate_source = models.CharField(gettext_lazy("Источник кандидата"), max_length=30, blank=True)
    recruited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="recruited_partners", verbose_name=gettext_lazy("Привлёк (рекрутер)"))
    responsible = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="responsible_partners", verbose_name=gettext_lazy("Ответственный OZOK"))
    status = models.CharField(gettext_lazy("Статус"), max_length=12, choices=Status.choices, default=Status.CANDIDATE)
    tier = models.CharField(gettext_lazy("Уровень"), max_length=10, choices=Tier.choices, default=Tier.STANDARD)
    funnel_stage = models.CharField(gettext_lazy("Этап воронки"), max_length=20, choices=Stage.choices,
                                    default=Stage.NEW_APPLICATION)
    funnel_stage_since = models.DateField(gettext_lazy("На этапе с"), default=today)
    training_date = models.DateField(gettext_lazy("Дата обучения"), null=True, blank=True)
    certification_result = models.CharField(gettext_lazy("Результат аттестации"), max_length=8, choices=Certification.choices,
                                            blank=True, default="")
    certification_date = models.DateField(gettext_lazy("Дата аттестации"), null=True, blank=True)
    suspended_at = models.DateField(gettext_lazy("Приостановлен"), null=True, blank=True)
    closed_at = models.DateField(gettext_lazy("Закрыт"), null=True, blank=True)
    status_reason = models.CharField(gettext_lazy("Причина статуса"), max_length=500, blank=True)
    curator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="curated_franchisees", verbose_name=gettext_lazy("Куратор"))
    joined_at = models.DateField(gettext_lazy("Дата вступления"), null=True, blank=True)
    note = models.TextField(gettext_lazy("Заметка"), blank=True)

    class Meta:
        verbose_name = gettext_lazy("Партнёр")
        verbose_name_plural = gettext_lazy("Партнёры")
        ordering = ("id",)

    def __str__(self):
        return self.name


class PartnerStatusHistory(TimeStampedModel):
    """История статуса / уровня / этапа воронки партнёра. Статус и уровень действуют с effective_from."""

    class Field(models.TextChoices):
        STATUS = "status", gettext_lazy("Статус")
        TIER = "tier", gettext_lazy("Уровень")
        FUNNEL_STAGE = "funnel_stage", gettext_lazy("Этап воронки")

    partner = models.ForeignKey(Franchisee, on_delete=models.CASCADE, related_name="history")
    field = models.CharField(max_length=14, choices=Field.choices)
    old = models.CharField(max_length=30, blank=True)
    new = models.CharField(max_length=30)
    effective_from = models.DateField(gettext_lazy("Действует с"), default=today)
    reason = models.CharField(gettext_lazy("Причина"), max_length=500, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    applied = models.BooleanField(default=True)

    class Meta:
        verbose_name = gettext_lazy("История статуса партнёра")
        verbose_name_plural = gettext_lazy("История статусов партнёров")
        ordering = ("-effective_from", "-id")


def default_withhold_percent():
    """Удержание по умолчанию — из настройки franchise.default_withhold_percent (не зашито в коде)."""
    from core.settings_registry import get_decimal
    return get_decimal("franchise.default_withhold_percent")


class FranchiseOffer(TimeStampedModel):
    """Предложение франшизы (P3): «Сразу», «Рассрочка до…»; суммы в сомах, настраиваются."""

    name = models.CharField(gettext_lazy("Название"), max_length=150)
    name_ky = models.CharField(gettext_lazy("Перевод (кыргызча)"), max_length=150, blank=True, default="")
    code = models.CharField(gettext_lazy("Код (cash/installment — суммы из настроек)"), max_length=20, blank=True)
    total = models.DecimalField(gettext_lazy("Стоимость"), max_digits=14, decimal_places=2)
    upfront = models.DecimalField(gettext_lazy("Сразу"), max_digits=14, decimal_places=2, default=0)
    installments_count = models.PositiveSmallIntegerField(gettext_lazy("Платежей рассрочки"), default=0)
    step_months = models.PositiveSmallIntegerField(gettext_lazy("Шаг, мес."), default=1)
    is_active = models.BooleanField(gettext_lazy("Активно"), default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = gettext_lazy("Предложение франшизы")
        verbose_name_plural = gettext_lazy("Предложения франшизы")
        ordering = ("sort_order", "id")

    def __str__(self):
        return self.name


class Contract(TimeStampedModel):
    class Scheme(models.TextChoices):
        CASH = "cash", gettext_lazy("Наличные")
        PREPAY = "prepay", gettext_lazy("Предоплата")
        PER_SALE = "per_sale", gettext_lazy("Оплата с каждой продажи")
        HYBRID = "hybrid", gettext_lazy("Гибридная")

    class Status(models.TextChoices):
        DRAFT = "draft", gettext_lazy("Черновик")
        ACTIVE = "active", gettext_lazy("Действует")
        CLOSED = "closed", gettext_lazy("Закрыт")

    franchisee = models.ForeignKey(Franchisee, on_delete=models.PROTECT, related_name="contracts")
    number = models.CharField(gettext_lazy("Номер"), max_length=32, unique=True, blank=True)
    signed_at = models.DateField(gettext_lazy("Дата подписания"), default=today)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, null=True, blank=True,
                                related_name="direct_contracts", verbose_name=gettext_lazy("Сфера"))
    package = models.ForeignKey(Package, on_delete=models.PROTECT, null=True, blank=True,
                                related_name="contracts", verbose_name=gettext_lazy("Пакет"))
    products = models.ManyToManyField(Product, related_name="contracts", verbose_name=gettext_lazy("Сферы (авто)"))
    payment_scheme = models.CharField(gettext_lazy("Схема оплаты"), max_length=10, choices=Scheme.choices, default=Scheme.CASH)
    list_price = models.DecimalField(gettext_lazy("Прайсовая сумма"), max_digits=14, decimal_places=2, default=0)
    fee_total = models.DecimalField(gettext_lazy("Сумма взноса"), max_digits=14, decimal_places=2, default=0)
    prepay_percent = models.DecimalField(gettext_lazy("Предоплата, % (для пресета)"), max_digits=5, decimal_places=2, default=40)
    # составная оплата франшизы (5Д): сразу + рассрочка + остаток удержаниями из дохода
    upfront_amount = models.DecimalField(gettext_lazy("Сразу, сумма"), max_digits=14, decimal_places=2, default=0)
    upfront_due = models.DateField(gettext_lazy("Срок платежа «сразу»"), null=True, blank=True)
    installments_total = models.DecimalField(gettext_lazy("Рассрочка, сумма"), max_digits=14, decimal_places=2, default=0)
    installments_count = models.PositiveSmallIntegerField(gettext_lazy("Платежей рассрочки"), default=0)
    installment_step_months = models.PositiveSmallIntegerField(gettext_lazy("Шаг рассрочки, мес."), default=1)
    withhold_percent = models.DecimalField(gettext_lazy("Удержание с дохода, %"), max_digits=5, decimal_places=2,
                                           default=default_withhold_percent)
    offer = models.ForeignKey("FranchiseOffer", on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="contracts", verbose_name=gettext_lazy("Предложение франшизы"))
    discount_note = models.CharField(gettext_lazy("Основание скидки"), max_length=255, blank=True)
    status = models.CharField(gettext_lazy("Статус"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    valid_until = models.DateField(gettext_lazy("Действует до"), null=True, blank=True)
    note = models.TextField(gettext_lazy("Заметка"), blank=True)

    class Meta:
        verbose_name = gettext_lazy("Договор")
        verbose_name_plural = gettext_lazy("Договоры")
        ordering = ("-signed_at", "-id")

    def save(self, *args, **kwargs):
        if not self.number:
            prefix = f"D-{(self.signed_at or today()).year}-"
            last = Contract.objects.filter(number__startswith=prefix).order_by("-number").first()
            seq = int(last.number.rsplit("-", 1)[-1]) + 1 if last else 1
            self.number = f"{prefix}{seq:03d}"
        super().save(*args, **kwargs)

    @property
    def withhold_target(self):
        """Часть франшизы, погашаемая удержаниями из дохода: fee_total − сразу − рассрочка."""
        return max(self.fee_total - self.upfront_amount - self.installments_total, 0)

    def __str__(self):
        return self.number


class Tariff(TimeStampedModel):
    """Тариф абонплаты продукта. Цена хранится версиями (TariffPrice)."""

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="tariffs")
    code = models.SlugField(gettext_lazy("Код тарифа"), max_length=40)
    name = models.CharField(gettext_lazy("Название"), max_length=120)
    name_ky = models.CharField(gettext_lazy("Перевод (кыргызча)"), max_length=120, blank=True, default="")
    description = models.TextField(gettext_lazy("Описание"), blank=True)
    description_ky = models.TextField(gettext_lazy("Перевод (кыргызча)"), blank=True, default="")
    min_price = models.DecimalField(gettext_lazy("Минимальная цена в месяц"), max_digits=14, decimal_places=2, null=True,
                                    blank=True)
    max_discount_percent = models.DecimalField(gettext_lazy("Макс. скидка, % (пусто — как у продукта)"), max_digits=5,
                                               decimal_places=2, null=True, blank=True)
    grace_days = models.PositiveSmallIntegerField(gettext_lazy("Льготный период, дней (пусто — как у продукта)"), null=True,
                                                  blank=True)
    restrict_after_days = models.PositiveSmallIntegerField(gettext_lazy("Ограничение через N дней (пусто — как у продукта)"),
                                                           null=True, blank=True)
    is_active = models.BooleanField(gettext_lazy("Активен"), default=True)
    sort_order = models.PositiveIntegerField(gettext_lazy("Порядок"), default=0)

    class Meta:
        verbose_name = gettext_lazy("Тариф")
        verbose_name_plural = gettext_lazy("Тарифы")
        ordering = ("product_id", "sort_order", "id")
        constraints = [models.UniqueConstraint(fields=("product", "code"), name="uniq_tariff_code_per_product")]

    def __str__(self):
        return f"{self.product.code}/{self.code}"


class TariffPrice(TimeStampedModel):
    """Версия цены тарифа: действует с valid_from по valid_to (включительно; null — бессрочно)."""

    tariff = models.ForeignKey(Tariff, on_delete=models.CASCADE, related_name="prices")
    monthly_price = models.DecimalField(gettext_lazy("Цена в месяц"), max_digits=14, decimal_places=2)
    valid_from = models.DateField(gettext_lazy("Действует с"), default=today)
    valid_to = models.DateField(gettext_lazy("Действует по"), null=True, blank=True)

    class Meta:
        verbose_name = gettext_lazy("Цена тарифа")
        verbose_name_plural = gettext_lazy("История цен тарифов")
        ordering = ("-valid_from", "-id")


class Equipment(TimeStampedModel):
    """Справочник оборудования центра."""

    name = models.CharField(gettext_lazy("Название"), max_length=150)
    name_ky = models.CharField(gettext_lazy("Перевод (кыргызча)"), max_length=150, blank=True, default="")
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="equipment", verbose_name=gettext_lazy("Сфера (пусто — для всех)"))
    price = models.DecimalField(gettext_lazy("Рекомендуемая цена"), max_digits=14, decimal_places=2, default=0)
    min_price = models.DecimalField(gettext_lazy("Минимальная цена"), max_digits=14, decimal_places=2, null=True, blank=True)
    purchase_price = models.DecimalField(gettext_lazy("Закупочная цена"), max_digits=14, decimal_places=2, default=0)
    warranty_months = models.PositiveSmallIntegerField(gettext_lazy("Гарантия, мес."), default=0)
    unit = models.CharField(gettext_lazy("Единица"), max_length=20, default="шт")
    is_active = models.BooleanField(gettext_lazy("Активно"), default=True)
    sort_order = models.PositiveIntegerField(gettext_lazy("Порядок"), default=0)

    class Meta:
        verbose_name = gettext_lazy("Оборудование")
        verbose_name_plural = gettext_lazy("Оборудование")
        ordering = ("sort_order", "id")

    @property
    def sale_price(self):
        return self.price

    @property
    def margin(self):
        return self.price - self.purchase_price

    def __str__(self):
        return self.name


class CommissionRule(TimeStampedModel):
    """Правило дохода франчайзи (5Д). Область — любые из product/tariff/franchisee/sale_kind (пусто = любой)."""

    class SaleKind(models.TextChoices):
        SUBSCRIPTION = "subscription", gettext_lazy("Абонплата (подписка и продление)")
        SETUP = "setup", gettext_lazy("Установка и обучение")
        TRAINING = "training", gettext_lazy("Обучение (отдельно от установки)")
        EQUIPMENT = "equipment", gettext_lazy("Оборудование")
        OTHER = "other", gettext_lazy("Прочее")

    class FixedPer(models.TextChoices):
        SALE = "sale", gettext_lazy("За продажу")
        MONTH = "month", gettext_lazy("За оплаченный месяц")

    class Base(models.TextChoices):
        PAID = "paid", gettext_lazy("От оплаченного")
        LIST = "list", gettext_lazy("От прайсовой суммы")

    name = models.CharField(gettext_lazy("Название"), max_length=200)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, null=True, blank=True, related_name="rules")
    tariff = models.ForeignKey(Tariff, on_delete=models.CASCADE, null=True, blank=True, related_name="rules")
    franchisee = models.ForeignKey(Franchisee, on_delete=models.CASCADE, null=True, blank=True,
                                   related_name="rules")
    sale_kind = models.CharField(gettext_lazy("Вид продажи"), max_length=14, choices=SaleKind.choices, blank=True)
    partner_tier = models.CharField(gettext_lazy("Уровень партнёра"), max_length=10, choices=Franchisee.Tier.choices, blank=True)
    lead_source = models.CharField(gettext_lazy("Источник лида (корректировка по лидам OZOK)"), max_length=20, blank=True)
    basis = models.CharField(gettext_lazy("Основание"), max_length=300, blank=True)
    percent = models.DecimalField(gettext_lazy("Доля франчайзи, %"), max_digits=5, decimal_places=2, null=True, blank=True)
    fixed_amount = models.DecimalField(gettext_lazy("Фиксированная сумма"), max_digits=14, decimal_places=2, null=True,
                                       blank=True)
    fixed_per = models.CharField(gettext_lazy("Фикс за"), max_length=6, choices=FixedPer.choices, default=FixedPer.SALE)
    base = models.CharField(gettext_lazy("База"), max_length=5, choices=Base.choices, default=Base.PAID)
    valid_from = models.DateField(gettext_lazy("Действует с"), default=today)
    valid_to = models.DateField(gettext_lazy("Действует по"), null=True, blank=True)
    is_active = models.BooleanField(gettext_lazy("Активно"), default=True)
    note = models.TextField(gettext_lazy("Заметка"), blank=True)

    class Meta:
        verbose_name = gettext_lazy("Правило вознаграждения")
        verbose_name_plural = gettext_lazy("Правила вознаграждения")
        ordering = ("-valid_from", "-id")

    def __str__(self):
        return self.name


class SetupSplitRule(TimeStampedModel):
    """Распределение дохода за установку (C10): total = доля партнёра + доля внедренца; перекрывает CommissionRule."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="setup_split_rules")
    total = models.DecimalField(gettext_lazy("Стоимость установки по правилу"), max_digits=14, decimal_places=2)
    partner_amount = models.DecimalField(gettext_lazy("Доля партнёра"), max_digits=14, decimal_places=2)
    implementer_amount = models.DecimalField(gettext_lazy("Доля внедренца"), max_digits=14, decimal_places=2)
    implementer_condition = models.CharField(max_length=40, default="channels_connected_and_confirmed")
    technical_channels = models.JSONField(gettext_lazy("Технические каналы"), default=list, blank=True)
    valid_from = models.DateField(default=today)
    valid_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = gettext_lazy("Правило распределения установки")
        verbose_name_plural = gettext_lazy("Правила распределения установки")
        ordering = ("-valid_from", "-id")
