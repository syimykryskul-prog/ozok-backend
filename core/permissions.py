"""Права по ролям (ТЗ OZOK §3, план P1).

ROLE_ACCESS: роль -> ресурс -> "r" (чтение) | "rw" (чтение и запись).
ACTION_ROLES: (ресурс, действие) -> роли, которым разрешено это действие
(переопределяет общее правило r/rw для конкретного действия вьюсета).
Видимость объектов (чьи данные видны) задаётся отдельно: core.viewsets.
"""
from django.utils.translation import gettext, gettext_lazy
from rest_framework.permissions import SAFE_METHODS, BasePermission

R, RW = "r", "rw"

SUPER_ADMIN, NETWORK_HEAD, RECRUITER = "super_admin", "network_head", "recruiter"
REGIONAL_SUPPORT, IMPLEMENTER, FINANCE, QUALITY = "regional_support", "implementer", "finance", "quality"
FRANCHISEE, FRANCHISEE_STAFF, MANAGER = "franchisee", "franchisee_staff", "manager"

NBS_ROLES = (SUPER_ADMIN, NETWORK_HEAD, RECRUITER, REGIONAL_SUPPORT, IMPLEMENTER, FINANCE, QUALITY)
PARTNER_ROLES = (FRANCHISEE, FRANCHISEE_STAFF, MANAGER)
ALL_ROLES = NBS_ROLES + PARTNER_ROLES
OTP_ROLES = (SUPER_ADMIN, FINANCE)
# совместимость со старым кодом
ADMIN, CURATOR, ACCOUNTANT, OWNER = SUPER_ADMIN, NETWORK_HEAD, FINANCE, FRANCHISEE
HQ_ROLES, FR_ROLES = NBS_ROLES, PARTNER_ROLES
# партнёрские роли без финансов франчайзи (долги, договоры): сотрудник и менеджер
LIMITED_PARTNER_ROLES = (FRANCHISEE_STAFF, MANAGER)

_CATALOG = {"products": R, "packages": R, "tariffs": R, "equipment": R, "commission_rules": R, "settings": R,
            "franchise_offers": R}

ROLE_ACCESS = {
    SUPER_ADMIN: {"*": RW},
    NETWORK_HEAD: {
        "cash_accounts": R, "cash_categories": R, "cash_transactions": R, "cash_report": R,
        "installations": RW, "implementer_accruals": R, "checklist_templates": R, "setup_split_rules": R,
        "contract_templates": R, "equipment_sales": R,
        **_CATALOG, "franchisees": RW, "contracts": R, "leads": RW, "clients": R, "client_accounts": R,
        "implementations": R, "client_sales": R, "charges": R, "payments": R, "commission_payouts": R,
        "courses": RW, "enrollments": RW, "tickets": RW, "audits": RW, "sales_plans": RW, "tasks": RW,
        "users": R, "integration_events": RW, "dashboard": R, "reports": R, "sales_report": R,
        "partner_history": RW, "change_requests": RW, "recruiter_rewards": R, "deals": RW, "ledger": R,
        "ledger_categories": R, "analytics": R, "branches": RW, "subscriptions": R, "client_contacts": RW,
        "branch_assignments": R, "subscription_payments": R, "commission_accruals": R, "payouts": R,
    },
    RECRUITER: {
        "franchisees": R, "recruiter_rewards": R, "settings": R, "dashboard": R,
        "products": R, "packages": R, "tariffs": R,  # справочники для страниц меню рекрутёра (M-10)
    },
    REGIONAL_SUPPORT: {
        "installations": RW, "checklist_templates": R, "contract_templates": R, "equipment_sales": R,
        **_CATALOG, "franchisees": R, "leads": R, "clients": RW, "client_accounts": R, "implementations": RW,
        "tickets": RW, "tasks": R, "courses": R, "enrollments": R, "dashboard": R, "branches": RW,
        "subscriptions": R, "client_contacts": RW, "branch_assignments": R, "subscription_payments": R,
    },
    IMPLEMENTER: {
        "installations": RW, "implementer_accruals": R, "checklist_templates": R,
        "products": R, "packages": R, "tariffs": R, "equipment": R, "settings": R, "implementations": RW,
        "tickets": R,
        "clients": R, "dashboard": R, "branches": R, "client_contacts": R,
    },
    FINANCE: {
        "cash_accounts": RW, "cash_categories": R, "cash_transactions": RW, "cash_report": R,
        "installations": R, "implementer_accruals": RW, "setup_split_rules": R, "contract_templates": R,
        "equipment_sales": R,
        **_CATALOG, "franchisees": R, "contracts": RW, "leads": R, "clients": R, "client_accounts": R,
        "implementations": R, "client_sales": RW, "charges": RW, "payments": RW, "commission_payouts": RW,
        "courses": R, "enrollments": R, "tickets": R, "audits": R, "sales_plans": R, "tasks": R,
        "integration_events": R, "dashboard": R, "reports": R, "sales_report": R, "audit_log": R,
        "partner_history": R, "recruiter_rewards": RW, "deals": R, "ledger": R, "ledger_categories": R,
        "change_requests": R, "analytics": R, "settings": RW, "branches": R, "subscriptions": R,
        "client_contacts": R, "branch_assignments": R, "subscription_payments": RW, "commission_accruals": RW,
        "payouts": RW, "legacy_clients": RW, "users": R,  # users: имена сотрудников для страницы кандидатов
    },
    QUALITY: {
        "installations": R,
        **_CATALOG, "franchisees": R, "clients": R, "client_accounts": R, "implementations": R, "tickets": RW,
        "audits": RW, "tasks": RW, "courses": R, "enrollments": R, "sales_plans": R, "partner_history": R,
        "reports": R, "analytics": R, "dashboard": R, "branches": R, "subscriptions": R, "client_contacts": R,
    },
    FRANCHISEE: {
        "access_roles": R, "installations": RW, "checklist_templates": R, "contract_templates": R, "equipment_sales": RW,
        **_CATALOG, "franchisees": R, "contracts": R, "leads": RW, "clients": RW, "client_accounts": R,
        "implementations": RW, "client_sales": RW, "charges": R, "payments": R, "commission_payouts": R,
        "courses": R, "enrollments": RW, "tickets": RW, "audits": R, "sales_plans": R, "tasks": RW,
        "users": RW, "dashboard": R, "sales_report": R, "change_requests": RW, "deals": RW, "ledger": RW,
        "ledger_categories": R, "analytics": R, "branches": RW, "subscriptions": R, "client_contacts": RW,
        "branch_assignments": R, "subscription_payments": R, "commission_accruals": R, "payouts": R,
    },
    FRANCHISEE_STAFF: {
        "installations": RW, "contract_templates": R, "equipment_sales": RW,
        **_CATALOG, "franchisees": R, "leads": RW, "clients": RW, "client_accounts": R, "implementations": RW,
        "client_sales": RW, "courses": R, "enrollments": R, "tickets": RW, "audits": R, "sales_plans": R,
        "tasks": RW, "users": R, "dashboard": R, "sales_report": R, "change_requests": RW, "deals": RW,
        "branches": R, "subscriptions": R, "client_contacts": RW,
    },
    MANAGER: {
        "installations": RW, "contract_templates": R, "equipment_sales": RW,
        **_CATALOG, "franchisees": R, "leads": RW, "clients": RW, "client_accounts": R, "implementations": RW,
        "client_sales": RW, "courses": R, "enrollments": R, "tickets": RW, "tasks": RW, "users": R,
        "dashboard": R, "sales_report": R, "change_requests": RW, "deals": RW, "branches": R, "subscriptions": R,
        "client_contacts": RW,
    },
}

_NBS_W = (SUPER_ADMIN, NETWORK_HEAD)
_PARTNER_ALL = PARTNER_ROLES
_FIN = (SUPER_ADMIN, FINANCE)
ACTION_ROLES = {
    # заявки
    ("leads", "assign"): _NBS_W, ("leads", "confirm_owner"): _NBS_W,
    ("leads", "take"): PARTNER_ROLES, ("leads", "demo"): PARTNER_ROLES, ("leads", "win"): PARTNER_ROLES,
    ("leads", "status"): PARTNER_ROLES + _NBS_W,
    ("leads", "lose"): PARTNER_ROLES + _NBS_W,
    ("leads", "destroy"): _NBS_W,
    ("leads", "activities"): PARTNER_ROLES + _NBS_W + (REGIONAL_SUPPORT, FINANCE),
    ("leads", "change_request"): PARTNER_ROLES,
    # договоры
    ("contracts", "activate"): _FIN, ("contracts", "close"): _FIN,
    # каталог: ключ интеграции и тест — super_admin; запрос на подключение — франчайзи
    ("products", "rotate_key"): (SUPER_ADMIN,), ("products", "test_event"): (SUPER_ADMIN,),
    ("products", "connect_request"): (FRANCHISEE, FRANCHISEE_STAFF),
    ("packages", "connect_request"): (FRANCHISEE, FRANCHISEE_STAFF),
    # обучение
    ("enrollments", "start"): PARTNER_ROLES,
    ("enrollments", "complete"): _NBS_W,
    ("enrollments", "update"): _NBS_W, ("enrollments", "partial_update"): _NBS_W,
    # обращения
    ("tickets", "status"): (SUPER_ADMIN, NETWORK_HEAD, REGIONAL_SUPPORT, QUALITY, FRANCHISEE, FRANCHISEE_STAFF,
                            MANAGER),
    ("tickets", "rate"): (SUPER_ADMIN, NETWORK_HEAD, FRANCHISEE, FRANCHISEE_STAFF, MANAGER),
    ("tickets", "destroy"): (SUPER_ADMIN,),
    # задачи: ставит OZOK, выполняет партнёр
    ("tasks", "create"): _NBS_W + (QUALITY,), ("tasks", "update"): _NBS_W + (QUALITY,),
    ("tasks", "partial_update"): _NBS_W + (QUALITY,),
    ("tasks", "destroy"): _NBS_W, ("tasks", "cancel"): _NBS_W + (QUALITY,),
    ("tasks", "start"): PARTNER_ROLES, ("tasks", "done"): PARTNER_ROLES, ("tasks", "reject"): PARTNER_ROLES,
    ("tasks", "comments"): ALL_ROLES,
    # продажи клиентам (ставку по продаже меняют super_admin и finance); отмена вместо удаления
    ("client_sales", "recalculate"): _FIN,
    ("client_sales", "create"): (SUPER_ADMIN, FRANCHISEE, FRANCHISEE_STAFF, MANAGER),
    ("client_sales", "cancel"): (SUPER_ADMIN, FINANCE, FRANCHISEE, FRANCHISEE_STAFF, MANAGER),
    ("client_sales", "update"): (SUPER_ADMIN, FINANCE, FRANCHISEE, FRANCHISEE_STAFF, MANAGER),
    ("client_sales", "partial_update"): (SUPER_ADMIN, FINANCE, FRANCHISEE, FRANCHISEE_STAFF, MANAGER),
    ("charges", "cancel"): _FIN, ("payments", "cancel"): _FIN, ("commission_payouts", "cancel"): _FIN,
    # пользователи
    ("users", "invite"): (SUPER_ADMIN, FRANCHISEE), ("users", "deactivate"): (SUPER_ADMIN, FRANCHISEE),
    ("users", "activate"): (SUPER_ADMIN, FRANCHISEE), ("users", "reset_password"): (SUPER_ADMIN, FRANCHISEE),
    ("users", "effective_permissions"): (SUPER_ADMIN, FRANCHISEE),
    ("access_roles", "create"): (SUPER_ADMIN,), ("access_roles", "update"): (SUPER_ADMIN,),
    ("access_roles", "partial_update"): (SUPER_ADMIN,), ("access_roles", "destroy"): (SUPER_ADMIN,),
    ("access_roles", "duplicate"): (SUPER_ADMIN,),
    ("users", "create"): (SUPER_ADMIN, FRANCHISEE), ("users", "update"): (SUPER_ADMIN, FRANCHISEE),
    ("users", "partial_update"): (SUPER_ADMIN, FRANCHISEE), ("users", "destroy"): (SUPER_ADMIN, FRANCHISEE),
    # журнал интеграции
    ("integration_events", "resolve"): _NBS_W, ("integration_events", "retry"): _NBS_W,
    # партнёры: воронка, статусы
    ("franchisees", "create"): _NBS_W, ("franchisees", "destroy"): (SUPER_ADMIN,),
    ("franchisees", "update"): _NBS_W, ("franchisees", "partial_update"): _NBS_W,
    ("partner_history", "create"): _NBS_W,
    # вознаграждение рекрутера: проверка и выплата — финансы
    ("recruiter_rewards", "verify"): _FIN, ("recruiter_rewards", "pay"): _FIN,
    ("recruiter_rewards", "cancel"): _FIN, ("recruiter_rewards", "create"): _FIN,
    ("recruiter_rewards", "update"): (), ("recruiter_rewards", "partial_update"): (),
    ("recruiter_rewards", "destroy"): (),
    # запросы на изменение: решает OZOK (список ролей дополнительно проверяется по настройке)
    ("change_requests", "approve"): _NBS_W, ("change_requests", "reject"): _NBS_W,
    ("change_requests", "update"): (), ("change_requests", "partial_update"): (), ("change_requests", "destroy"): (),
    # предложения франшизы и каталог правил — super_admin
    ("franchise_offers", "create"): (SUPER_ADMIN,), ("franchise_offers", "update"): (SUPER_ADMIN,),
    ("franchise_offers", "partial_update"): (SUPER_ADMIN,), ("franchise_offers", "destroy"): (SUPER_ADMIN,),
    # филиалы (P5): принимает приоритетный партнёр; переназначает и привязывает счёт OZOK
    ("branches", "accept"): (SUPER_ADMIN, FRANCHISEE), ("branches", "reassign"): _NBS_W,
    ("branches", "elqr_account"): _NBS_W + (FINANCE,),
    ("branches", "create"): (SUPER_ADMIN, NETWORK_HEAD, REGIONAL_SUPPORT, FRANCHISEE),
    ("branches", "update"): (SUPER_ADMIN, NETWORK_HEAD, REGIONAL_SUPPORT, FRANCHISEE),
    ("branches", "partial_update"): (SUPER_ADMIN, NETWORK_HEAD, REGIONAL_SUPPORT, FRANCHISEE),
    ("subscriptions", "close"): _NBS_W + (FINANCE,),
    ("franchisees", "close"): _NBS_W, ("franchisees", "reopen"): (SUPER_ADMIN,),
    # касса: вручную только finance и super_admin; статьи — super_admin
    ("cash_transactions", "create"): _FIN, ("cash_transactions", "cancel"): _FIN,
    ("cash_accounts", "create"): _FIN, ("cash_accounts", "update"): _FIN, ("cash_accounts", "partial_update"): _FIN,
    ("cash_categories", "create"): (SUPER_ADMIN,), ("cash_categories", "update"): (SUPER_ADMIN,),
    ("cash_categories", "partial_update"): (SUPER_ADMIN,),
    # установки (P7)
    ("installations", "channels"): _NBS_W + (IMPLEMENTER, REGIONAL_SUPPORT),
    ("installations", "create"): _NBS_W + (REGIONAL_SUPPORT, FRANCHISEE, FRANCHISEE_STAFF, MANAGER),
    ("implementer_accruals", "verify"): _FIN, ("implementer_accruals", "pay"): _FIN,
    ("checklist_templates", "create"): (SUPER_ADMIN,), ("checklist_templates", "update"): (SUPER_ADMIN,),
    ("checklist_templates", "partial_update"): (SUPER_ADMIN,),
    ("setup_split_rules", "create"): (SUPER_ADMIN,), ("setup_split_rules", "update"): (SUPER_ADMIN,),
    ("setup_split_rules", "partial_update"): (SUPER_ADMIN,),
    ("contract_templates", "create"): (SUPER_ADMIN,), ("contract_templates", "update"): (SUPER_ADMIN,),
    ("contract_templates", "partial_update"): (SUPER_ADMIN,),
    ("equipment_sales", "update"): _NBS_W + (REGIONAL_SUPPORT, FRANCHISEE, FRANCHISEE_STAFF, MANAGER, IMPLEMENTER),
    ("equipment_sales", "partial_update"): _NBS_W + (REGIONAL_SUPPORT, FRANCHISEE, FRANCHISEE_STAFF, MANAGER),
    # оплаты абонплаты, начисления, выплаты (P9/P8): ввод, импорт и выплаты — финансист
    ("subscription_payments", "create"): _FIN, ("subscription_payments", "cancel"): _FIN,
    ("subscription_payments", "refund"): _FIN, ("subscription_payments", "import_preview"): _FIN,
    ("subscription_payments", "import_confirm"): _FIN,
    ("commission_accruals", "release"): _FIN, ("commission_accruals", "cancel"): _FIN,
    ("payouts", "create"): _FIN, ("payouts", "verify"): _FIN, ("payouts", "pay"): _FIN, ("payouts", "cancel"): _FIN,
    # продажи одной формой
    ("deals", "approve"): _NBS_W, ("deals", "mark_paid"): _FIN,
    # существующие клиенты OZOK (P0+ п.9): ввод, шаблон и импорт — финансист/владелец и тем, кому выдано право
    ("legacy_clients", "create"): _FIN, ("legacy_clients", "template"): _FIN,
    ("legacy_clients", "import_preview"): _FIN, ("legacy_clients", "import_confirm"): _FIN,
    ("branches", "commission_switch"): (SUPER_ADMIN,),
    ("ledger_categories", "create"): (SUPER_ADMIN,), ("ledger_categories", "update"): (SUPER_ADMIN,),
    ("ledger_categories", "partial_update"): (SUPER_ADMIN,), ("ledger_categories", "destroy"): (SUPER_ADMIN,),
}


def role_access(role, resource):
    """Прежняя константная матрица — только для сидера и справки; проверки идут через core.access (БД)."""
    table = ROLE_ACCESS.get(role, {})
    return table.get(resource) or table.get("*")


def role_consistent(user):
    """Роль и привязка пользователя согласованы (OZOK без партнёра / роль партнёра с партнёром)."""
    if user.role in NBS_ROLES:
        return user.franchisee_id is None
    if user.role in PARTNER_ROLES:
        return user.franchisee_id is not None
    return False


class RolePermission(BasePermission):
    message = gettext_lazy("У вас нет прав для этого действия.")

    def has_permission(self, request, view):
        from . import access
        user = request.user
        if not (user and user.is_authenticated and role_consistent(user)):
            return False
        if getattr(user, "must_change_password", False):
            self.message = gettext("Требуется сменить временный пароль (auth/change-password/).")
            return False
        resource = getattr(view, "resource", None)
        if resource is None:
            return True
        eff = access.get_effective(request)
        level = eff.level(resource)
        if level == 0:
            return False
        action = getattr(view, "action", None)
        if (resource, action) in access.GUARDED:
            return eff.can(resource, action)
        if request.method in SAFE_METHODS:
            return True
        return level >= access.LEVEL[access.WRITE]
