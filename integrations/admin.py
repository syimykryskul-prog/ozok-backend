from django.contrib import admin

from .models import IntegrationEvent


@admin.register(IntegrationEvent)
class IntegrationEventAdmin(admin.ModelAdmin):
    list_display = ("id", "provider", "product", "event_type", "external_id", "status", "franchisee", "received_at")
    list_filter = ("status", "event_type", "product")

