from django.utils.translation import gettext, gettext_lazy
from rest_framework import serializers

from core.l10n import BilingualMixin, L10nField

from accounts.models import User
from core.serializers import ScopedPK

from . import services
from .models import Course, Enrollment


class CourseSerializer(BilingualMixin, serializers.ModelSerializer):
    bilingual = ('title', 'description')

    enrolled_count = serializers.IntegerField(read_only=True, default=0)
    passed_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Course
        fields = ("id", "title", "product", "description", "materials_url", "pass_score", "validity_months",
                  "is_active", "enrolled_count", "passed_count")

    def validate_pass_score(self, value):
        if not 0 <= value <= 100:
            raise serializers.ValidationError(gettext("Проходной балл — от 0 до 100."))
        return value


class EnrollmentSerializer(serializers.ModelSerializer):
    user = ScopedPK(queryset=User.objects.all())
    course_title = L10nField(source="course.title", read_only=True)
    user_name = serializers.CharField(source="user.full_name", read_only=True)
    franchisee_name = serializers.CharField(source="franchisee.name", read_only=True)
    expired = serializers.BooleanField(read_only=True)

    class Meta:
        model = Enrollment
        fields = ("id", "course", "course_title", "user", "user_name", "franchisee", "franchisee_name", "status",
                  "score", "assigned_at", "completed_at", "expires_at", "expired")
        read_only_fields = ("franchisee", "status", "score", "assigned_at", "completed_at", "expires_at")
        validators = []  # уникальность course+user обрабатывает services.enroll

    def validate(self, attrs):
        if self.instance is not None:
            attrs.pop("course", None)
            attrs.pop("user", None)
        return attrs

    def create(self, validated_data):
        return services.enroll(validated_data["course"], validated_data["user"])


class ScoreSerializer(serializers.Serializer):
    score = serializers.IntegerField(min_value=0, max_value=100,
                                     error_messages={"required": gettext_lazy("Укажите балл."),
                                                     "min_value": gettext_lazy("Балл — от 0 до 100."),
                                                     "max_value": gettext_lazy("Балл — от 0 до 100.")})
