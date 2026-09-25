from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models.deletion import ProtectedError
from rest_framework import decorators, response, serializers, viewsets

from core.mixins import AuditModelViewSetMixin
from core.permissions import RolePermission
from .models import Expense, ExpenseCategory, Turnover, Wallet, WalletTransaction
from .serializers import (
    ExpenseCategorySerializer,
    ExpenseFilterSerializer,
    ExpenseSerializer,
    ReceiveTurnoverSerializer,
    TurnoverFilterSerializer,
    TurnoverSerializer,
    VoidExpenseSerializer,
    WalletFilterSerializer,
    WalletSerializer,
    WalletTransactionFilterSerializer,
    WalletTransactionSerializer,
    translate_model_validation,
)
from .services import SYSTEM_WALLET_KINDS, get_system_wallet


FINANCE_ROLES = ('administrator', 'finance', 'manager')
WALLET_READ_ROLES = FINANCE_ROLES + ('pharmacy',)


def validated_filters(serializer_class, params):
    # QueryDict is treated as HTML form input by DRF, which turns an omitted
    # BooleanField into False. A plain dict preserves the meaning of "not filtered".
    data = params.dict() if hasattr(params, 'dict') else params
    serializer = serializer_class(data=data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


class ProtectedDestroyMixin:
    destroy_error = 'This record is in use and cannot be deleted; deactivate it instead.'

    def perform_destroy(self, instance):
        try:
            return super().perform_destroy(instance)
        except DjangoValidationError as exc:
            raise translate_model_validation(exc) from exc
        except ProtectedError as exc:
            raise serializers.ValidationError({'detail': self.destroy_error}) from exc


class WalletViewSet(ProtectedDestroyMixin, AuditModelViewSetMixin, viewsets.ModelViewSet):
    queryset = Wallet.objects.all()
    serializer_class = WalletSerializer
    permission_classes = (RolePermission,)
    read_roles = WALLET_READ_ROLES
    write_roles = ('administrator',)
    search_fields = ('code', 'name')
    ordering_fields = ('code', 'name', 'balance', 'kind', 'created_at')

    def _ensure_visible_system_wallets(self):
        user = self.request.user
        if not user.is_authenticated:
            return

        filters = validated_filters(WalletFilterSerializer, self.request.query_params)
        requested_kind = filters.get('kind')
        if user.has_role(*FINANCE_ROLES):
            kinds = (
                (requested_kind,)
                if requested_kind in SYSTEM_WALLET_KINDS
                else (() if requested_kind else SYSTEM_WALLET_KINDS)
            )
        elif user.has_role('pharmacy'):
            kinds = (Wallet.Kind.PHARMACY,)
        else:
            kinds = ()

        for kind in kinds:
            get_system_wallet(kind)

    def get_queryset(self):
        self._ensure_visible_system_wallets()
        qs = super().get_queryset()
        user = self.request.user
        if user.is_authenticated and not user.is_superuser and user.has_role('pharmacy') and not user.has_role(*FINANCE_ROLES):
            qs = qs.filter(kind=Wallet.Kind.PHARMACY)
        filters = validated_filters(WalletFilterSerializer, self.request.query_params)
        if 'kind' in filters:
            qs = qs.filter(kind=filters['kind'])
        if 'is_active' in filters:
            qs = qs.filter(is_active=filters['is_active'])
        return qs


class WalletTransactionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = WalletTransaction.objects.select_related(
        'wallet', 'created_by', 'reverses', 'reversal_entry'
    )
    serializer_class = WalletTransactionSerializer
    permission_classes = (RolePermission,)
    read_roles = FINANCE_ROLES
    search_fields = ('reference', 'description', 'source_type', 'source_id')
    ordering_fields = ('transaction_date', 'amount', 'balance_after', 'created_at')

    def get_queryset(self):
        qs = super().get_queryset()
        filters = validated_filters(WalletTransactionFilterSerializer, self.request.query_params)
        if wallet := filters.get('wallet'):
            qs = qs.filter(wallet=wallet)
        if start := filters.get('start'):
            qs = qs.filter(transaction_date__date__gte=start)
        if end := filters.get('end'):
            qs = qs.filter(transaction_date__date__lte=end)
        for field in ('entry_type', 'category', 'source_type', 'source_id'):
            if value := filters.get(field):
                qs = qs.filter(**{field: value})
        return qs


class ExpenseCategoryViewSet(ProtectedDestroyMixin, AuditModelViewSetMixin, viewsets.ModelViewSet):
    queryset = ExpenseCategory.objects.all()
    serializer_class = ExpenseCategorySerializer
    permission_classes = (RolePermission,)
    read_roles = FINANCE_ROLES
    write_roles = ('administrator', 'finance')
    search_fields = ('name', 'description')
    ordering_fields = ('name', 'is_active', 'created_at')
    destroy_error = 'This category is used by an expense and cannot be deleted; deactivate it instead.'


class ExpenseViewSet(AuditModelViewSetMixin, viewsets.ModelViewSet):
    queryset = Expense.objects.select_related('category', 'wallet', 'created_by', 'voided_by')
    serializer_class = ExpenseSerializer
    permission_classes = (RolePermission,)
    read_roles = FINANCE_ROLES
    write_roles = FINANCE_ROLES
    action_roles = {'void': ('administrator', 'finance')}
    http_method_names = ('get', 'post', 'head', 'options')
    search_fields = ('purpose', 'payee', 'receipt_number', 'category__name', 'wallet__name')
    ordering_fields = ('expense_date', 'amount', 'created_at', 'voided_at')

    def get_serializer_class(self):
        return VoidExpenseSerializer if self.action == 'void' else ExpenseSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        filters = validated_filters(ExpenseFilterSerializer, self.request.query_params)
        if category := filters.get('category'):
            qs = qs.filter(category=category)
        if wallet := filters.get('wallet'):
            qs = qs.filter(wallet=wallet)
        if created_by := filters.get('created_by'):
            qs = qs.filter(created_by=created_by)
        if start := filters.get('start'):
            qs = qs.filter(expense_date__gte=start)
        if end := filters.get('end'):
            qs = qs.filter(expense_date__lte=end)
        if 'is_void' in filters:
            qs = qs.filter(is_void=filters['is_void'])
        return qs

    @decorators.action(detail=True, methods=('post',))
    def void(self, request, pk=None):
        request_serializer = self.get_serializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        reason = request_serializer.validated_data['reason']
        expense = self.get_object().void(user=request.user, reason=reason)
        self._audit('update', expense, {'is_void': {'from': 'False', 'to': 'True'}, 'reason': reason})
        return response.Response(ExpenseSerializer(expense, context=self.get_serializer_context()).data)


class TurnoverViewSet(AuditModelViewSetMixin, viewsets.ModelViewSet):
    queryset = Turnover.objects.select_related(
        'source_wallet', 'destination_wallet', 'handed_over_by', 'received_by'
    )
    serializer_class = TurnoverSerializer
    permission_classes = (RolePermission,)
    read_roles = FINANCE_ROLES
    write_roles = FINANCE_ROLES
    action_roles = {'receive': ('administrator', 'finance', 'manager')}
    http_method_names = ('get', 'post', 'head', 'options')
    search_fields = ('source_wallet__code', 'source_wallet__name', 'destination_wallet__code', 'destination_wallet__name')
    ordering_fields = ('created_at', 'period_start', 'period_end', 'amount', 'received_at')

    def get_serializer_class(self):
        return ReceiveTurnoverSerializer if self.action == 'receive' else TurnoverSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        filters = validated_filters(TurnoverFilterSerializer, self.request.query_params)
        if source := filters.get('source_wallet'):
            qs = qs.filter(source_wallet=source)
        if destination := filters.get('destination_wallet'):
            qs = qs.filter(destination_wallet=destination)
        if status_value := filters.get('status'):
            qs = qs.filter(status=status_value)
        if start := filters.get('start'):
            qs = qs.filter(period_start__date__gte=start)
        if end := filters.get('end'):
            qs = qs.filter(period_end__date__lte=end)
        return qs

    @decorators.action(detail=True, methods=('post',))
    def receive(self, request, pk=None):
        request_serializer = self.get_serializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        turnover = self.get_object().receive(user=request.user)
        self._audit(
            'update', turnover,
            {'status': {'from': Turnover.Status.HANDED_OVER, 'to': Turnover.Status.RECEIVED}},
        )
        return response.Response(TurnoverSerializer(turnover, context=self.get_serializer_context()).data)
