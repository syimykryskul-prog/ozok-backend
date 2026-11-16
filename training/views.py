from django.utils.translation import gettext
from django.db.models import Count, Q
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from core.filters import build_filterset, flag, product_filter
from core.models import today
from core.permissions import LIMITED_PARTNER_ROLES, RolePermission
from core.viewsets import ScopedViewSet

from . import services
from .models import Course, Enrollment
from .serializers import CourseSerializer, EnrollmentSerializer, ScoreSerializer


class CourseViewSet(viewsets.ModelViewSet):
    resource = "courses"
    permission_classes = [RolePermission]
    serializer_class = CourseSerializer
    filterset_class = build_filterset(Course, bools=("is_active",), product=product_filter())
    search_fields = ("title",)
    ordering_fields = ("id", "title")

    def get_queryset(self):
        qs = Course.objects.order_by("id")
        user = self.request.user
        if user.franchisee_id:  # счётчики — только по своему франчайзи
            scope = Q(enrollments__franchisee_id=user.franchisee_id)
        else:
            scope = Q()
        return qs.annotate(
            enrolled_count=Count("enrollments", filter=scope, distinct=True),
            passed_count=Count("enrollments", filter=scope & Q(enrollments__status="passed"), distinct=True))


def _expired(qs, name, value):
    cond = Q(status="passed", expires_at__lt=today())
    return qs.filter(cond) if value else qs.exclude(cond)


class EnrollmentViewSet(ScopedViewSet):
    resource = "enrollments"
    queryset = Enrollment.objects.select_related("course", "user", "franchisee")
    serializer_class = EnrollmentSerializer
    filterset_class = build_filterset(Enrollment, exact=("status",), ids=("course", "user"),
                                      expired=flag(_expired))
    search_fields = ("user__first_name", "user__last_name", "user__email")
    ordering_fields = ("id", "assigned_at", "expires_at", "status")

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.role in LIMITED_PARTNER_ROLES:  # менеджер видит только своё обучение
            qs = qs.filter(user=self.request.user)
        return qs

    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        enrollment = self.get_object()
        if request.user.role in LIMITED_PARTNER_ROLES and enrollment.user_id != request.user.id:
            raise PermissionDenied(gettext("Начать курс может сам сотрудник или владелец франчайзи."))
        return Response(self.get_serializer(services.start(enrollment)).data)

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        ser = ScoreSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        enrollment = services.complete(self.get_object(), ser.validated_data["score"])
        return Response(self.get_serializer(enrollment).data)

