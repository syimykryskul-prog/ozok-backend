from django.contrib import admin

from .models import CommissionRule, Contract, Equipment, Franchisee, Package, Product, Tariff, TariffPrice


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "sphere", "price_cash", "price_per_sale", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")


@admin.register(Package)
class PackageAdmin(admin.ModelAdmin):
    list_display = ("name", "price_cash", "price_prepay", "price_per_sale", "is_active")
    filter_horizontal = ("products",)


@admin.register(Franchisee)
class FranchiseeAdmin(admin.ModelAdmin):
    list_display = ("name", "city", "status", "curator", "joined_at")
    list_filter = ("status", "city")
    search_fields = ("name", "city", "owner_name", "phone")


@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = ("number", "franchisee", "product", "package", "payment_scheme", "fee_total", "status")
    list_filter = ("status", "payment_scheme")
    search_fields = ("number", "franchisee__name")


@admin.register(Tariff)
class TariffAdmin(admin.ModelAdmin):
    list_display = ("product", "code", "name", "is_active")


admin.site.register(TariffPrice)
admin.site.register(Equipment)


@admin.register(CommissionRule)
class CommissionRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "franchisee", "product", "tariff", "sale_kind", "percent", "fixed_amount", "valid_from", "valid_to", "is_active")




from .models import SetupSplitRule  # noqa: E402


@admin.register(SetupSplitRule)
class SetupSplitRuleAdmin(admin.ModelAdmin):
    list_display = ("product", "total", "partner_amount", "implementer_amount", "valid_from", "is_active")
