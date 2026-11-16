from django.contrib import admin

from .models import Task, TaskComment


class CommentInline(admin.TabularInline):
    model = TaskComment
    extra = 0


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "franchisee", "priority", "status", "due_date")
    list_filter = ("status", "priority")
    inlines = [CommentInline]
