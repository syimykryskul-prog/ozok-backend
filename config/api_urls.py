from django.urls import include, path
from rest_framework.routers import DefaultRouter

from accounts.views import (AccessRoleViewSet, ChangePasswordView, LoginView, MeView, OtpLoginView, RefreshView, TwoFactorDisableView, TwoFactorEnableView,
                            TwoFactorSetupView, UserViewSet)
from billing.views import (AccrualViewSet, CashAccountViewSet, CashCategoryViewSet, CashReportView,
                           CashTransactionViewSet, NbsExpenseViewSet, PaymentViewSet, PayoutViewSet, ChargeViewSet, CommissionPayoutViewSet, FranchiseePaymentViewSet, LedgerCategoryViewSet,
                           LedgerEntryViewSet, RecruiterRewardViewSet)
from core.views import AuditLogViewSet, ChangeRequestViewSet, SettingDetailView, SettingsListView
from clients.views import (BranchAssignmentViewSet, BranchViewSet, ClientContactViewSet, ClientProductAccountViewSet,
                           ChecklistTemplateViewSet, ClientSaleViewSet, ClientViewSet, ContractTemplateViewSet,
                           DealViewSet, EquipmentSaleViewSet, ImplementationViewSet, ImplementerAccrualViewSet,
                           InstallationViewSet, SalesReportView, SubscriptionViewSet)
from clients.legacy_views import (LegacyClientCreateView, LegacyImportConfirmView, LegacyImportPreviewView,
                                  LegacyImportTemplateView)
from dashboard.views import (DashboardView, LedgerReportView, OzokClientsAnalyticsView, PartnerAnalyticsView,
                             SphereAnalyticsView)
from integrations.views import ElqrWebhookView, IntegrationEventsView, IntegrationEventViewSet, IntegrationPingView, IntegrationTestEventView
from leads.views import LeadViewSet, PublicLeadView
from network.views import (CommissionRuleViewSet, ContractViewSet, EquipmentViewSet, FranchiseeViewSet,
                           FranchiseOfferViewSet, PackageViewSet, SetupSplitRuleViewSet, PartnerStatusHistoryViewSet, ProductViewSet,
                           TariffViewSet)
from planning.views import RatingReportView, SalesPlanViewSet
from quality.views import AuditViewSet, TicketViewSet
from tasks.views import TaskViewSet
from training.views import CourseViewSet, EnrollmentViewSet

router = DefaultRouter()
router.register("products", ProductViewSet, basename="product")
router.register("packages", PackageViewSet, basename="package")
router.register("tariffs", TariffViewSet, basename="tariff")
router.register("setup-split-rules", SetupSplitRuleViewSet, basename="setupsplitrule")
router.register("equipment", EquipmentViewSet, basename="equipment")
router.register("commission-rules", CommissionRuleViewSet, basename="commissionrule")
router.register("franchisees", FranchiseeViewSet, basename="franchisee")
router.register("contracts", ContractViewSet, basename="contract")
router.register("leads", LeadViewSet, basename="lead")
router.register("clients", ClientViewSet, basename="client")
router.register("client-accounts", ClientProductAccountViewSet, basename="clientaccount")
router.register("branches", BranchViewSet, basename="branch")
router.register("branch-assignments", BranchAssignmentViewSet, basename="branchassignment")
router.register("client-contacts", ClientContactViewSet, basename="clientcontact")
router.register("subscriptions", SubscriptionViewSet, basename="subscription")
router.register("installations", InstallationViewSet, basename="installation")
router.register("implementer-accruals", ImplementerAccrualViewSet, basename="implementeraccrual")
router.register("checklist-templates", ChecklistTemplateViewSet, basename="checklisttemplate")
router.register("contract-templates", ContractTemplateViewSet, basename="contracttemplate")
router.register("equipment-sales", EquipmentSaleViewSet, basename="equipmentsale")
router.register("implementations", ImplementationViewSet, basename="implementation")
router.register("client-sales", ClientSaleViewSet, basename="clientsale")
router.register("charges", ChargeViewSet, basename="charge")
router.register("franchisee-payments", FranchiseePaymentViewSet, basename="franchiseepayment")
router.register("commission-payouts", CommissionPayoutViewSet, basename="commissionpayout")
router.register("courses", CourseViewSet, basename="course")
router.register("enrollments", EnrollmentViewSet, basename="enrollment")
router.register("tickets", TicketViewSet, basename="ticket")
router.register("audits", AuditViewSet, basename="audit")
router.register("sales-plans", SalesPlanViewSet, basename="salesplan")
router.register("tasks", TaskViewSet, basename="task")
router.register("integration-events", IntegrationEventViewSet, basename="integrationevent")
router.register("access-roles", AccessRoleViewSet, basename="accessrole")
router.register("users", UserViewSet, basename="user")
router.register("audit-log", AuditLogViewSet, basename="auditlog")
router.register("change-requests", ChangeRequestViewSet, basename="changerequest")
router.register("partner-status-history", PartnerStatusHistoryViewSet, basename="partnerstatushistory")
router.register("franchise-offers", FranchiseOfferViewSet, basename="franchiseoffer")
router.register("recruiter-rewards", RecruiterRewardViewSet, basename="recruiterreward")
router.register("ledger-categories", LedgerCategoryViewSet, basename="ledgercategory")
router.register("cash-accounts", CashAccountViewSet, basename="cashaccount")
router.register("cash-categories", CashCategoryViewSet, basename="cashcategory")
router.register("cash-transactions", CashTransactionViewSet, basename="cashtransaction")
router.register("nbs-expenses", NbsExpenseViewSet, basename="nbsexpense")
router.register("payments", PaymentViewSet, basename="payment")
router.register("commission-accruals", AccrualViewSet, basename="commissionaccrual")
router.register("payouts", PayoutViewSet, basename="payout")
router.register("deals", DealViewSet, basename="deal")
router.register("ledger", LedgerEntryViewSet, basename="ledgerentry")

urlpatterns = [
    path("auth/login/", LoginView.as_view(), name="auth-login"),
    path("auth/login/otp/", OtpLoginView.as_view(), name="auth-login-otp"),
    path("auth/2fa/setup/", TwoFactorSetupView.as_view(), name="auth-2fa-setup"),
    path("auth/2fa/enable/", TwoFactorEnableView.as_view(), name="auth-2fa-enable"),
    path("auth/2fa/disable/", TwoFactorDisableView.as_view(), name="auth-2fa-disable"),
    path("settings/", SettingsListView.as_view(), name="settings"),
    path("settings/<str:key>/", SettingDetailView.as_view(), name="setting-detail"),
    path("auth/change-password/", ChangePasswordView.as_view(), name="auth-change-password"),
    path("auth/refresh/", RefreshView.as_view(), name="auth-refresh"),
    path("auth/me/", MeView.as_view(), name="auth-me"),
    path("auth/preferences/", MeView.as_view(), name="auth-preferences"),
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    path("reports/ledger/", LedgerReportView.as_view(), name="report-ledger"),
    path("reports/partner-analytics/", PartnerAnalyticsView.as_view(), name="report-partner-analytics"),
    path("reports/sphere-analytics/", SphereAnalyticsView.as_view(), name="report-sphere-analytics"),
    path("reports/rating/", RatingReportView.as_view(), name="report-rating"),
    path("reports/cash/", CashReportView.as_view(), name="report-cash"),
    path("reports/sales/", SalesReportView.as_view(), name="report-sales"),
    path("leads/public/", PublicLeadView.as_view(), name="lead-public"),  # до роутера: иначе public = pk
    path("clients/legacy/", LegacyClientCreateView.as_view(), name="client-legacy"),  # до роутера: иначе legacy = pk
    path("clients/legacy-import/template/", LegacyImportTemplateView.as_view(), name="client-legacy-template"),
    path("clients/legacy-import/preview/", LegacyImportPreviewView.as_view(), name="client-legacy-preview"),
    path("clients/legacy-import/confirm/", LegacyImportConfirmView.as_view(), name="client-legacy-confirm"),
    path("reports/ozok-clients/", OzokClientsAnalyticsView.as_view(), name="report-ozok-clients"),
    path("integrations/elqr/webhook/", ElqrWebhookView.as_view(), name="integration-elqr-webhook"),
    path("integrations/events/", IntegrationEventsView.as_view(), name="integration-events-post"),
    path("integrations/test-event/", IntegrationTestEventView.as_view(), name="integration-test-event"),
    path("integrations/ping/", IntegrationPingView.as_view(), name="integration-ping"),
    path("", include(router.urls)),
]


