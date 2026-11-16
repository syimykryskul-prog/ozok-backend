from django.contrib import admin

from .models import Client, ClientSale, ClientProductAccount, Implementation


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("business_name", "franchisee", "business_type", "city", "is_active")
    list_filter = ("business_type", "is_active")
    search_fields = ("business_name", "contact_name", "phone")


@admin.register(Implementation)
class ImplementationAdmin(admin.ModelAdmin):
    list_display = ("client", "product", "stage", "planned_launch", "franchisee")
    list_filter = ("stage",)


@admin.register(ClientProductAccount)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("client", "product", "external_id", "monthly_fee", "paid_until", "status")
    list_filter = ("status",)


@admin.register(ClientSale)
class ClientSaleAdmin(admin.ModelAdmin):
    list_display = ("client", "product", "kind", "date", "amount", "commission_percent", "commission_amount")
    list_filter = ("kind", "product")




from .models import (Branch, BranchAssignmentHistory, ChecklistTemplate, ClientContact, ContractTemplate, Deal,  # noqa: E402
                     ImplementerAccrual, InstallationComment)


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ("client", "name", "product", "is_main", "status", "implementer_partner", "responsible_partner",
                    "elqr_account")
    list_filter = ("status", "is_main")
    search_fields = ("name", "client__business_name", "elqr_account")


@admin.register(BranchAssignmentHistory)
class BranchAssignmentAdmin(admin.ModelAdmin):
    list_display = ("branch", "field", "from_partner", "to_partner", "reason_code", "effective_from")
    list_filter = ("reason_code", "field")


@admin.register(ClientContact)
class ClientContactAdmin(admin.ModelAdmin):
    list_display = ("full_name", "client", "branch", "phone", "is_primary")


@admin.register(Deal)
class DealAdmin(admin.ModelAdmin):
    list_display = ("id", "client", "partner", "product", "status", "payment_status", "total")
    list_filter = ("status", "payment_status")


@admin.register(InstallationComment)
class InstallationCommentAdmin(admin.ModelAdmin):
    list_display = ("installation", "author", "created_at")


@admin.register(ChecklistTemplate)
class ChecklistTemplateAdmin(admin.ModelAdmin):
    list_display = ("product", "name", "is_active")


@admin.register(ImplementerAccrual)
class ImplementerAccrualAdmin(admin.ModelAdmin):
    list_display = ("installation", "user", "amount", "status")
    list_filter = ("status",)


@admin.register(ContractTemplate)
class ContractTemplateAdmin(admin.ModelAdmin):
    list_display = ("kind", "name", "is_active")
