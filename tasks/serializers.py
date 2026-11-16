from django.utils.translation import gettext
from rest_framework import serializers

from accounts.models import User
from core.serializers import OwnedSerializer, ScopedPK

from .models import Task, TaskComment


class TaskSerializer(OwnedSerializer):
    franchisee_name = serializers.CharField(source="franchisee.name", read_only=True)
    created_by_name = serializers.CharField(source="created_by.full_name", read_only=True, default=None)
    assignee = ScopedPK(queryset=User.objects.all(), required=False, allow_null=True)
    assignee_name = serializers.CharField(source="assignee.full_name", read_only=True, default=None)
    overdue = serializers.BooleanField(read_only=True)
    comments_count = serializers.SerializerMethodField()
    consistency_fields = ("assignee",)

    class Meta:
        model = Task
        fields = ("id", "franchisee", "franchisee_name", "title", "description", "priority", "status", "due_date",
                  "created_by", "created_by_name", "assignee", "assignee_name", "done_at", "overdue",
                  "comments_count", "created_at")
        read_only_fields = ("status", "created_by", "done_at")

    def get_comments_count(self, obj):
        n = getattr(obj, "comments_n", None)
        return n if n is not None else obj.comments.count()


class TaskCommentSerializer(serializers.ModelSerializer):
    author_name = serializers.CharField(source="author.full_name", read_only=True, default=None)

    class Meta:
        model = TaskComment
        fields = ("id", "task", "author", "author_name", "text", "created_at")
        read_only_fields = ("task", "author", "created_at")

    def validate_text(self, value):
        if not value.strip():
            raise serializers.ValidationError(gettext("Введите текст комментария."))
        return value.strip()


class ActionCommentSerializer(serializers.Serializer):
    comment = serializers.CharField(required=False, allow_blank=True)
