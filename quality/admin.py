from django.contrib import admin

from .models import Audit, Ticket


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("subject", "client", "franchisee", "priority", "status", "opened_at")
    list_filter = ("status", "priority")


@admin.register(Audit)
class AuditAdmin(admin.ModelAdmin):
    list_display = ("franchisee", "date", "score", "inspector")
