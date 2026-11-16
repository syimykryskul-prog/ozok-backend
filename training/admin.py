from django.contrib import admin

from .models import Course, Enrollment

admin.site.register(Course)


@admin.register(Enrollment)
class EnrollmentAdmin(admin.ModelAdmin):
    list_display = ("course", "user", "franchisee", "status", "score", "expires_at")
    list_filter = ("status",)
