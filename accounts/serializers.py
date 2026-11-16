from django.utils.translation import gettext, gettext_lazy
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from core.l10n import BilingualMixin, L10nField
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from core.exceptions import BusinessError
from core.permissions import FRANCHISEE, NBS_ROLES, OTP_ROLES, PARTNER_ROLES
from core.serializers import ScopedPK
from network.models import Franchisee

from core import access
from .models import AccessRole, User


class FranchiseeBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Franchisee
        fields = ("id", "name", "city", "status")


class PreferencesSerializer(serializers.ModelSerializer):
    """Личные настройки: язык интерфейса и тема (PATCH auth/me/, auth/preferences/)."""

    class Meta:
        model = User
        fields = ("language", "theme")


class UserSerializer(serializers.ModelSerializer):
    """`user` из раздела 6.1 (login, auth/me)."""

    full_name = serializers.CharField(read_only=True)
    is_hq = serializers.BooleanField(read_only=True)
    franchisee = FranchiseeBriefSerializer(read_only=True)
    partner = FranchiseeBriefSerializer(source="franchisee", read_only=True)

    class Meta:
        model = User
        fields = ("id", "email", "first_name", "last_name", "full_name", "phone", "role", "region", "partner", "is_hq",
                  "franchisee", "otp_enabled", "otp_available", "must_change_password", "access_role", "permissions",
                  "modules", "actions", "scope", "language", "theme")

    otp_available = serializers.SerializerMethodField()
    access_role = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()
    modules = serializers.SerializerMethodField()
    actions = serializers.SerializerMethodField()
    scope = serializers.SerializerMethodField()

    def _eff(self, obj):
        cache = self.context.setdefault("_eff", {})
        if obj.pk not in cache:
            cache[obj.pk] = access.compute(obj).as_dict()
        return cache[obj.pk]

    def get_access_role(self, obj):
        r = access.role_object(obj)
        return {"id": r.id, "code": r.code, "name": r.name} if r else None

    def get_permissions(self, obj):
        return self._eff(obj)["permissions"]

    def get_modules(self, obj):
        return self._eff(obj)["modules"]

    def get_actions(self, obj):
        return self._eff(obj)["actions"]

    def get_scope(self, obj):
        return self._eff(obj)["scope"]

    def get_otp_available(self, obj):
        return obj.role in OTP_ROLES


class LoginSerializer(TokenObtainPairSerializer):
    default_error_messages = {"no_active_account": gettext_lazy("Неверный email или пароль.")}

    def validate(self, attrs):
        data = super().validate(attrs)
        if self.user.role in OTP_ROLES and self.user.otp_enabled:
            from django.core import signing
            token = signing.dumps({"uid": self.user.pk}, salt="ozok-otp")
            return {"otp_required": True, "otp_token": token}
        data["user"] = UserSerializer(self.user).data
        return data


class ManagedUserSerializer(serializers.ModelSerializer):
    """Ресурс users/: super_admin — все; franchisee — сотрудники своего партнёра."""

    full_name = serializers.CharField(read_only=True)
    franchisee = ScopedPK(queryset=Franchisee.objects.all(), scope="id", required=False, allow_null=True)
    franchisee_name = serializers.CharField(source="franchisee.name", read_only=True, default=None)
    password = serializers.CharField(write_only=True, required=False, style={"input_type": "password"})
    access_role = serializers.PrimaryKeyRelatedField(queryset=AccessRole.objects.all(), required=False,
                                                     allow_null=True)
    access_role_name = L10nField(source="access_role.name", read_only=True, default=None)
    role = serializers.CharField(required=False)

    class Meta:
        model = User
        fields = ("id", "email", "first_name", "last_name", "full_name", "phone", "role", "franchisee",
                  "franchisee_name", "region", "recruiter_reward_enabled", "is_active", "otp_enabled", "password",
                  "access_role", "access_role_name", "permission_overrides", "must_change_password", "last_login",
                  "invited_by", "language", "theme")
        read_only_fields = ("otp_enabled", "must_change_password", "last_login", "invited_by")

    def validate_role(self, value):
        if value not in User.Role.values:
            raise serializers.ValidationError(gettext("Неизвестная роль."))
        return value

    def validate_permission_overrides(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError(gettext("Ожидается объект."))
        bad = set(value) - {"permissions", "actions_add", "actions_remove", "scope"}
        if bad:
            raise serializers.ValidationError(gettext("Неизвестные ключи: ") + ", ".join(sorted(bad)))
        errs = access.validate_permissions_payload(value.get("permissions") or {}, (value.get("actions_add") or [])
                                                   + (value.get("actions_remove") or []),
                                                   value.get("scope") or "all")
        if errs:
            raise serializers.ValidationError(errs)
        return value

    def validate_email(self, value):
        value = value.lower()
        qs = User.objects.filter(email__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(gettext("Пользователь с таким email уже существует."))
        return value

    def validate_password(self, value):
        validate_password(value)
        return value

    def validate(self, attrs):
        request_user = self.context["request"].user
        arole = attrs.get("access_role")
        if arole is not None:
            attrs["role"] = arole.base_role  # тип пользователя задаёт роль доступа
        elif "role" in attrs and "access_role" not in attrs and self.instance is not None                 and attrs["role"] != self.instance.role:
            attrs["access_role"] = None  # смена базовой роли — роль доступа переопределяется системной
        role = attrs.get("role", self.instance.role if self.instance else None)
        if self.instance is None and not attrs.get("password"):
            raise serializers.ValidationError({"password": [gettext("Укажите пароль.")]})
        if role is None:
            raise serializers.ValidationError({"role": [gettext("Укажите роль.")]})
        if request_user.franchisee_id:  # franchisee: только свои сотрудники
            if role not in (FRANCHISEE, "franchisee_staff", "manager"):
                raise serializers.ValidationError(
                    {"role": [gettext("Можно назначать только роли франчайзи, сотрудника и менеджера.")]})
            if self.instance is None:
                attrs["franchisee"] = request_user.franchisee
            else:
                attrs.pop("franchisee", None)
        else:
            franchisee = attrs.get("franchisee", self.instance.franchisee if self.instance else None)
            if role in PARTNER_ROLES and franchisee is None:
                raise serializers.ValidationError({"franchisee": [gettext("Для роли партнёра укажите партнёра.")]})
            if role in NBS_ROLES and franchisee is not None:
                raise serializers.ValidationError({"franchisee": [gettext("Сотрудник OZOK не привязан к партнёру.")]})
        if request_user.franchisee_id:
            attrs.pop("region", None)
            attrs.pop("recruiter_reward_enabled", None)
        me_super = access.compute(request_user).is_super
        if self.instance is not None and self.instance.pk == request_user.pk and not me_super and (
                {"access_role", "permission_overrides"} & set(attrs) or attrs.get("role") not in (None, self.instance.role)):
            raise serializers.ValidationError(gettext("Нельзя менять собственные права."))
        if role == "super_admin" and attrs.get("permission_overrides"):
            raise serializers.ValidationError({"permission_overrides": [
                gettext("У Super Admin всегда все права: отличия не применяются.")]})
        if self.instance is not None:
            try:
                access.assert_can_manage_user(request_user, self.instance)
            except BusinessError as e:
                raise serializers.ValidationError(str(e.detail))
        # ВСЕГДА проверяем выдачу прав: роль доступа или системная роль по типу пользователя + отличия
        target = attrs.get("access_role") or (self.instance.access_role if self.instance and "role" not in attrs
                                              else None)
        overrides = attrs.get("permission_overrides", self.instance.permission_overrides if self.instance else {})
        try:
            access.check_grant(request_user, role=target, overrides=overrides, role_code=role)
        except BusinessError as e:
            raise serializers.ValidationError({"access_role": [str(e.detail)]})
        if self.instance and self.instance.pk == request_user.pk:
            if attrs.get("is_active") is False:
                raise serializers.ValidationError({"is_active": [gettext("Нельзя деактивировать самого себя.")]})
            if "role" in attrs and attrs["role"] != self.instance.role:
                raise serializers.ValidationError({"role": [gettext("Нельзя изменить собственную роль.")]})
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for k, v in validated_data.items():
            setattr(instance, k, v)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


class AccessRoleSerializer(BilingualMixin, serializers.ModelSerializer):
    bilingual = ('name', 'description')

    users_count = serializers.SerializerMethodField()

    class Meta:
        model = AccessRole
        fields = ("id", "code", "name", "description", "is_system", "base_role", "permissions", "actions", "scope",
                  "users_count", "created_at", "updated_at")
        read_only_fields = ("is_system", "created_at", "updated_at")

    def get_users_count(self, obj):
        return obj.users.count()

    def validate(self, attrs):
        get = lambda k, d=None: attrs[k] if k in attrs else (getattr(self.instance, k) if self.instance else d)  # noqa: E731
        errs = access.validate_permissions_payload(get("permissions", {}), get("actions", []), get("scope", "all"))
        if errs:
            raise serializers.ValidationError(errs)
        if get("base_role") not in User.Role.values:
            raise serializers.ValidationError({"base_role": [gettext("Неизвестный тип пользователя.")]})
        if get("base_role") == "super_admin" and not (self.instance is not None and self.instance.is_system):
            raise serializers.ValidationError({"base_role": [gettext("Нельзя создать роль на основе владельца")]})
        if self.instance is not None:
            if self.instance.is_system:
                attrs.pop("code", None)
                attrs.pop("base_role", None)
                if self.instance.code == "super_admin" and ({"permissions", "actions", "scope"} & set(attrs)):
                    raise serializers.ValidationError(gettext("У Super Admin всегда все права: роль не редактируется."))
        else:
            if not attrs.get("code"):
                import re
                attrs["code"] = "custom-" + re.sub(r"[^a-z0-9]+", "-", attrs.get("name", "role").lower()).strip("-")[:40]
            if attrs["code"] in User.Role.values:
                raise serializers.ValidationError({"code": [gettext("Код занят системной ролью.")]})
        return attrs
