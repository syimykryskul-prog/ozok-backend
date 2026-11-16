from django.utils.translation import gettext, gettext_lazy
from django.core import signing
from rest_framework import generics, permissions
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from core import audit, totp
from core.exceptions import BusinessError
from core.filters import build_filterset
from core.permissions import OTP_ROLES
from core.viewsets import ScopedViewSet

from .models import User
from .serializers import LoginSerializer, ManagedUserSerializer, PreferencesSerializer, UserSerializer

OTP_TOKEN_MAX_AGE = 300


class AuthenticationFailed(APIException):
    status_code = 401
    default_detail = gettext_lazy("Неверный код.")
    default_code = "authentication_failed"


class LoginThrottle(AnonRateThrottle):
    scope = "login"  # 5/мин с IP (settings: DEFAULT_THROTTLE_RATES["login"])


class LoginView(TokenObtainPairView):
    permission_classes = [permissions.AllowAny]
    serializer_class = LoginSerializer
    authentication_classes = []
    throttle_classes = [LoginThrottle]

    def post(self, request, *args, **kwargs):
        email = str(request.data.get("email", ""))[:254].lower()
        off = User.objects.filter(email=email, is_active=False).first()
        if off is not None and off.check_password(str(request.data.get("password", ""))):
            audit.write("login_failed", entity="accounts.User", entity_id=off.pk, user=off,
                        changes={"reason": "deactivated"})
            raise AuthenticationFailed(gettext("Учётная запись отключена."))
        try:
            response = super().post(request, *args, **kwargs)
        except Exception:
            audit.write("login_failed", entity="accounts.User", changes={"email": email})
            raise
        user = User.objects.filter(email=email).first()
        if response.status_code == 200 and user and "otp_required" not in response.data:
            audit.write("login", entity="accounts.User", entity_id=user.pk, user=user)
        return response


class OtpLoginView(APIView):
    """POST auth/login/otp/ {otp_token, code} -> токены (второй шаг входа при включённой 2FA)."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = [LoginThrottle]

    def post(self, request):
        try:
            payload = signing.loads(str(request.data.get("otp_token", "")), salt="ozok-otp",
                                    max_age=OTP_TOKEN_MAX_AGE)
        except signing.BadSignature:
            raise AuthenticationFailed(gettext("Сессия входа истекла: войдите заново."))
        user = User.objects.filter(pk=payload["uid"], is_active=True).first()
        if user is None or not user.otp_enabled:
            raise AuthenticationFailed(gettext("Неверный код."))
        step = totp.verify(user.otp_secret, request.data.get("code"))
        if step is None or step <= user.otp_last_step:
            audit.write("login_failed", entity="accounts.User", entity_id=user.pk, user=user,
                        changes={"reason": "otp"})
            raise AuthenticationFailed(gettext("Неверный код."))
        user.otp_last_step = step
        user.save(update_fields=["otp_last_step"])
        refresh = RefreshToken.for_user(user)
        audit.write("login", entity="accounts.User", entity_id=user.pk, user=user, reason="2FA")
        return Response({"refresh": str(refresh), "access": str(refresh.access_token),
                         "user": UserSerializer(user).data})


class RefreshView(TokenRefreshView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []


class MeView(generics.RetrieveUpdateAPIView):
    """GET auth/me/ — профиль; PATCH auth/me/ — только личные настройки (language, theme)."""
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ["get", "patch", "head", "options"]

    def get_object(self):
        return self.request.user

    def update(self, request, *args, **kwargs):
        ser = PreferencesSerializer(request.user, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(UserSerializer(request.user, context=self.get_serializer_context()).data)


class _TwoFactorBase(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def check_role(self, request):
        if request.user.role not in OTP_ROLES:
            raise PermissionDenied(gettext("2FA доступна ролям Super Admin и Финансист."))


class TwoFactorSetupView(_TwoFactorBase):
    """POST auth/2fa/setup/ -> {secret, otpauth_url}; включается подтверждением кода (enable)."""

    def post(self, request):
        self.check_role(request)
        user = request.user
        if user.otp_enabled:
            raise BusinessError(gettext("2FA уже включена: сначала отключите её."))
        user.otp_secret = totp.generate_secret()
        user.save(update_fields=["otp_secret"])
        return Response({"secret": user.otp_secret,
                         "otpauth_url": totp.provisioning_uri(user.email, user.otp_secret)})


class TwoFactorEnableView(_TwoFactorBase):
    def post(self, request):
        self.check_role(request)
        user = request.user
        if not user.otp_secret:
            raise BusinessError(gettext("Сначала вызовите auth/2fa/setup/."))
        step = totp.verify(user.otp_secret, request.data.get("code"))
        if step is None:
            raise BusinessError(gettext("Неверный код."))
        user.otp_enabled, user.otp_last_step = True, step
        user.save(update_fields=["otp_enabled", "otp_last_step"])
        return Response({"otp_enabled": True})


class TwoFactorDisableView(_TwoFactorBase):
    def post(self, request):
        self.check_role(request)
        user = request.user
        if not user.otp_enabled:
            raise BusinessError(gettext("2FA не включена."))
        if totp.verify(user.otp_secret, request.data.get("code")) is None:
            raise BusinessError(gettext("Неверный код."))
        user.otp_enabled, user.otp_secret, user.otp_last_step = False, "", 0
        user.save(update_fields=["otp_enabled", "otp_secret", "otp_last_step"])
        return Response({"otp_enabled": False})


import secrets  # noqa: E402

from django.contrib.auth.password_validation import validate_password  # noqa: E402
from django.db.models import ProtectedError  # noqa: E402
from rest_framework import viewsets  # noqa: E402
from rest_framework.decorators import action  # noqa: E402

from core import access  # noqa: E402
from core.permissions import RolePermission  # noqa: E402

from .models import AccessRole  # noqa: E402
from .serializers import AccessRoleSerializer  # noqa: E402


def temp_password():
    return secrets.token_urlsafe(9)


class ChangePasswordView(APIView):
    """POST auth/change-password/ {old_password, new_password}: сброс флага must_change_password."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        old, new = str(request.data.get("old_password") or ""), str(request.data.get("new_password") or "")
        if not user.check_password(old):
            raise BusinessError(gettext("Текущий пароль неверный."))
        if old == new:
            raise BusinessError(gettext("Новый пароль должен отличаться от текущего."))
        try:
            validate_password(new, user)
        except Exception as e:  # noqa: BLE001
            raise BusinessError(" ".join(getattr(e, "messages", [str(e)])))
        user.set_password(new)
        user.must_change_password = False
        user.save(update_fields=["password", "must_change_password"])
        audit.write("update", entity="accounts.User", entity_id=user.pk, user=user,
                    changes={"password": ["***", "***"]}, reason=gettext("Смена пароля пользователем"))
        return Response({"detail": gettext("Пароль изменён."), "must_change_password": False})


class AccessRoleViewSet(viewsets.ModelViewSet):
    """access-roles/: роли доступа (матрица раздел→уровень, особые права, область). duplicate/ — копия."""

    resource = "access_roles"
    permission_classes = [RolePermission]
    queryset = AccessRole.objects.all()
    serializer_class = AccessRoleSerializer
    filterset_class = build_filterset(AccessRole, exact=("base_role", "scope"), bools=("is_system",))
    search_fields = ("name", "code")

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.franchisee_id:  # владелец франчайзи видит только партнёрские роли
            qs = qs.filter(base_role__in=("franchisee_staff", "manager"))
        return qs

    def perform_destroy(self, instance):
        if instance.is_system:
            raise BusinessError(gettext("Системные роли редактируются, но не удаляются."))
        if instance.users.exists():
            raise BusinessError(gettext("Роль назначена сотрудникам: сначала переназначьте их."))
        audit.log_event("cancel", instance, reason=gettext("Удаление роли доступа"), user=self.request.user,
                        changes={"name": [instance.name, None]})
        instance.delete()

    @action(detail=True, methods=["post"])
    def duplicate(self, request, pk=None):
        src = self.get_object()
        name = str(request.data.get("name") or gettext('{name} (копия)').format(name=src.name))[:150]
        n = AccessRole.objects.count() + 1
        code = f"custom-{n}"
        while AccessRole.objects.filter(code=code).exists():
            n += 1
            code = f"custom-{n}"
        new = AccessRole.objects.create(code=code, name=name, description=src.description, is_system=False,
                                        base_role=src.base_role, permissions=dict(src.permissions),
                                        actions=list(src.actions), scope=src.scope)
        return Response(self.get_serializer(new).data, status=201)


class UserViewSet(ScopedViewSet):
    resource = "users"
    queryset = User.objects.select_related("franchisee", "access_role").order_by("id")
    serializer_class = ManagedUserSerializer
    filterset_class = build_filterset(User, exact=("role",), bools=("is_active",))
    search_fields = ("email", "first_name", "last_name")
    ordering_fields = ("id", "email", "first_name", "last_name", "role", "is_active")

    def perform_destroy(self, instance):
        if instance.pk == self.request.user.pk:
            raise BusinessError(gettext("Нельзя удалить самого себя."))
        super().perform_destroy(instance)


    # ---- доступ сотрудников (P0+ п.8) ---------------------------------------------------------------------

    def _target(self):
        user = self.get_object()
        if user.pk == self.request.user.pk:
            raise BusinessError(gettext("Это действие нельзя применить к самому себе."))
        from core import access
        access.assert_can_manage_user(self.request.user, user)
        return user

    @action(detail=False, methods=["post"])
    def invite(self, request):
        """Создать сотрудника с временным паролем; must_change_password=true. Пароль показывается один раз."""
        data = {k: v for k, v in request.data.items() if k in (
            "email", "first_name", "last_name", "phone", "role", "region", "franchisee", "access_role",
            "permission_overrides", "recruiter_reward_enabled")}
        password = temp_password()
        ser = self.get_serializer(data={**data, "password": password})
        ser.is_valid(raise_exception=True)
        user = ser.save(must_change_password=True, invited_by=request.user)
        audit.log_event("create", user, reason=gettext("Приглашение сотрудника"), user=request.user,
                        changes={"invited_by": [None, request.user.pk]})
        return Response({**self.get_serializer(user).data, "temporary_password": password}, status=201)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        user = self._target()
        if not user.is_active:
            raise BusinessError(gettext("Сотрудник уже отключён."))
        user.is_active = False
        user.save(update_fields=["is_active"])
        from network.closure import revoke_tokens
        revoke_tokens(user)
        audit.log_event("status", user, changes={"is_active": [True, False]},
                        reason=str(request.data.get("reason") or gettext("Отключение сотрудника")), user=request.user)
        return Response(self.get_serializer(user).data)

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        user = self._target()
        if user.is_active:
            raise BusinessError(gettext("Сотрудник уже включён."))
        user.is_active = True
        user.save(update_fields=["is_active"])
        audit.log_event("status", user, changes={"is_active": [False, True]}, reason=gettext("Включение сотрудника"),
                        user=request.user)
        return Response(self.get_serializer(user).data)

    @action(detail=True, methods=["post"], url_path="reset-password")
    def reset_password(self, request, pk=None):
        user = self._target()
        password = temp_password()
        user.set_password(password)
        user.must_change_password = True
        user.save(update_fields=["password", "must_change_password"])
        from network.closure import revoke_tokens
        revoke_tokens(user)
        audit.log_event("update", user, changes={"password": ["***", "***"], "must_change_password": [False, True]},
                        reason=gettext("Сброс пароля"), user=request.user)
        return Response({"id": user.id, "temporary_password": password, "must_change_password": True})

    @action(detail=True, methods=["get"], url_path="effective-permissions")
    def effective_permissions(self, request, pk=None):
        user = self.get_object()
        eff = access.compute(user)
        role = eff.role
        return Response({"id": user.id, "role": user.role, "access_role": role.id if role else None,
                         "access_role_name": role.name if role else None,
                         "overrides": user.permission_overrides or {}, **eff.as_dict(),
                         "role_permissions": role.permissions if role else {},
                         "role_actions": role.actions if role else []})
