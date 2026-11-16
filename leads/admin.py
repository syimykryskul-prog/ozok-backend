from django.contrib import admin

from .models import Lead


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "city", "interest", "status", "franchisee", "created_at")
    list_filter = ("status", "interest", "source")
    search_fields = ("name", "phone", "city")
