"""Права из БД (P0+ п.8): AccessRole + индивидуальные отличия -> итоговые права пользователя.

Матрица ROLE_ACCESS / ACTION_ROLES в core.permissions — только ИСТОЧНИК СИДЕРА системных ролей и перечень
защищённых действий; проверка прав в API читает итоговые права из БД (кэш на запрос)."""
from django.utils.translation import gettext
from . import permissions as P
from .exceptions import BusinessError

NONE, READ, WRITE = "none", "read", "write"
LEVEL = {NONE: 0, READ: 1, WRITE: 2}
LEVEL_NAME = {0: NONE, 1: READ, 2: WRITE}

GUARDED = set(P.ACTION_ROLES)                                   # {(resource, action)}
GUARDED_KEYS = {f"{r}.{a}" for r, a in GUARDED}
BLOCKED_KEYS = {f"{r}.{a}" for (r, a), roles in P.ACTION_ROLES.items() if not roles}  # никому, даже super_admin
ALL_RESOURCES = sorted({r for t in P.ROLE_ACCESS.values() for r in t if r != "*"} | {r for r, _ in GUARDED})
SCOPES = ("all", "own_region", "own_partners", "assigned")
SCOPE_BY_ROLE = {P.RECRUITER: "own_partners", P.REGIONAL_SUPPORT: "own_region", P.IMPLEMENTER: "assigned"}

# Особые права = группы защищённых действий (можно выдавать по имени группы или по ключу resource.action)
ACTION_GROUPS = {
    "approve_discounts": ["deals.approve"],
    "approve_lead_changes": ["change_requests.approve", "change_requests.reject", "leads.confirm_owner",
                             "leads.assign"],
    "verify_payouts": ["payouts.verify", "recruiter_rewards.verify", "implementer_accruals.verify"],
    "pay_payouts": ["payouts.pay", "recruiter_rewards.pay", "implementer_accruals.pay"],
    "cancel_financial": ["charges.cancel", "payments.cancel", "commission_payouts.cancel",
                         "subscription_payments.cancel", "cash_transactions.cancel", "client_sales.cancel",
                         "recruiter_rewards.cancel"],
    "change_prices_rules": ["franchise_offers.create", "franchise_offers.update", "franchise_offers.partial_update",
                            "setup_split_rules.create", "setup_split_rules.update",
                            "setup_split_rules.partial_update", "checklist_templates.create",
                            "contract_templates.create"],
    "manage_partner_status": ["franchisees.close", "partner_history.create", "franchisees.update",
                              "franchisees.partial_update", "franchisees.create"],
    "manage_users": ["users.create", "users.update", "users.partial_update", "users.destroy", "users.invite",
                     "users.deactivate", "users.activate", "users.reset_password"],
    "import_legacy_clients": ["legacy_clients.create", "legacy_clients.template", "legacy_clients.import_preview",
                              "legacy_clients.import_confirm"],
    "mark_deals_paid": ["deals.mark_paid"],
    "view_personal_data": [],   # флаги без отдельных защищённых действий: используются интерфейсом
    "export": [],
}
KNOWN_ACTIONS = GUARDED_KEYS | set(ACTION_GROUPS)

# Разделы меню (модули плана) -> ресурсы API; уровень модуля = максимум по его ресурсам
MODULES = {
    "dashboard": ["dashboard"], "leads": ["leads"], "sales": ["deals", "client_sales", "equipment_sales"],
    # оплаты абонплаты (subscription_payments) — данные клиентов, а не Касса OZOK (D4)
    "clients": ["clients", "client_accounts", "branches", "subscriptions", "client_contacts", "branch_assignments",
                "subscription_payments"],
    "legacy_clients": ["legacy_clients"],
    "installations": ["installations", "implementations", "implementer_accruals"],
    "cash": ["cash_accounts", "cash_transactions", "cash_report", "cash_categories"],
    "cash_partners": ["charges", "payments"], "payouts": ["payouts", "commission_payouts", "commission_accruals"],
    "candidates": ["partner_history"], "partners": ["franchisees", "franchise_offers"],
    "recruiters": ["recruiter_rewards"], "franchise_contracts": ["contracts", "franchise_offers"],
    "tasks": ["tasks"], "quality": ["tickets", "audits"], "training": ["courses", "enrollments"],
    "plans_rating": ["sales_plans"], "approvals": ["change_requests"], "integrations": ["integration_events"],
    "audit_log": ["audit_log"], "settings": ["settings", "commission_rules", "access_roles", "products", "tariffs",
                                              "packages", "equipment", "setup_split_rules", "checklist_templates",
                                              "contract_templates", "ledger_categories"],
    "users": ["users"], "reports": ["reports", "sales_report", "analytics", "ledger"],
}
# разделы, доступные партнёрским ролям (граница выдачи прав владельцем франчайзи)
PARTNER_RESOURCES = {r for role in P.PARTNER_ROLES for r in P.ROLE_ACCESS[role]} | {
    "access_roles", "installations", "equipment_sales", "client_contacts", "branches", "subscriptions"}
PARTNER_ACTIONS = {f"{r}.{a}" for (r, a), roles in P.ACTION_ROLES.items() if set(roles) & set(P.PARTNER_ROLES)}


def expand_actions(actions):
    out = set()
    for a in actions or []:
        if a in ACTION_GROUPS:
            out.update(ACTION_GROUPS[a])
            out.add(a)
        else:
            out.add(a)
    return out


# ---- сидер системных ролей --------------------------------------------------------------------------------------

def seed_definitions():
    defs = {}
    names = {code: label for code, label in __import__("accounts.models", fromlist=["User"]).User.Role.choices}
    for role in P.ALL_ROLES:
        table = P.ROLE_ACCESS[role]
        if "*" in table:
            perms = {r: WRITE for r in ALL_RESOURCES}
        else:
            perms = {r: (WRITE if lvl == P.RW else READ) for r, lvl in table.items()}
        actions = sorted(f"{r}.{a}" for (r, a), roles in P.ACTION_ROLES.items() if role in roles)
        defs[role] = {"name": names[role], "base_role": role, "permissions": perms, "actions": actions,
                      "scope": SCOPE_BY_ROLE.get(role, "all")}
    return defs


def seed_system_roles(model=None):
    """Создать/досоздать системные роли (идемпотентно, существующие не перезаписываются)."""
    from accounts.models import AccessRole
    model = model or AccessRole
    for code, d in seed_definitions().items():
        model.objects.get_or_create(code=code, defaults={**d, "is_system": True,
                                                         "description": gettext("Системная роль ТЗ OZOK §3")})


# ---- итоговые права ----------------------------------------------------------------------------------------------

class Effective:
    def __init__(self, permissions, actions, scope, role_obj, is_super):
        self.permissions, self.actions, self.scope, self.role = permissions, actions, scope, role_obj
        self.is_super = is_super

    def level(self, resource):
        if self.is_super:
            return LEVEL[WRITE]
        return LEVEL.get(self.permissions.get(resource, NONE), 0)

    def can(self, resource, action):
        key = f"{resource}.{action}"
        if key in BLOCKED_KEYS:
            return False
        return key in self.actions

    def as_dict(self):
        resources = ALL_RESOURCES if self.is_super else sorted(self.permissions)
        perms = {r: LEVEL_NAME[self.level(r)] for r in resources}
        modules = {m: LEVEL_NAME[max([self.level(r) for r in rs] or [0])] for m, rs in MODULES.items()}
        actions = sorted(a for a in self.actions if a not in BLOCKED_KEYS)
        return {"permissions": perms, "modules": modules, "actions": actions, "scope": self.scope}


def role_object(user):
    from accounts.models import AccessRole
    if user.access_role_id:
        return user.access_role
    return AccessRole.objects.filter(code=user.role).first()


def compute(user):
    role = role_object(user)
    # владелец — только пользователь с СИСТЕМНОЙ ролью super_admin; кастомная роль с base_role=super_admin прав не даёт (D3)
    is_super = user.role == P.SUPER_ADMIN and (role is None or (role.is_system and role.code == P.SUPER_ADMIN))
    perms = dict(role.permissions) if role else {}
    actions = expand_actions(role.actions) if role else set()
    scope = role.scope if role else "all"
    ov = user.permission_overrides or {}
    if not is_super:  # владелец (super_admin) всегда имеет все права: отличия к нему не применяются
        for r, lvl in (ov.get("permissions") or {}).items():
            if lvl in LEVEL:
                perms[r] = lvl
        actions |= expand_actions(ov.get("actions_add"))
        actions -= expand_actions(ov.get("actions_remove"))
        if ov.get("scope") in SCOPES:
            scope = ov["scope"]
    return Effective(perms, actions, scope, role, is_super)


def get_effective(request):
    """Итоговые права пользователя запроса; считаются один раз на запрос."""
    eff = getattr(request, "_access_effective", None)
    if eff is None:
        eff = compute(request.user)
        try:
            request._access_effective = eff
        except Exception:  # noqa: BLE001
            pass
    return eff


# ---- защитные правила --------------------------------------------------------------------------------------------

def active_super_admins(exclude_pk=None):
    from accounts.models import User
    qs = User.objects.filter(role=P.SUPER_ADMIN, is_active=True)
    return qs.exclude(pk=exclude_pk) if exclude_pk else qs


def before_user_save(user):
    """Роль доступа и базовая роль согласованы; последнего super_admin нельзя отключить или понизить."""
    from accounts.models import AccessRole, User
    if user.pk:
        old = User.objects.filter(pk=user.pk).values("role", "is_active").first()
        if old and old["role"] == P.SUPER_ADMIN and old["is_active"]:
            demoted = user.role != P.SUPER_ADMIN
            if (demoted or not user.is_active) and not active_super_admins(exclude_pk=user.pk).exists():
                raise BusinessError(gettext("Нельзя отключить или понизить последнего Super Admin."))
    role = user.access_role if user.access_role_id else None
    if role is not None and not role.is_system:
        user.role = role.base_role
    elif role is None or role.code != user.role:
        user.access_role = AccessRole.objects.filter(code=user.role).first()


def validate_permissions_payload(permissions, actions, scope):
    errors = {}
    if not isinstance(permissions, dict) or any(r not in ALL_RESOURCES or lvl not in LEVEL
                                                for r, lvl in permissions.items()):
        errors["permissions"] = [gettext("Матрица: раздел -> none|read|write; разделы должны быть известными.")]
    if not isinstance(actions, list) or any(a not in KNOWN_ACTIONS for a in actions):
        errors["actions"] = [gettext("Неизвестное особое право.")]
    if scope not in SCOPES:
        errors["scope"] = [gettext("Область: all, own_region, own_partners или assigned.")]
    return errors


def target_effective(role, overrides, role_code=None):
    """Права, которые получит пользователь: роль доступа (или системная роль по коду) + отличия."""
    from accounts.models import AccessRole
    if role is None and role_code:
        role = AccessRole.objects.filter(code=role_code).first()
    is_super = bool(role is not None and role.is_system and role.code == P.SUPER_ADMIN)
    ov = overrides or {}
    perms = dict(role.permissions) if role else {}
    actions = expand_actions(role.actions) if role else set()
    if not is_super:
        perms.update({r: l for r, l in (ov.get("permissions") or {}).items() if l in LEVEL})
        actions = (actions | expand_actions(ov.get("actions_add"))) - expand_actions(ov.get("actions_remove"))
    return Effective(perms, actions, "all", role, is_super)


def check_grant(granter, *, role, overrides, role_code=None):
    """Выдача прав: НИКТО не выдаёт больше собственных (разделы, уровни, особые права) — ни через роль, ни через
    отличия, ни через смену типа пользователя. Super_admin — без ограничений. Владелец франчайзи дополнительно
    ограничен партнёрскими разделами/ролями."""
    mine = compute(granter)
    if mine.is_super:
        return
    target = target_effective(role, overrides, role_code)
    if target.is_super:
        raise BusinessError(gettext("Роль владельца может выдать только Super Admin."))
    if granter.franchisee_id and role is not None and role.base_role not in P.PARTNER_ROLES:
        raise BusinessError(gettext("Партнёру можно выдавать только партнёрские роли."))
    for r, lvl in target.permissions.items():
        if LEVEL[lvl] == 0:
            continue
        if granter.franchisee_id and r not in PARTNER_RESOURCES:
            raise BusinessError(gettext('Раздел «{r}» нельзя выдавать: доступны только партнёрские разделы.').format(r=r))
        if LEVEL[lvl] > mine.level(r):
            raise BusinessError(gettext('Нельзя выдать права на «{r}» выше собственных.').format(r=r))
    for a in target.actions:
        if a in ACTION_GROUPS:
            continue
        if (granter.franchisee_id and a not in PARTNER_ACTIONS) or a not in mine.actions:
            raise BusinessError(gettext('Нельзя выдать особое право «{a}»: оно выходит за ваши права.').format(a=a))


def assert_can_manage_user(granter, target):
    """Изменять/отключать можно только пользователя, чьи права не выше собственных; владельца партнёра сотрудники
    партнёра не трогают."""
    mine = compute(granter)
    if mine.is_super or granter.pk == target.pk:
        return
    if granter.franchisee_id and target.franchisee_id == granter.franchisee_id and target.role == P.FRANCHISEE:
        raise BusinessError(gettext("Нельзя изменять или отключать владельца партнёра."))
    cur = compute(target)
    if cur.is_super:
        raise BusinessError(gettext("Нельзя изменять Super Admin."))
    for r, lvl in cur.permissions.items():
        if LEVEL[lvl] > mine.level(r):
            raise BusinessError(gettext('Права пользователя на «{r}» выше ваших: изменять его нельзя.').format(r=r))
    for a in cur.actions:
        if a not in ACTION_GROUPS and a not in mine.actions:
            raise BusinessError(gettext("Права пользователя выше ваших: изменять его нельзя."))
