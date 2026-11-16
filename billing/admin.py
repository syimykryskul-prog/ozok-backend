from django.contrib import admin

from .models import Charge, CommissionPayout, FranchiseePayment


@admin.register(Charge)
class ChargeAdmin(admin.ModelAdmin):
    list_display = ("franchisee", "kind", "amount", "due_date", "contract")
    list_filter = ("kind",)


@admin.register(FranchiseePayment)
class FranchiseePaymentAdmin(admin.ModelAdmin):
    list_display = ("franchisee", "amount", "date", "method")
    list_filter = ("method",)


@admin.register(CommissionPayout)
class CommissionPayoutAdmin(admin.ModelAdmin):
    list_display = ("franchisee", "amount", "date", "method")
    list_filter = ("method",)



from .models import CommissionAccrual, Payment, PaymentImport, Payout  # noqa: E402


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "branch", "amount", "paid_at", "source", "external_id", "status")
    list_filter = ("source", "status")
    search_fields = ("external_id", "client__business_name")


@admin.register(CommissionAccrual)
class CommissionAccrualAdmin(admin.ModelAdmin):
    list_display = ("id", "partner", "branch", "date", "rate", "amount", "withheld_amount", "status")
    list_filter = ("status",)


@admin.register(Payout)
class PayoutAdmin(admin.ModelAdmin):
    list_display = ("id", "partner", "period", "amount", "status")
    list_filter = ("status",)


@admin.register(PaymentImport)
class PaymentImportAdmin(admin.ModelAdmin):
    list_display = ("id", "filename", "created_by", "created_at")


from .models import CashAccount, CashCategory, CashTransaction, NbsExpense  # noqa: E402

admin.site.register(CashAccount)
admin.site.register(CashCategory)
admin.site.register(NbsExpense)


@admin.register(CashTransaction)
class CashTransactionAdmin(admin.ModelAdmin):
    list_display = ("id", "date", "account", "direction", "amount", "category", "is_auto", "reversal_of")
    list_filter = ("direction", "is_auto", "account")
