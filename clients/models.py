from django.utils.translation import gettext_lazy
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import CancellableModel, TimeStampedModel, today


class Client(TimeStampedModel):
    class BusinessType(models.TextChoices):
        SHOP = "shop", gettext_lazy("Магазин")
        WHOLESALE = "wholesale", gettext_lazy("Опт")
        SERVICES = "services", gettext_lazy("Услуги")
        CAFE = "cafe", gettext_lazy("Кафе")
        OTHER = "other", gettext_lazy("Другое")

    class Origin(models.TextChoices):
        PARTNER = "partner", gettext_lazy("Партнёр")
        OZOK_LEGACY = "ozok_legacy", gettext_lazy("Клиент OZOK (существующий)")
        OZOK_DIRECT = "ozok_direct", gettext_lazy("Прямая продажа OZOK")

    OZOK_ORIGINS = ("ozok_legacy", "ozok_direct")

    # null — клиент OZOK без партнёра (абонплата 100% OZOK, комиссия не начисляется)
    franchisee = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="clients")
    origin = models.CharField(gettext_lazy("Источник"), max_length=12, choices=Origin.choices, default=Origin.PARTNER,
                              db_index=True)
    owner_partner = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, null=True, blank=True,
                                      related_name="owned_clients", verbose_name=gettext_lazy("Первичный партнёр (привёл)"))
    business_name = models.CharField(gettext_lazy("Бизнес"), max_length=200)
    legal_name = models.CharField(gettext_lazy("Юрлицо"), max_length=255, blank=True)
    inn = models.CharField(gettext_lazy("ИНН"), max_length=32, blank=True)
    region = models.CharField(gettext_lazy("Регион"), max_length=100, blank=True)
    business_type = models.CharField(gettext_lazy("Тип бизнеса"), max_length=12, choices=BusinessType.choices,
                                     default=BusinessType.OTHER)
    contact_name = models.CharField(gettext_lazy("Контактное лицо"), max_length=150, blank=True)
    phone = models.CharField(gettext_lazy("Телефон"), max_length=32, blank=True)
    city = models.CharField(gettext_lazy("Город"), max_length=100, blank=True)
    address = models.CharField(gettext_lazy("Адрес"), max_length=255, blank=True)
    lead = models.ForeignKey("leads.Lead", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    note = models.TextField(gettext_lazy("Заметка"), blank=True)
    is_active = models.BooleanField(gettext_lazy("Активен"), default=True)

    class Meta:
        verbose_name = gettext_lazy("Клиент")
        verbose_name_plural = gettext_lazy("Клиенты")
        ordering = ("-created_at", "-id")

    def save(self, *args, **kwargs):
        if self.owner_partner_id is None:
            self.owner_partner_id = self.franchisee_id
        super().save(*args, **kwargs)

    def __str__(self):
        return self.business_name


class Branch(TimeStampedModel):
    """Филиал клиента (P5): единица подписки, комиссии и закрепления за партнёром."""

    class Status(models.TextChoices):
        PENDING = "pending", gettext_lazy("Ожидает")
        ACTIVE = "active", gettext_lazy("Работает")
        SUSPENDED = "suspended", gettext_lazy("Приостановлен")
        CLOSED = "closed", gettext_lazy("Закрыт")

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="branches")
    name = models.CharField("Название", max_length=200, default="Основной")
    address = models.CharField(gettext_lazy("Адрес"), max_length=255, blank=True)
    city = models.CharField(gettext_lazy("Город"), max_length=100, blank=True)
    region = models.CharField(gettext_lazy("Регион"), max_length=100, blank=True)
    product = models.ForeignKey("network.Product", on_delete=models.PROTECT, null=True, blank=True,
                                related_name="branches")
    tariff = models.ForeignKey("network.Tariff", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="branches")
    launch_date = models.DateField(gettext_lazy("Дата запуска"), null=True, blank=True)
    elqr_account = models.CharField(gettext_lazy("Лицевой счёт Элкюар"), max_length=64, null=True, blank=True, unique=True)
    seller_partner = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, null=True, blank=True,
                                       related_name="sold_branches")
    implementer_partner = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, null=True, blank=True,
                                            related_name="implemented_branches")
    responsible_partner = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, null=True, blank=True,
                                            related_name="served_branches")
    support_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="supported_branches")
    is_main = models.BooleanField(gettext_lazy("Основной филиал"), default=False)
    # клиенты OZOK (legacy/direct): комиссия сопровождающему партнёру не начисляется, пока OZOK явно не включит
    # (меняет только super_admin, с аудитом и датой начала)
    commission_enabled = models.BooleanField(gettext_lazy("Начислять комиссию партнёру"), default=False)
    commission_enabled_from = models.DateField(gettext_lazy("Комиссия начисляется с"), null=True, blank=True)
    status = models.CharField(gettext_lazy("Статус"), max_length=10, choices=Status.choices, default=Status.PENDING)
    # правило второго филиала (§7.2)
    priority_partner = models.ForeignKey("network.Franchisee", on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name="priority_branches")
    priority_deadline = models.DateTimeField(gettext_lazy("Приоритет партнёра до"), null=True, blank=True)
    priority_accepted_at = models.DateTimeField(null=True, blank=True)
    # лид OZOK (H4): корректировка действует только на филиалы, созданные из этого лида
    lead = models.ForeignKey("leads.Lead", on_delete=models.SET_NULL, null=True, blank=True, related_name="branches")
    lead_source = models.CharField(gettext_lazy("Источник лида"), max_length=20, blank=True)

    class Meta:
        verbose_name = gettext_lazy("Филиал")
        verbose_name_plural = gettext_lazy("Филиалы")
        ordering = ("client_id", "-is_main", "id")

    def __str__(self):
        return f"{self.client.business_name} / {self.name}"

    @property
    def priority_expired(self):
        return bool(self.priority_partner_id and self.priority_accepted_at is None
                    and self.implementer_partner_id is None and self.priority_deadline is not None
                    and self.priority_deadline < timezone.now())

    @property
    def effective_lead(self):
        """Лид для корректировки по лидам OZOK: свой лид филиала; основной филиал наследует лид клиента."""
        if self.lead_id:
            return self.lead
        return self.client.lead if self.is_main and self.client.lead_id else None


class BranchAssignmentHistory(TimeStampedModel):
    """Закрепление филиала за партнёром с датой вступления (C8): получатель комиссии определяется на дату оплаты."""

    class Field(models.TextChoices):
        SERVICE = "service", gettext_lazy("Внедрение и сопровождение")
        IMPLEMENTER = "implementer", gettext_lazy("Внедренец")
        RESPONSIBLE = "responsible", gettext_lazy("Ответственный")
        SELLER = "seller", gettext_lazy("Продавец")

    class Reason(models.TextChoices):
        NO_RESPONSE = "no_response", gettext_lazy("Нет ответа партнёра")
        NO_CRM = "no_crm_activity", gettext_lazy("Нет активности в CRM")
        INSTALL_OVERDUE = "install_overdue", gettext_lazy("Просрочена установка")
        COMPLAINT = "confirmed_complaint", gettext_lazy("Подтверждённая жалоба")
        ACCEPTED = "accepted", gettext_lazy("Принято партнёром")
        PARTNER_CLOSED = "partner_closed", gettext_lazy("Уход партнёра")
        OTHER = "other", gettext_lazy("Другое")

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="assignment_history")
    field = models.CharField(max_length=12, choices=Field.choices, default=Field.SERVICE)
    from_partner = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, null=True, blank=True,
                                     related_name="+")
    to_partner = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="+")
    from_priority = models.BooleanField(gettext_lazy("from_partner — приоритетный партнёр (филиал ещё никем не обслуживался)"),
                                        default=False)
    reason_code = models.CharField(max_length=20, choices=Reason.choices, default=Reason.OTHER)
    reason = models.CharField(max_length=500, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    effective_from = models.DateField(gettext_lazy("Действует с"), default=today)

    class Meta:
        verbose_name = gettext_lazy("История закрепления филиала")
        verbose_name_plural = gettext_lazy("История закрепления филиалов")
        ordering = ("-effective_from", "-id")


class ClientContact(TimeStampedModel):
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="contacts")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, null=True, blank=True, related_name="contacts")
    full_name = models.CharField(gettext_lazy("ФИО"), max_length=200)
    position = models.CharField(gettext_lazy("Должность"), max_length=120, blank=True)
    phone = models.CharField(gettext_lazy("Телефон"), max_length=32, blank=True)
    email = models.EmailField("Email", blank=True)
    is_primary = models.BooleanField(gettext_lazy("Основной контакт"), default=False)

    class Meta:
        verbose_name = gettext_lazy("Контакт клиента")
        verbose_name_plural = gettext_lazy("Контакты клиентов")
        ordering = ("-is_primary", "id")


class Implementation(TimeStampedModel):
    class Stage(models.TextChoices):
        CONTRACT = "contract", gettext_lazy("Договор")
        SETUP = "setup", gettext_lazy("Настройка")
        TRAINING = "training", gettext_lazy("Обучение")
        LAUNCHED = "launched", gettext_lazy("Запущено")
        CANCELLED = "cancelled", gettext_lazy("Отменено")

    franchisee = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="implementations")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="implementations")
    product = models.ForeignKey("network.Product", on_delete=models.PROTECT, related_name="implementations")
    stage = models.CharField(gettext_lazy("Этап"), max_length=12, choices=Stage.choices, default=Stage.CONTRACT)
    started_at = models.DateField(gettext_lazy("Начато"), default=today)
    planned_launch = models.DateField(gettext_lazy("План запуска"), null=True, blank=True)
    launched_at = models.DateField(gettext_lazy("Запущено"), null=True, blank=True)
    responsible = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="implementations")
    note = models.TextField(gettext_lazy("Заметка"), blank=True)
    # P7: установка (Installation)
    deal = models.ForeignKey("clients.Deal", on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="installations")
    branch = models.ForeignKey("clients.Branch", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="installations")
    implementer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="installations", verbose_name=gettext_lazy("Внедренец (разработчик)"))
    deadline = models.DateField(gettext_lazy("Срок установки"), null=True, blank=True)
    status = models.CharField(gettext_lazy("Статус установки"), max_length=14, default="assigned",
                              choices=[("assigned", gettext_lazy("Назначена")), ("in_progress", gettext_lazy("В работе")),
                                       ("training", gettext_lazy("Обучение")), ("awaiting_act", gettext_lazy("Ожидает акта")),
                                       ("done", gettext_lazy("Завершена")), ("cancelled", gettext_lazy("Отменена"))])
    checklist = models.JSONField(gettext_lazy("Чек-лист"), default=list, blank=True)
    training_result = models.CharField(gettext_lazy("Результат обучения"), max_length=12, blank=True, default="",
                                       choices=[("passed", gettext_lazy("Пройдено")), ("needs_repeat", gettext_lazy("Нужно повторить"))])
    client_rating = models.PositiveSmallIntegerField(gettext_lazy("Оценка клиента (1–5)"), null=True, blank=True)
    act_file = models.CharField(gettext_lazy("Акт (файл/ссылка)"), max_length=500, blank=True)
    act_confirmed_at = models.DateTimeField(gettext_lazy("Акт подтверждён"), null=True, blank=True)
    client_confirmed_by = models.CharField(gettext_lazy("Подтвердил (ФИО клиента)"), max_length=200, blank=True)
    technical_channels = models.JSONField(gettext_lazy("Технические каналы"), default=list, blank=True)

    class Meta:
        verbose_name = gettext_lazy("Внедрение")
        verbose_name_plural = gettext_lazy("Внедрения")
        ordering = ("-started_at", "-id")

    @property
    def overdue(self):
        due = self.deadline or self.planned_launch
        return (due is not None and due < today() and self.stage not in ("launched", "cancelled")
                and self.status not in ("done", "cancelled"))


class Subscription(TimeStampedModel):
    """Подписка филиала на продукт (§10.3). Бывший «аккаунт клиента в продукте» (ClientProductAccount)."""

    class Status(models.TextChoices):
        AWAITING_PAYMENT = "awaiting_payment", gettext_lazy("Ожидает оплаты")
        ACTIVE = "active", gettext_lazy("Активна")
        GRACE = "grace", gettext_lazy("Льготный период")
        OVERDUE = "overdue", gettext_lazy("Просрочена")
        RESTRICTED = "restricted", gettext_lazy("Ограничена")
        CLOSED = "closed", gettext_lazy("Закрыта")

    LIVE_STATUSES = ("active", "grace", "overdue", "restricted")

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="accounts")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True, related_name="subscriptions")
    product = models.ForeignKey("network.Product", on_delete=models.PROTECT, related_name="accounts")
    external_id = models.CharField(gettext_lazy("ID клиента в продукте"), max_length=100, blank=True)
    status = models.CharField(gettext_lazy("Статус"), max_length=16, choices=Status.choices, default=Status.ACTIVE)
    paid_until = models.DateField(gettext_lazy("Оплачено до"), null=True, blank=True)
    monthly_fee = models.DecimalField(gettext_lazy("Абонплата в месяц"), max_digits=14, decimal_places=2, null=True, blank=True)
    tariff = models.ForeignKey("network.Tariff", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="accounts")
    discount_percent = models.DecimalField(gettext_lazy("Скидка клиента, %"), max_digits=5, decimal_places=2, default=0)
    connected_at = models.DateField(gettext_lazy("Подключён"), default=today)
    started_at = models.DateField(gettext_lazy("Старт подписки"), null=True, blank=True)
    closed_at = models.DateField(gettext_lazy("Закрыта"), null=True, blank=True)
    deal = models.ForeignKey("clients.Deal", on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="subscriptions")

    class Meta:
        verbose_name = gettext_lazy("Подписка")
        verbose_name_plural = gettext_lazy("Подписки")
        ordering = ("-id",)
        constraints = [models.UniqueConstraint(fields=("product", "external_id"), condition=~Q(external_id=""),
                                               name="uniq_account_product_ext")]

    def save(self, *args, **kwargs):
        if self.branch_id is None and self.client_id:
            from .branches import ensure_main_branch
            self.branch = ensure_main_branch(self.client)
        super().save(*args, **kwargs)

    @property
    def franchisee_id(self):
        return self.client.franchisee_id

    @property
    def monthly_price(self):
        return self.monthly_fee


ClientProductAccount = Subscription  # совместимость со старым кодом


class ClientSale(CancellableModel):
    class Kind(models.TextChoices):
        SETUP = "setup", gettext_lazy("Установка и обучение")
        TRAINING = "training", gettext_lazy("Обучение")
        EQUIPMENT = "equipment", gettext_lazy("Оборудование")
        SUBSCRIPTION = "subscription", gettext_lazy("Абонплата")
        RENEWAL = "renewal", gettext_lazy("Продление")
        OTHER = "other", gettext_lazy("Прочее")

    class Source(models.TextChoices):
        INTEGRATION = "integration", gettext_lazy("Из продукта")
        MANUAL = "manual", gettext_lazy("Вручную")

    COMMISSION_KINDS = ("subscription", "renewal")

    franchisee = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="sales")
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="sales")
    product = models.ForeignKey("network.Product", on_delete=models.PROTECT, related_name="sales")
    kind = models.CharField(gettext_lazy("Вид"), max_length=14, choices=Kind.choices, default=Kind.SETUP)
    date = models.DateField(gettext_lazy("Дата"), default=today)
    amount = models.DecimalField(gettext_lazy("Оплачено (сумма продажи)"), max_digits=14, decimal_places=2)
    equipment = models.ForeignKey("network.Equipment", on_delete=models.PROTECT, null=True, blank=True,
                                  related_name="sales")
    quantity = models.DecimalField(gettext_lazy("Количество"), max_digits=10, decimal_places=3, default=1)
    tariff = models.ForeignKey("network.Tariff", on_delete=models.PROTECT, null=True, blank=True,
                               related_name="sales")
    list_amount = models.DecimalField(gettext_lazy("По прайсу"), max_digits=14, decimal_places=2, default=0)
    discount_amount = models.DecimalField(gettext_lazy("Скидка"), max_digits=14, decimal_places=2, default=0)
    # снимок расчёта дохода франчайзи (5Д): правило, ставка, фикс, база
    commission_rule = models.ForeignKey("network.CommissionRule", on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name="sales")
    commission_percent = models.DecimalField(gettext_lazy("Ставка вознаграждения, %"), max_digits=5, decimal_places=2, default=0)
    commission_fixed = models.DecimalField(gettext_lazy("Фиксированная часть"), max_digits=14, decimal_places=2, default=0)
    commission_base_amount = models.DecimalField(gettext_lazy("База расчёта"), max_digits=14, decimal_places=2, default=0)
    commission_amount = models.DecimalField(gettext_lazy("Вознаграждение с абонплаты"), max_digits=14, decimal_places=2,
                                            default=0)
    franchisee_income = models.DecimalField(gettext_lazy("Доход франчайзи"), max_digits=14, decimal_places=2, default=0)
    income_overridden = models.BooleanField(gettext_lazy("Доход задан вручную"), default=False)
    withheld_amount = models.DecimalField(gettext_lazy("Удержано в счёт франшизы"), max_digits=14, decimal_places=2, default=0)
    # корректировка по лидам OZOK (C11): второй шаг расчёта хранится отдельно от базовой ставки
    lead = models.ForeignKey("leads.Lead", on_delete=models.SET_NULL, null=True, blank=True, related_name="sales")
    lead_source = models.CharField(gettext_lazy("Источник лида"), max_length=20, blank=True)
    partner_tier = models.CharField(gettext_lazy("Уровень партнёра на дату"), max_length=10, blank=True)
    adjustment_rule = models.ForeignKey("network.CommissionRule", on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name="adjusted_sales")
    adjustment_amount = models.DecimalField(gettext_lazy("Корректировка по лиду OZOK"), max_digits=14, decimal_places=2, default=0)
    nbs_share_amount = models.DecimalField(gettext_lazy("Доля OZOK по лиду OZOK"), max_digits=14, decimal_places=2, default=0)
    deal = models.ForeignKey("clients.Deal", on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="sales")
    branch = models.ForeignKey("clients.Branch", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="sales")
    # оборудование (P7)
    serial_numbers = models.JSONField(gettext_lazy("Серийные номера"), default=list, blank=True)
    sold_by = models.ForeignKey("network.Franchisee", on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="+", verbose_name=gettext_lazy("Продал (партнёр)"))
    installed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+", verbose_name=gettext_lazy("Установил"))
    delivery_status = models.CharField(gettext_lazy("Доставка"), max_length=10, blank=True, default="",
                                       choices=[("ordered", gettext_lazy("Заказано")), ("in_stock", gettext_lazy("На складе")),
                                                ("delivered", gettext_lazy("Доставлено")), ("installed", gettext_lazy("Установлено"))])
    warranty_until = models.DateField(gettext_lazy("Гарантия до"), null=True, blank=True)
    paid_until = models.DateField(gettext_lazy("Оплачено до"), null=True, blank=True)
    source = models.CharField(gettext_lazy("Источник"), max_length=12, choices=Source.choices, default=Source.MANUAL)
    external_payment_id = models.CharField(gettext_lazy("ID платежа в продукте"), max_length=120, null=True, blank=True)
    months = models.PositiveSmallIntegerField(gettext_lazy("Месяцев"), null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+", verbose_name=gettext_lazy("Кем внесено"))
    refund_of = models.ForeignKey("self", on_delete=models.RESTRICT, null=True, blank=True, related_name="refunds",
                                  verbose_name=gettext_lazy("Возврат по продаже"))
    comment = models.CharField(gettext_lazy("Комментарий"), max_length=255, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("product", "external_payment_id"),
                                               condition=Q(external_payment_id__isnull=False),
                                               name="uniq_sale_product_ext_payment")]
        verbose_name = gettext_lazy("Продажа клиенту")
        verbose_name_plural = gettext_lazy("Продажи клиентам")
        ordering = ("-date", "-id")





class Deal(TimeStampedModel):
    """Продажа одной формой (P0+): установка/обучение, оборудование, абонплата — любые сочетания."""

    class Status(models.TextChoices):
        DRAFT = "draft", gettext_lazy("Черновик")
        PENDING_APPROVAL = "pending_approval", gettext_lazy("Ожидает согласования")
        CONFIRMED = "confirmed", gettext_lazy("Подтверждена")
        CANCELLED = "cancelled", gettext_lazy("Отменена")

    client = models.ForeignKey(Client, on_delete=models.PROTECT, null=True, blank=True, related_name="deals")
    client_name = models.CharField(gettext_lazy("Клиент (для продаж OZOK без карточки)"), max_length=200, blank=True)
    partner = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, null=True, blank=True,
                                related_name="deals", verbose_name=gettext_lazy("Партнёр (пусто — продажа OZOK)"))
    lead = models.ForeignKey("leads.Lead", on_delete=models.SET_NULL, null=True, blank=True, related_name="deals")
    product = models.ForeignKey("network.Product", on_delete=models.PROTECT, related_name="deals")
    date = models.DateField(default=today)
    status = models.CharField(max_length=18, choices=Status.choices, default=Status.DRAFT)
    comment = models.CharField(max_length=500, blank=True)
    deal_discount_percent = models.DecimalField(gettext_lazy("Общая скидка, %"), max_digits=5, decimal_places=2, default=0)
    deal_discount_amount = models.DecimalField(gettext_lazy("Общая скидка, сумма"), max_digits=14, decimal_places=2, default=0)
    discount_reason = models.CharField(max_length=300, blank=True)
    list_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    partner_income_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    nbs_income_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    approval_note = models.CharField(max_length=500, blank=True)
    branch = models.ForeignKey("clients.Branch", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="deals")
    price_list_snapshot = models.JSONField(gettext_lazy("Прайс на дату продажи"), default=dict, blank=True)
    contract_number = models.CharField(gettext_lazy("Номер договора"), max_length=60, blank=True)
    signed_at = models.DateField(gettext_lazy("Дата подписания"), null=True, blank=True)
    payment_status = models.CharField(gettext_lazy("Оплата"), max_length=8, default="unpaid",
                                      choices=[("unpaid", gettext_lazy("Не оплачено")), ("partial", gettext_lazy("Частично")),
                                               ("paid", gettext_lazy("Оплачено"))])
    marked_paid_at = models.DateTimeField(gettext_lazy("Отмечена оплаченной вручную"), null=True, blank=True)
    marked_paid_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name="+")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")
    cancel_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        verbose_name = gettext_lazy("Продажа (форма)")
        verbose_name_plural = gettext_lazy("Продажи (формы)")
        ordering = ("-date", "-id")

    @property
    def franchisee_id_scope(self):
        return self.partner_id


class DealLine(TimeStampedModel):
    class Kind(models.TextChoices):
        SETUP = "setup", gettext_lazy("Установка и обучение")
        TRAINING = "training", gettext_lazy("Обучение")
        EQUIPMENT = "equipment", gettext_lazy("Оборудование")
        SUBSCRIPTION = "subscription", gettext_lazy("Абонплата")

    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, related_name="lines")
    kind = models.CharField(max_length=14, choices=Kind.choices)
    equipment = models.ForeignKey("network.Equipment", on_delete=models.PROTECT, null=True, blank=True,
                                  related_name="deal_lines")
    tariff = models.ForeignKey("network.Tariff", on_delete=models.PROTECT, null=True, blank=True,
                               related_name="deal_lines")
    quantity = models.DecimalField(max_digits=10, decimal_places=3, default=1)
    months = models.PositiveSmallIntegerField(null=True, blank=True)
    serial_numbers = models.JSONField(default=list, blank=True)
    list_price = models.DecimalField(gettext_lazy("Цена по прайсу (за единицу/месяц)"), max_digits=14, decimal_places=2,
                                     default=0)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount_reason = models.CharField(max_length=300, blank=True)
    list_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    line_discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    deal_discount_share = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    partner_income = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    nbs_income = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    withheld = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    violation = models.CharField(max_length=300, blank=True)
    subscription_status = models.CharField(max_length=20, blank=True)
    sale = models.ForeignKey(ClientSale, on_delete=models.SET_NULL, null=True, blank=True, related_name="deal_lines")

    class Meta:
        ordering = ("id",)


Installation = Implementation  # P7: внедрение расширено до установки


class InstallationComment(TimeStampedModel):
    installation = models.ForeignKey(Implementation, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="+")
    text = models.TextField(gettext_lazy("Комментарий"))

    class Meta:
        verbose_name = gettext_lazy("Комментарий к установке")
        verbose_name_plural = gettext_lazy("Комментарии к установкам")
        ordering = ("id",)


class ChecklistTemplate(TimeStampedModel):
    """Шаблон чек-листа установки по продукту (редактирует super_admin)."""

    product = models.ForeignKey("network.Product", on_delete=models.CASCADE, related_name="checklist_templates")
    name = models.CharField(max_length=150, default="Чек-лист установки")
    items = models.JSONField(gettext_lazy("Пункты"), default=list)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = gettext_lazy("Шаблон чек-листа")
        verbose_name_plural = gettext_lazy("Шаблоны чек-листов")
        ordering = ("product_id", "id")


class ImplementerAccrual(TimeStampedModel):
    """Доход внедренца за установку (P7, C10): создаётся при подключении всех каналов и подтверждении акта."""

    class Status(models.TextChoices):
        PENDING = "pending", gettext_lazy("Ожидает")
        ACCRUED = "accrued", gettext_lazy("Начислено")
        VERIFIED = "verified", gettext_lazy("Проверено")
        PAID = "paid", gettext_lazy("Выплачено")

    installation = models.OneToOneField(Implementation, on_delete=models.PROTECT, related_name="implementer_accrual")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="implementer_accruals")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    split_rule = models.ForeignKey("network.SetupSplitRule", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="accruals")
    sold_amount = models.DecimalField(gettext_lazy("Продано за"), max_digits=14, decimal_places=2, default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACCRUED)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="+")
    paid_at = models.DateTimeField(null=True, blank=True)
    payment_ref = models.CharField(max_length=120, blank=True)

    class Meta:
        verbose_name = gettext_lazy("Начисление внедренцу")
        verbose_name_plural = gettext_lazy("Начисления внедренцам")
        ordering = ("-id",)


class ContractTemplate(TimeStampedModel):
    """Шаблон договора (менеджер / франчайзи / клиент)."""

    class Kind(models.TextChoices):
        MANAGER = "manager", gettext_lazy("Договор менеджера")
        FRANCHISEE = "franchisee", gettext_lazy("Договор франчайзи")
        CLIENT = "client", gettext_lazy("Договор с клиентом")

    kind = models.CharField(max_length=12, choices=Kind.choices)
    name = models.CharField(max_length=200)
    file = models.FileField(upload_to="contract_templates/", blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = gettext_lazy("Шаблон договора")
        verbose_name_plural = gettext_lazy("Шаблоны договоров")
        ordering = ("kind", "name")
