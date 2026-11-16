from django.utils.translation import gettext
from decimal import Decimal

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from clients.services import cancel_derived
from core.exceptions import BusinessError
from core.filters import build_filterset, flag
from core.models import money, today
from core.permissions import RolePermission
from core.viewsets import CancelOnlyMixin, FranchiseeScopedMixin, ScopedReadOnlyViewSet, ScopedViewSet

from . import recruiters
from .models import Charge, CommissionPayout, FranchiseePayment, LedgerCategory, PartnerLedgerEntry, RecruiterReward
from .serializers import (ChargeSerializer, CommissionPayoutSerializer, FranchiseePaymentSerializer,
                          LedgerCategorySerializer, LedgerEntrySerializer, RecruiterRewardSerializer)


def _overdue(qs, name, value):
    cond = {"due_date__lt": today()}
    return qs.filter(**cond) if value else qs.exclude(**cond)


class ChargeViewSet(CancelOnlyMixin, ScopedViewSet):
    resource = "charges"
    queryset = Charge.all_objects.select_related("franchisee", "contract")
    serializer_class = ChargeSerializer
    filterset_class = build_filterset(Charge, exact=("kind",), ids=("contract",), date_field="due_date",
                                      overdue=flag(_overdue))
    search_fields = ("description",)
    ordering_fields = ("id", "due_date", "amount", "created_at")


class _MoneyViewSet(CancelOnlyMixin, ScopedViewSet):
    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def cancel_service(self, obj, user, reason):
        if obj.method == "offset":
            raise BusinessError(gettext("Автоматический зачёт отменяется вместе с продажей, по которой он создан."))
        if isinstance(obj, FranchiseePayment):
            obj.status = FranchiseePayment.Status.CANCELLED
        return obj.mark_cancelled(user, reason)


class FranchiseePaymentViewSet(_MoneyViewSet):
    resource = "payments"
    queryset = FranchiseePayment.all_objects.select_related("franchisee", "created_by")
    serializer_class = FranchiseePaymentSerializer
    filterset_class = build_filterset(FranchiseePayment, exact=("method", "status"), date_field="date")
    search_fields = ("comment",)
    ordering_fields = ("id", "date", "amount")


class CommissionPayoutViewSet(ScopedReadOnlyViewSet):
    """commission-payouts/: только история старых ручных выплат. Новые выплаты — через payouts/ (D1)."""

    resource = "commission_payouts"
    queryset = CommissionPayout.all_objects.select_related("franchisee", "created_by")
    serializer_class = CommissionPayoutSerializer
    filterset_class = build_filterset(CommissionPayout, exact=("method",), date_field="date")
    search_fields = ("comment",)
    ordering_fields = ("id", "date", "amount")


class RecruiterRewardViewSet(FranchiseeScopedMixin, viewsets.ReadOnlyModelViewSet):
    """recruiter-rewards/: рекрутёр видит только свои; verify/pay — финансы (verified_by != paid_by)."""

    resource = "recruiter_rewards"
    franchisee_lookup = "partner_id"
    recruiter_lookup = "recruiter_id"
    queryset = RecruiterReward.objects.select_related("partner", "recruiter")
    serializer_class = RecruiterRewardSerializer
    filterset_class = build_filterset(RecruiterReward, exact=("status",), ids=("partner", "recruiter"))
    ordering_fields = ("id", "accrued_amount", "status")

    def _reply(self, reward):
        return Response(self.get_serializer(reward).data)

    @action(detail=True, methods=["post"])
    def verify(self, request, pk=None):
        return self._reply(recruiters.verify(self.get_object(), request.user))

    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        from django.utils.dateparse import parse_date
        d = parse_date(str(request.data.get("date") or "")) if request.data.get("date") else None
        return self._reply(recruiters.pay(self.get_object(), request.user, date=d,
                                          payment_ref=str(request.data.get("payment_ref") or "")))

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return self._reply(recruiters.cancel(self.get_object(), request.user, request.data.get("reason")))

    @action(detail=False, methods=["get"])
    def report(self, request):
        return Response(recruiters.recruiters_report())


class LedgerCategoryViewSet(viewsets.ModelViewSet):
    resource = "ledger_categories"
    permission_classes = [RolePermission]
    queryset = LedgerCategory.objects.all()
    serializer_class = LedgerCategorySerializer
    filterset_class = build_filterset(LedgerCategory, exact=("type",), bools=("is_active",))
    http_method_names = ["get", "post", "put", "patch", "head", "options"]  # категории отключаются, не удаляются


class LedgerEntryViewSet(CancelOnlyMixin, ScopedViewSet):
    resource = "ledger"
    franchisee_lookup = "partner_id"
    queryset = PartnerLedgerEntry.all_objects.select_related("partner", "category")
    serializer_class = LedgerEntrySerializer
    filterset_class = build_filterset(PartnerLedgerEntry, exact=("type",), ids=("partner", "category"),
                                      date_field="date")
    search_fields = ("description",)
    ordering_fields = ("id", "date", "amount")

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


# ---- P9 / P8 ------------------------------------------------------------------------------------------------------
from django.db.models import Q, Sum  # noqa: E402
from rest_framework import mixins  # noqa: E402

from clients import branches as branch_svc  # noqa: E402
from clients.models import Branch  # noqa: E402
from core.models import parse_month  # noqa: E402

from . import accruals as accrual_svc  # noqa: E402
from . import payments as payment_svc  # noqa: E402
from . import payouts as payout_svc  # noqa: E402
from .models import CommissionAccrual, Payment, Payout  # noqa: E402
from .serializers import (AccrualSerializer, PaymentCreateSerializer, PaymentSerializer,  # noqa: E402
                          PayoutSerializer)
from django_filters import rest_framework as df  # noqa: E402


def _payment_scope(fid):
    return branch_svc.served_by_prefixed("branch__", fid) | Q(accruals__partner_id=fid)


class PaymentViewSet(CancelOnlyMixin, FranchiseeScopedMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                     mixins.CreateModelMixin, viewsets.GenericViewSet):
    """payments/: оплаты абонплаты. Ручной ввод и импорт выписки — только финансист (§10.2)."""

    resource = "subscription_payments"
    franchisee_lookup = "branch__responsible_partner_id"
    region_field = "branch__region"
    partner_scope = staticmethod(_payment_scope)
    queryset = Payment.all_objects.select_related("branch", "client", "product")
    serializer_class = PaymentSerializer
    filterset_class = build_filterset(
        Payment, exact=("source", "status"), ids=("branch", "subscription", "client", "product"),
        date_from=df.DateFilter(field_name="paid_at__date", lookup_expr="gte"),
        date_to=df.DateFilter(field_name="paid_at__date", lookup_expr="lte"),
        reconciled=flag(lambda qs, n, v: qs.filter(reconciled_at__isnull=not v)))
    search_fields = ("external_id", "client__business_name", "elqr_account")
    ordering_fields = ("id", "paid_at", "amount")

    def create(self, request, *args, **kwargs):
        ser = PaymentCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        payment, warning = payment_svc.register_payment(
            branch=d["branch"], subscription=d.get("subscription"), amount=d["amount"], paid_at=d["paid_at"],
            source=d["source"], external_id=d.get("external_id", ""), months=d.get("months", 1),
            period_from=d.get("period_from"), period_to=d.get("period_to"), purpose=d.get("purpose", ""),
            user=request.user, raw={"entered_by": request.user.email})
        data = PaymentSerializer(payment, context=self.get_serializer_context()).data
        data["warning"] = warning
        return Response(data, status=201)

    def cancel_service(self, payment, user, reason):
        return payment_svc.cancel_payment(payment, user, reason)

    @action(detail=True, methods=["post"])
    def refund(self, request, pk=None):
        from clients.services import refund_sale
        payment = self.get_object()
        if payment.sale_id is None or payment.cancelled_at is not None:
            raise BusinessError(gettext("По этой оплате возврат невозможен."))
        from decimal import Decimal, InvalidOperation
        try:
            amount = Decimal(str(request.data.get("amount")))
        except (InvalidOperation, TypeError):
            raise BusinessError(gettext("Укажите сумму возврата."))
        refund_sale(payment.sale, amount, external_payment_id=str(request.data.get("external_id") or "") or None)
        payment.refresh_from_db()
        return Response(self.get_serializer(payment).data)

    def _csv_text(self, request):
        f = request.FILES.get("file")
        if f is not None:
            return f.read().decode("utf-8-sig", errors="replace"), f.name
        if request.data.get("csv"):
            return str(request.data["csv"]), "inline.csv"
        raise BusinessError(gettext("Передайте CSV файлом (file) или текстом (csv)."))

    @action(detail=False, methods=["post"], url_path="import/preview")
    def import_preview(self, request):
        text, _ = self._csv_text(request)
        return Response(payment_svc.import_preview(text))

    @action(detail=False, methods=["post"], url_path="import")
    def import_confirm(self, request):
        text, name = self._csv_text(request)
        return Response(payment_svc.import_confirm(text, request.user, name))


class AccrualViewSet(FranchiseeScopedMixin, viewsets.ReadOnlyModelViewSet):
    """commission-accruals/: начисления комиссий; release/ и cancel/ — решение OZOK по удержанным (H2)."""

    resource = "commission_accruals"
    franchisee_lookup = "partner_id"
    queryset = CommissionAccrual.all_objects.select_related("partner", "branch", "client", "product", "payment")
    serializer_class = AccrualSerializer
    filterset_class = build_filterset(
        CommissionAccrual, exact=("status",), ids=("partner", "branch", "client", "product", "payout"),
        date_from=df.DateFilter(field_name="date", lookup_expr="gte"),
        date_to=df.DateFilter(field_name="date", lookup_expr="lte"))
    ordering_fields = ("id", "date", "amount")

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action == "list" and self.request.query_params.get("cancelled") != "all":
            qs = qs.filter(cancelled_at__isnull=True)
        return qs

    def _to_pay(self, qs, request):
        """to_pay = Σ owed по отфильтрованным начислениям − нераспределённый остаток старых выплат этих партнёров
        (та же формула, что у commission_due в карточке партнёра и дашборде: billing.services.commission_to_pay)."""
        from .services import unallocated_credit
        rows = qs.filter(status__in=("accrued", "in_payout"))
        owed = sum((a.owed for a in rows), Decimal("0"))
        credit = sum(unallocated_credit(set(qs.values_list("partner_id", flat=True))).values(), Decimal("0"))
        return owed - credit

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Начислено / удержано / выплачено / корректировки с причинами."""
        qs = self.filter_queryset(self.get_queryset()).filter(cancelled_at__isnull=True)
        zero = Decimal("0")
        agg = qs.aggregate(accrued=Sum("amount"), withheld=Sum("withheld_amount"))
        paid = sum((a.amount - a.withheld_amount for a in qs.filter(status="paid")), zero) + sum(
            (a.paid_amount for a in qs.exclude(status="paid")), zero)
        held = sum((a.amount for a in qs.filter(status="on_hold")), zero)
        corr = qs.filter(reversal_of__isnull=False)
        return Response({
            "accrued": money(agg["accrued"] or zero), "withheld": money(agg["withheld"] or zero),
            "paid": money(paid), "on_hold": money(held),
            "to_pay": money(self._to_pay(qs, request)),
            "corrections": [{"id": a.id, "amount": str(a.amount), "reason": a.reason, "date": a.date.isoformat()}
                            for a in corr]})

    @action(detail=True, methods=["post"])
    def release(self, request, pk=None):
        return Response(self.get_serializer(accrual_svc.release(self.get_object(), request.user)).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            raise BusinessError(gettext("Укажите причину отмены."))
        return Response(self.get_serializer(accrual_svc.cancel_hold(self.get_object(), request.user, reason)).data)


class PayoutViewSet(FranchiseeScopedMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin,
                    viewsets.GenericViewSet):
    """payouts/: выплата комиссий draft → verified → paid (verified_by ≠ paid_by)."""

    resource = "payouts"
    franchisee_lookup = "partner_id"
    queryset = Payout.all_objects.select_related("partner", "verified_by", "paid_by").prefetch_related("accruals")
    serializer_class = PayoutSerializer
    filterset_class = build_filterset(Payout, exact=("status",), ids=("partner",))
    ordering_fields = ("id", "period", "amount")

    def create(self, request, *args, **kwargs):
        from network.models import Franchisee
        partner = Franchisee.objects.filter(pk=request.data.get("partner")).first() \
            if str(request.data.get("partner") or "").isdigit() else None
        if partner is None:
            raise BusinessError(gettext("Укажите партнёра (partner)."))
        payout = payout_svc.create(partner, parse_month(request.data.get("period")), request.user,
                                   str(request.data.get("comment") or ""))
        return Response(self.get_serializer(payout).data, status=201)

    @action(detail=True, methods=["post"])
    def verify(self, request, pk=None):
        return Response(self.get_serializer(payout_svc.verify(self.get_object(), request.user)).data)

    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        from django.utils.dateparse import parse_date
        d = parse_date(str(request.data["date"])) if request.data.get("date") else None
        return Response(self.get_serializer(payout_svc.pay(
            self.get_object(), request.user, date=d, payment_ref=str(request.data.get("payment_ref") or ""))).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return Response(self.get_serializer(payout_svc.cancel(self.get_object(), request.user,
                                                              request.data.get("reason"))).data)


# ---- Касса -------------------------------------------------------------------------------------------------------
from rest_framework.views import APIView  # noqa: E402

from . import cash as cash_svc  # noqa: E402
from .models import CashAccount, CashCategory, CashTransaction, NbsExpense  # noqa: E402
from .serializers import (CashAccountSerializer, CashCategorySerializer, CashTransactionSerializer,  # noqa: E402
                          NbsExpenseSerializer)


class CashAccountViewSet(viewsets.ModelViewSet):
    resource = "cash_accounts"
    permission_classes = [RolePermission]
    queryset = CashAccount.objects.all()
    serializer_class = CashAccountSerializer
    http_method_names = ["get", "post", "put", "patch", "head", "options"]  # счета отключаются, не удаляются
    filterset_class = build_filterset(CashAccount, exact=("kind",), bools=("is_active",))


class CashCategoryViewSet(viewsets.ModelViewSet):
    resource = "cash_categories"
    permission_classes = [RolePermission]
    queryset = CashCategory.objects.all()
    serializer_class = CashCategorySerializer
    http_method_names = ["get", "post", "put", "patch", "head", "options"]
    filterset_class = build_filterset(CashCategory, exact=("direction",), bools=("is_active", "is_system",
                                                                                   "manual_allowed"))


def _reversed_filter(qs, name, value):
    ids = CashTransaction.objects.filter(reversal_of__isnull=False).values("reversal_of_id")
    return qs.filter(id__in=ids) if value else qs.exclude(id__in=ids)


class CashTransactionViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin,
                             viewsets.GenericViewSet):
    """cash-transactions/: единый журнал денег OZOK. Вручную — прочие поступления, расходы OZOK, переводы;
    остальное создаётся автоматически. Изменений и удаления нет: cancel/ создаёт сторнирующую проводку."""

    resource = "cash_transactions"
    permission_classes = [RolePermission]
    queryset = CashTransaction.objects.select_related("account", "transfer_account", "category", "partner")
    serializer_class = CashTransactionSerializer
    filterset_class = build_filterset(
        CashTransaction, exact=("direction", "source_type"), ids=("account", "category", "partner", "client"),
        bools=("is_auto",), date_field="date", reversed=flag(_reversed_filter))
    search_fields = ("comment",)
    ordering_fields = ("id", "date", "amount")

    def create(self, request, *args, **kwargs):
        ser = CashTransactionSerializer(data=request.data, context=self.get_serializer_context())
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        tx = cash_svc.manual_entry(
            user=request.user, direction=d["direction"], amount=d["amount"], account=d["account"],
            date=d.get("date"), category_obj=d.get("category"), transfer_account=d.get("transfer_account"),
            partner=d.get("partner"), comment=d.get("comment", ""), attachment=d.get("attachment", ""))
        return Response(CashTransactionSerializer(tx, context=self.get_serializer_context()).data, status=201)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        tx = cash_svc.cancel_entry(self.get_object(), request.user, request.data.get("reason"))
        return Response(CashTransactionSerializer(tx, context=self.get_serializer_context()).data)


class NbsExpenseViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin,
                        viewsets.GenericViewSet):
    """nbs-expenses/: расход OZOK = ручная проводка out со статьёй и вложением."""

    resource = "cash_transactions"
    permission_classes = [RolePermission]
    queryset = NbsExpense.objects.select_related("transaction__account", "transaction__category")
    serializer_class = NbsExpenseSerializer
    ordering_fields = ("id",)

    def create(self, request, *args, **kwargs):
        d = request.data
        account = CashAccount.objects.filter(pk=d.get("account")).first() if str(d.get("account") or "").isdigit() \
            else None
        cat = CashCategory.objects.filter(pk=d.get("category")).first() if str(d.get("category") or "").isdigit() \
            else None
        if account is None or cat is None:
            raise BusinessError(gettext("Укажите счёт (account) и статью расхода (category)."))
        from django.utils.dateparse import parse_date
        exp = cash_svc.add_expense(
            user=request.user, amount=d.get("amount") or 0, account=account, category_obj=cat,
            date=parse_date(str(d["date"])) if d.get("date") else None, description=str(d.get("description") or ""),
            supplier=str(d.get("supplier") or ""), attachment=str(d.get("attachment") or ""))
        return Response(self.get_serializer(exp).data, status=201)


class CashReportView(APIView):
    """GET reports/cash/?date_from=&date_to=[&export=csv]: остатки по счетам, поступления/расходы по статьям и месяцам."""

    resource = "cash_report"
    permission_classes = [RolePermission]

    def get(self, request):
        from dashboard.analytics import parse_range
        d1, d2 = parse_range(request.query_params)
        from core.models import month_first, month_last
        d1, d2 = d1 or month_first(today()), d2 or month_last(today())
        data = cash_svc.cash_report(d1, d2)
        if request.query_params.get("export") == "csv":
            import csv
            import io

            from django.http import HttpResponse
            buf = io.StringIO()
            w = csv.writer(buf, delimiter=";")
            w.writerow([gettext("Счёт"), gettext("Остаток на начало"), gettext("Поступило"), gettext("Расход"), gettext("Остаток на конец")])
            for a in data["accounts"]:
                w.writerow([a["name"], a["opening"], a["in"], a["out"], a["closing"]])
            w.writerow([])
            w.writerow([gettext("Направление"), gettext("Статья"), gettext("Сумма")])
            for direction, key in ((gettext("Поступления"), "in_by_category"), (gettext("Расходы"), "out_by_category")):
                for r in data[key]:
                    w.writerow([direction, r["name"], r["amount"]])
            resp = HttpResponse("﻿" + buf.getvalue(), content_type="text/csv; charset=utf-8")
            resp["Content-Disposition"] = 'attachment; filename="cash-report.csv"'
            from core import audit
            audit.write("export", entity="reports.cash", user=request.user)
            return resp
        return Response(data)
