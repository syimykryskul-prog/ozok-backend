from django.db.models import Count
from rest_framework.decorators import action
from rest_framework.response import Response

from core.filters import build_filterset, flag
from core.models import today
from core.viewsets import ScopedViewSet
from django_filters import rest_framework as df

from . import services
from .models import Task
from .serializers import ActionCommentSerializer, TaskCommentSerializer, TaskSerializer


def _overdue(qs, name, value):
    cond = {"due_date__lt": today(), "status__in": Task.ACTIVE_STATUSES}
    return qs.filter(**cond) if value else qs.exclude(**cond)


def _assignee(qs, name, value):
    return qs.filter(assignee_id=value) if str(value).isdigit() else qs


class TaskViewSet(ScopedViewSet):
    resource = "tasks"
    queryset = Task.objects.select_related("franchisee", "created_by", "assignee")
    serializer_class = TaskSerializer
    filterset_class = build_filterset(Task, exact=("status", "priority"), overdue=flag(_overdue),
                                      assignee=df.CharFilter(method=_assignee))
    search_fields = ("title",)
    ordering_fields = ("id", "due_date", "priority", "status", "created_at")

    def get_queryset(self):
        qs = super().get_queryset().order_by("-created_at", "-id").annotate(comments_n=Count("comments", distinct=True))
        if self.request.query_params.get("assignee") == "me":
            qs = qs.filter(assignee=self.request.user)
        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def _comment(self, request):
        ser = ActionCommentSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        return ser.validated_data.get("comment", "")

    def _reply(self, task):
        return Response(self.get_serializer(self.get_queryset().get(pk=task.pk)).data)

    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        return self._reply(services.start_task(self.get_object(), request.user, self._comment(request)))

    @action(detail=True, methods=["post"])
    def done(self, request, pk=None):
        return self._reply(services.done_task(self.get_object(), request.user, self._comment(request)))

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        return self._reply(services.reject_task(self.get_object(), request.user, self._comment(request)))

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return self._reply(services.cancel_task(self.get_object(), request.user))

    @action(detail=True, methods=["get", "post"])
    def comments(self, request, pk=None):
        task = self.get_object()
        if request.method == "POST":
            ser = TaskCommentSerializer(data=request.data)
            ser.is_valid(raise_exception=True)
            comment = services.add_comment(task, request.user, ser.validated_data["text"])
            return Response(TaskCommentSerializer(comment).data, status=201)
        page = self.paginate_queryset(task.comments.select_related("author"))
        return self.get_paginated_response(TaskCommentSerializer(page, many=True).data)

