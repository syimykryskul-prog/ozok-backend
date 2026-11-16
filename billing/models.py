from django.utils.translation import gettext, gettext_lazy
from django.conf import settings
from django.db import models

from core.models import CancellableModel, TimeStampedModel, today


class Charge(CancellableModel):
    class Kind(models.TextChoices):
        FEE = "fee", gettext_lazy("Взнос")
        INSTALLMENT = "installment", gettext_lazy("Рассрочка")
        WITHHOLD = "withhold", gettext_lazy("Удержание с продаж")
        NBS_LEAD_SHARE = "nbs_lead_share", gettext_lazy("Доля OZOK по лидам OZOK")
        OTHER = "other", gettext_lazy("Прочее")

    franchisee = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, related_name="charges")
    contract = models.ForeignKey("network.Contract", on_delete=models.PROTECT, null=True, blank=True,
                                 related_name="charges")
    client_sale = models.ForeignKey("clients.ClientSale", on_delete=models.CASCADE, null=True, blank=True,
                                    related_name="withhold_charges")
    kind = models.CharField(gettext_lazy("Вид"), max_length=16, choices=Kind.choices, default=Kind.OTHER)
    amount = models.DecimalField(gettext_lazy("Сумма"), max_digits=14, decimal_places=2)
    due_date = models.DateField(gettext_lazy("Срок"), null=True, blank=True)
    description = models.CharField(gettext_lazy("Описание"), max_length=255, blank=True)

    class Meta:
        verbose_name = gettext_lazy("Начисление")
        verbose_name_plural = gettext_lazy("Начисления")
        ordering = ("-due_date", "-id")


class FranchiseePayment(CancellableModel):
    """Платёж франчайзи центру (уменьшает долг франчайзи)."""

    class Method(models.TextChoices):
        CASH = "cash", gettext_lazy("Наличные")
        BANK = "bank", gettext_lazy("Банк")
        OFFSET = "offset", gettext_lazy("Зачёт")

    class Status(models.TextChoices):
        CONFIRMED = "confirmed", gettext_lazy("Подтверждён")
        CANCELLED = "cancelled", gettext_lazy("Отменён")

    franchisee = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, related_name="payments")
    status = models.CharField(gettext_lazy("Статус"), max_length=10, choices=Status.choices, default=Status.CONFIRMED)
    contract = models.ForeignKey("network.Contract", on_delete=models.PROTECT, null=True, blank=True,
                                 related_name="payments")
    client_sale = models.ForeignKey("clients.ClientSale", on_delete=models.CASCADE, null=True, blank=True,
                                    related_name="offset_payments")
    charge = models.ForeignKey(Charge, on_delete=models.PROTECT, null=True, blank=True, related_name="payments",
                               verbose_name=gettext_lazy("Оплачиваемое начисление"))
    income_category = models.CharField(gettext_lazy("Статья кассы (код)"), max_length=40, blank=True)
    amount = models.DecimalField(gettext_lazy("Сумма"), max_digits=14, decimal_places=2)
    date = models.DateField(gettext_lazy("Дата"), default=today)
    method = models.CharField(gettext_lazy("Способ"), max_length=8, choices=Method.choices, default=Method.BANK)
    comment = models.CharField(gettext_lazy("Комментарий"), max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")

    class Meta:
        verbose_name = gettext_lazy("Платёж франчайзи")
        verbose_name_plural = gettext_lazy("Платежи франчайзи")
        ordering = ("-date", "-id")


class CommissionPayout(CancellableModel):
    """Выплата вознаграждения центром франчайзи (уменьшает долг центра)."""

    class Method(models.TextChoices):
        CASH = "cash", gettext_lazy("Наличные")
        BANK = "bank", gettext_lazy("Банк")
        OFFSET = "offset", gettext_lazy("Зачёт")

    franchisee = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, related_name="payouts")
    client_sale = models.ForeignKey("clients.ClientSale", on_delete=models.CASCADE, null=True, blank=True,
                                    related_name="offset_payouts")
    amount = models.DecimalField(gettext_lazy("Сумма"), max_digits=14, decimal_places=2)
    date = models.DateField(gettext_lazy("Дата"), default=today)
    method = models.CharField(gettext_lazy("Способ"), max_length=8, choices=Method.choices, default=Method.BANK)
    comment = models.CharField(gettext_lazy("Комментарий"), max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")

    class Meta:
        verbose_name = gettext_lazy("Выплата вознаграждения")
        verbose_name_plural = gettext_lazy("Выплаты вознаграждения")
        ordering = ("-date", "-id")



class RecruiterReward(TimeStampedModel):
    """Вознаграждение рекрутера за привлечённого партнёра (P3): считается FIFO по реальным деньгам."""

    class Status(models.TextChoices):
        ACCRUING = "accruing", gettext_lazy("Копится")
        ACCRUED = "accrued", gettext_lazy("Начислено")
        VERIFIED = "verified", gettext_lazy("Проверено")
        PAID = "paid", gettext_lazy("Выплачено")
        CANCELLED = "cancelled", gettext_lazy("Отменено")

    partner = models.OneToOneField("network.Franchisee", on_delete=models.PROTECT, related_name="recruiter_reward")
    recruiter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                  related_name="rewards")
    target_amount = models.DecimalField(gettext_lazy("Цель, сом"), max_digits=14, decimal_places=2)
    accrued_amount = models.DecimalField(gettext_lazy("Начислено, сом"), max_digits=14, decimal_places=2, default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACCRUING)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="+")
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_date = models.DateField(null=True, blank=True)
    paid_amount = models.DecimalField(gettext_lazy("Выплачено, сом"), max_digits=14, decimal_places=2, default=0)
    payment_ref = models.CharField(max_length=120, blank=True)
    debt_amount = models.DecimalField(gettext_lazy("Долг рекрутера (корректировка после выплаты)"), max_digits=14,
                                      decimal_places=2, default=0)
    cancel_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        verbose_name = gettext_lazy("Вознаграждение рекрутера")
        verbose_name_plural = gettext_lazy("Вознаграждения рекрутеров")
        ordering = ("-id",)


class RecruiterRewardAllocation(models.Model):
    """Какая часть какого платежа партнёра пошла в вознаграждение (пересобирается при каждом изменении)."""

    reward = models.ForeignKey(RecruiterReward, on_delete=models.CASCADE, related_name="allocations")
    payment = models.ForeignKey(FranchiseePayment, on_delete=models.CASCADE, related_name="reward_allocations")
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ("id",)


class LedgerCategory(TimeStampedModel):
    """Справочник категорий доходов/расходов партнёра (настраивается OZOK)."""

    class Type(models.TextChoices):
        INCOME = "income", gettext_lazy("Доход")
        EXPENSE = "expense", gettext_lazy("Расход")

    type = models.CharField(max_length=8, choices=Type.choices)
    name = models.CharField(max_length=120)
    name_ky = models.CharField(gettext_lazy("Перевод (кыргызча)"), max_length=120, blank=True, default="")
    is_active = models.BooleanField(default=True)
    is_marketing = models.BooleanField(gettext_lazy("Рекламный расход (идёт в Маркетинг/Growth)"), default=False)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = gettext_lazy("Категория учёта")
        verbose_name_plural = gettext_lazy("Категории учёта")
        ordering = ("type", "sort_order", "id")
        constraints = [models.UniqueConstraint(fields=("type", "name"), name="uniq_ledger_category")]

    def __str__(self):
        return self.name


class PartnerLedgerEntry(CancellableModel):
    """Доход/расход партнёра, внесённый вручную. Доходы от продаж подтягиваются автоматически — не вводятся."""

    partner = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, related_name="ledger_entries")
    date = models.DateField(default=today)
    type = models.CharField(max_length=8, choices=LedgerCategory.Type.choices)
    category = models.ForeignKey(LedgerCategory, on_delete=models.PROTECT, related_name="entries")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    description = models.CharField(max_length=300, blank=True)
    product = models.ForeignKey("network.Product", on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")

    class Meta:
        verbose_name = gettext_lazy("Доход/расход партнёра")
        verbose_name_plural = gettext_lazy("Доходы и расходы партнёров")
        ordering = ("-date", "-id")


class Payment(CancellableModel):
    """Оплата абонплаты филиала — каноническая запись (C6, P9). ClientSale(subscription/renewal) выводится из неё 1:1."""

    class Source(models.TextChoices):
        ELQR = "elqr", gettext_lazy("Элкюар")
        PRODUCT = "product", gettext_lazy("Продукт")
        MANUAL = "manual", gettext_lazy("Вручную")
        BANK_IMPORT = "bank_import", gettext_lazy("Импорт выписки")

    class Status(models.TextChoices):
        CONFIRMED = "confirmed", gettext_lazy("Подтверждён")
        CANCELLED = "cancelled", gettext_lazy("Отменён")
        REFUNDED = "refunded", gettext_lazy("Возвращён")
        ERROR = "error", gettext_lazy("Ошибка")

    branch = models.ForeignKey("clients.Branch", on_delete=models.PROTECT, related_name="payments")
    subscription = models.ForeignKey("clients.Subscription", on_delete=models.PROTECT, null=True, blank=True,
                                     related_name="payments")
    client = models.ForeignKey("clients.Client", on_delete=models.PROTECT, related_name="payments")
    product = models.ForeignKey("network.Product", on_delete=models.PROTECT, related_name="payments")
    deal = models.ForeignKey("clients.Deal", on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="payments")
    sale = models.OneToOneField("clients.ClientSale", on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="payment", verbose_name=gettext_lazy("Производная продажа"))
    amount = models.DecimalField(gettext_lazy("Сумма, сом"), max_digits=14, decimal_places=2)
    refunded_amount = models.DecimalField(gettext_lazy("Возвращено"), max_digits=14, decimal_places=2, default=0)
    paid_at = models.DateTimeField(gettext_lazy("Дата и время оплаты (операции)"))
    period_from = models.DateField(gettext_lazy("Период с"), null=True, blank=True)
    period_to = models.DateField(gettext_lazy("Период по"), null=True, blank=True)
    months = models.PositiveSmallIntegerField(gettext_lazy("Месяцев"), default=1)
    source = models.CharField(gettext_lazy("Источник"), max_length=12, choices=Source.choices)
    external_id = models.CharField(gettext_lazy("ID операции"), max_length=120, blank=True)
    elqr_account = models.CharField(gettext_lazy("Лицевой счёт"), max_length=64, blank=True)
    purpose = models.CharField(gettext_lazy("Назначение"), max_length=255, blank=True)
    status = models.CharField(gettext_lazy("Статус"), max_length=10, choices=Status.choices, default=Status.CONFIRMED)
    raw = models.JSONField(gettext_lazy("Исходные данные"), default=dict, blank=True)
    reconciled_at = models.DateTimeField(gettext_lazy("Сверено"), null=True, blank=True)
    reconciled_source = models.CharField(gettext_lazy("Чем сверено"), max_length=12, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")

    class Meta:
        verbose_name = gettext_lazy("Оплата абонплаты")
        verbose_name_plural = gettext_lazy("Оплаты абонплаты")
        ordering = ("-paid_at", "-id")
        constraints = [models.UniqueConstraint(fields=("product", "source", "external_id"),
                                               condition=~models.Q(external_id=""), name="uniq_payment_source_ext")]

    @property
    def paid_date(self):
        from django.utils import timezone
        return timezone.localtime(self.paid_at).date()


class PaymentImport(TimeStampedModel):
    """Импорт банковской выписки (CSV): итог сопоставления сохраняется в журнале."""

    filename = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    summary = models.JSONField(default=dict)
    rows = models.JSONField(default=list)

    class Meta:
        verbose_name = gettext_lazy("Импорт выписки")
        verbose_name_plural = gettext_lazy("Импорты выписок")
        ordering = ("-id",)


class Payout(CancellableModel):
    """Выплата комиссий партнёру за период: draft → verified → paid (verified_by ≠ paid_by)."""

    class Status(models.TextChoices):
        DRAFT = "draft", gettext_lazy("Черновик")
        VERIFIED = "verified", gettext_lazy("Проверена")
        PAID = "paid", gettext_lazy("Выплачена")
        CANCELLED = "cancelled", gettext_lazy("Отменена")

    partner = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, related_name="commission_payouts_v2")
    period = models.DateField(gettext_lazy("Период (1-е число месяца)"))
    amount = models.DecimalField(gettext_lazy("Сумма к выплате"), max_digits=14, decimal_places=2, default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="+")
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_date = models.DateField(null=True, blank=True)
    payment_ref = models.CharField(max_length=120, blank=True)
    comment = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")

    class Meta:
        verbose_name = gettext_lazy("Выплата комиссий")
        verbose_name_plural = gettext_lazy("Выплаты комиссий")
        ordering = ("-period", "-id")


class CommissionAccrual(CancellableModel):
    """Начисление комиссии по оплате (§11.1): снимок двухшаговой ставки. Отрицательные записи — сторно (H5)."""

    class Status(models.TextChoices):
        ON_HOLD = "on_hold", gettext_lazy("Удержано до решения OZOK")
        ACCRUED = "accrued", gettext_lazy("Начислено")
        IN_PAYOUT = "in_payout", gettext_lazy("В выплате")
        PAID = "paid", gettext_lazy("Выплачено")
        CANCELLED = "cancelled", gettext_lazy("Отменено")

    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, null=True, blank=True, related_name="accruals")
    sale = models.ForeignKey("clients.ClientSale", on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="accruals")
    partner = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, related_name="accruals")
    branch = models.ForeignKey("clients.Branch", on_delete=models.PROTECT, related_name="accruals")
    client = models.ForeignKey("clients.Client", on_delete=models.PROTECT, related_name="accruals")
    product = models.ForeignKey("network.Product", on_delete=models.PROTECT, related_name="accruals")
    date = models.DateField(gettext_lazy("Дата оплаты (на которую взята ставка)"))
    rule = models.ForeignKey("network.CommissionRule", on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="accruals")
    adjustment_rule = models.ForeignKey("network.CommissionRule", on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name="adjusted_accruals")
    rate = models.DecimalField(gettext_lazy("Ставка, %"), max_digits=5, decimal_places=2, default=0)
    fixed_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    base_amount = models.DecimalField(gettext_lazy("База"), max_digits=14, decimal_places=2, default=0)
    adjustment_amount = models.DecimalField(gettext_lazy("Корректировка по лиду OZOK"), max_digits=14, decimal_places=2, default=0)
    partner_tier = models.CharField(max_length=10, blank=True)
    lead_source = models.CharField(max_length=20, blank=True)
    amount = models.DecimalField(gettext_lazy("Начислено"), max_digits=14, decimal_places=2)
    withheld_amount = models.DecimalField(gettext_lazy("Удержано в счёт франшизы"), max_digits=14, decimal_places=2, default=0)
    paid_amount = models.DecimalField(gettext_lazy("Погашено ручными выплатами (до пакетов выплат)"), max_digits=14,
                                      decimal_places=2, default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACCRUED)
    reversal_of = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="reversals")
    reason = models.CharField(max_length=500, blank=True)
    payout = models.ForeignKey(Payout, on_delete=models.PROTECT, null=True, blank=True, related_name="accruals")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")

    class Meta:
        verbose_name = gettext_lazy("Начисление комиссии")
        verbose_name_plural = gettext_lazy("Начисления комиссий")
        ordering = ("-date", "-id")

    @property
    def net_amount(self):
        return self.amount - self.withheld_amount

    @property
    def owed(self):
        """Ещё не выплачено: начислено − удержано − погашено ручными выплатами."""
        return self.amount - self.withheld_amount - self.paid_amount


# ---- Касса (P0+ п.6): единый журнал денег OZOK ----------------------------------------------------------------------

class CashAccount(TimeStampedModel):
    class Kind(models.TextChoices):
        CASH = "cash", gettext_lazy("Наличная касса")
        BANK = "bank", gettext_lazy("Банк")
        ELQR = "elqr", gettext_lazy("Элкюар")

    name = models.CharField(gettext_lazy("Название"), max_length=120, unique=True)
    kind = models.CharField(max_length=6, choices=Kind.choices)
    is_active = models.BooleanField(default=True)
    opening_balance = models.DecimalField(gettext_lazy("Начальный остаток"), max_digits=14, decimal_places=2, default=0)
    opening_date = models.DateField(gettext_lazy("Остаток на дату"), default=today)

    class Meta:
        verbose_name = gettext_lazy("Счёт / касса OZOK")
        verbose_name_plural = gettext_lazy("Счета и кассы OZOK")
        ordering = ("id",)

    def __str__(self):
        return self.name


class CashCategory(TimeStampedModel):
    """Справочник статей кассы; системные (автопроводки) вручную не используются."""

    class Direction(models.TextChoices):
        IN = "in", gettext_lazy("Поступление")
        OUT = "out", gettext_lazy("Расход")

    direction = models.CharField(max_length=3, choices=Direction.choices)
    code = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=150)
    name_ky = models.CharField(gettext_lazy("Перевод (кыргызча)"), max_length=150, blank=True, default="")
    is_system = models.BooleanField(gettext_lazy("Автопроводки"), default=False)
    manual_allowed = models.BooleanField(gettext_lazy("Можно вносить вручную"), default=False)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = gettext_lazy("Статья кассы")
        verbose_name_plural = gettext_lazy("Статьи кассы")
        ordering = ("direction", "sort_order", "id")

    def __str__(self):
        return self.name


class CashTransaction(TimeStampedModel):
    """Движение денег OZOK. Не изменяется и не удаляется: отмена = сторнирующая проводка (reversal_of)."""

    class Direction(models.TextChoices):
        IN = "in", gettext_lazy("Поступление")
        OUT = "out", gettext_lazy("Расход")
        TRANSFER = "transfer", gettext_lazy("Перевод")

    date = models.DateField(default=today)
    account = models.ForeignKey(CashAccount, on_delete=models.PROTECT, related_name="transactions")
    transfer_account = models.ForeignKey(CashAccount, on_delete=models.PROTECT, null=True, blank=True,
                                         related_name="incoming_transfers", verbose_name=gettext_lazy("Счёт назначения"))
    direction = models.CharField(max_length=8, choices=Direction.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    category = models.ForeignKey(CashCategory, on_delete=models.PROTECT, null=True, blank=True,
                                 related_name="transactions")
    partner = models.ForeignKey("network.Franchisee", on_delete=models.PROTECT, null=True, blank=True,
                                related_name="+")
    client = models.ForeignKey("clients.Client", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    source_type = models.CharField(max_length=24, blank=True)
    source_id = models.PositiveIntegerField(null=True, blank=True)
    reversal_of = models.OneToOneField("self", on_delete=models.PROTECT, null=True, blank=True,
                                       related_name="reversal")
    comment = models.CharField(max_length=500, blank=True)
    attachment = models.CharField(gettext_lazy("Вложение (чек/счёт)"), max_length=500, blank=True)
    is_auto = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")

    class Meta:
        verbose_name = gettext_lazy("Проводка кассы")
        verbose_name_plural = gettext_lazy("Проводки кассы")
        ordering = ("-date", "-id")
        indexes = [models.Index(fields=("source_type", "source_id"))]

    @property
    def is_reversed(self):
        return hasattr(self, "reversal")

    def save(self, *args, **kwargs):
        if self.pk:
            raise RuntimeError(gettext("Проводки кассы не изменяются: отмена — сторнирующей проводкой."))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError(gettext("Проводки кассы не удаляются."))


class NbsExpense(TimeStampedModel):
    """Расход OZOK (ручная проводка out с категорией и вложением)."""

    transaction = models.OneToOneField(CashTransaction, on_delete=models.PROTECT, related_name="expense")
    description = models.CharField(max_length=300, blank=True)
    supplier = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = gettext_lazy("Расход OZOK")
        verbose_name_plural = gettext_lazy("Расходы OZOK")
        ordering = ("-id",)
