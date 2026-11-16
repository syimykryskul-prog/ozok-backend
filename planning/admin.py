from django.contrib import admin

from .models import SalesPlan


@admin.register(SalesPlan)
class SalesPlanAdmin(admin.ModelAdmin):
    list_display = ("franchisee", "month", "target_clients", "target_revenue")
