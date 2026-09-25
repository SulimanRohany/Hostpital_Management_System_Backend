from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import decorators, response, status, viewsets

from core.mixins import AuditModelViewSetMixin
from core.permissions import RolePermission
from .models import Medicine, MedicineBatch, MedicineCategory, Purchase, Sale, StockMovement, Supplier, SupplierPayment
from .serializers import (
    MedicineBatchSerializer, MedicineCategorySerializer, MedicineSerializer, PurchaseSerializer,
    SaleSerializer, StockAdjustmentSerializer, StockMovementSerializer, SupplierPaymentSerializer,
    SupplierSerializer, VoidTransactionSerializer,
)
from .services import change_stock
from finance.models import Wallet, WalletTransaction
from finance.services import get_system_wallet, post_wallet_entry


PHARMACY_ROLES = ('administrator', 'pharmacy', 'manager')


def paginated_response(view, queryset):
    page = view.paginate_queryset(queryset)
    if page is not None:
        return view.get_paginated_response(view.get_serializer(page, many=True).data)
    return response.Response(view.get_serializer(queryset, many=True).data)


def filter_date_range(queryset, params, field):
    start = parse_date(params.get('date_from', ''))
    end = parse_date(params.get('date_to', ''))
    if start:
        queryset = queryset.filter(**{f'{field}__date__gte' if field in ('sale_date', 'created_at') else f'{field}__gte': start})
    if end:
        queryset = queryset.filter(**{f'{field}__date__lte' if field in ('sale_date', 'created_at') else f'{field}__lte': end})
    return queryset


class MedicineCategoryViewSet(AuditModelViewSetMixin, viewsets.ModelViewSet):
    queryset = MedicineCategory.objects.all()
    serializer_class = MedicineCategorySerializer
    permission_classes = (RolePermission,)
    read_roles = PHARMACY_ROLES
    write_roles = ('administrator', 'pharmacy')
    search_fields = ('name',)


class SupplierViewSet(AuditModelViewSetMixin, viewsets.ModelViewSet):
    queryset = Supplier.objects.all()
    serializer_class = SupplierSerializer
    permission_classes = (RolePermission,)
    read_roles = ('administrator', 'pharmacy', 'finance', 'manager')
    write_roles = ('administrator', 'pharmacy')
    search_fields = ('name', 'contact_person', 'phone', 'email')
    ordering_fields = ('name', 'created_at')


class MedicineViewSet(AuditModelViewSetMixin, viewsets.ModelViewSet):
    queryset = Medicine.objects.select_related('category').prefetch_related('batches')
    serializer_class = MedicineSerializer
    permission_classes = (RolePermission,)
    read_roles = PHARMACY_ROLES
    write_roles = ('administrator', 'pharmacy')
    action_roles = {'inventory_summary': ('administrator', 'manager', 'finance')}
    search_fields = ('code', 'name', 'generic_name', 'strength', 'dosage_form')
    ordering_fields = ('code', 'name', 'default_sale_price', 'created_at')

    @decorators.action(detail=False, methods=('get',), url_path='low-stock')
    def low_stock(self, request):
        today = timezone.localdate()
        decimal_output = DecimalField(max_digits=14, decimal_places=3)
        medicines = self.filter_queryset(self.get_queryset()).annotate(
            usable_stock_total=Coalesce(
                Sum('batches__quantity_available', filter=Q(
                    batches__is_active=True, batches__expiry_date__gt=today,
                    batches__quantity_available__gt=0,
                )), Value(Decimal('0.000')), output_field=decimal_output,
            )
        ).filter(usable_stock_total__lte=F('reorder_level')).order_by('name', 'strength')
        return paginated_response(self, medicines)

    @decorators.action(detail=False, methods=('get',), url_path='inventory-summary')
    def inventory_summary(self, request):
        today = timezone.localdate()
        batches = MedicineBatch.objects.filter(quantity_available__gt=0)
        usable = batches.filter(is_active=True, expiry_date__gt=today)
        expired = batches.filter(expiry_date__lte=today)
        return response.Response({
            'medicine_count': self.get_queryset().filter(is_active=True).count(),
            'usable_quantity': usable.aggregate(value=Sum('quantity_available'))['value'] or Decimal('0'),
            'expired_quantity': expired.aggregate(value=Sum('quantity_available'))['value'] or Decimal('0'),
            'inventory_cost_value': usable.aggregate(value=Sum(F('quantity_available') * F('purchase_price')))['value'] or Decimal('0'),
            'inventory_retail_value': usable.aggregate(value=Sum(F('quantity_available') * F('sale_price')))['value'] or Decimal('0'),
        })


class MedicineBatchViewSet(AuditModelViewSetMixin, viewsets.ModelViewSet):
    queryset = MedicineBatch.objects.select_related('medicine', 'supplier')
    serializer_class = MedicineBatchSerializer
    permission_classes = (RolePermission,)
    read_roles = PHARMACY_ROLES
    write_roles = ('administrator', 'pharmacy')
    http_method_names = ('get', 'patch', 'head', 'options')
    search_fields = ('batch_number', 'medicine__name', 'medicine__code', 'supplier__name')
    ordering_fields = ('expiry_date', 'quantity_available', 'created_at')

    def get_queryset(self):
        qs = super().get_queryset()
        if medicine := self.request.query_params.get('medicine'):
            qs = qs.filter(medicine_id=medicine)
        if self.request.query_params.get('in_stock') == 'true':
            qs = qs.filter(quantity_available__gt=0)
        if supplier := self.request.query_params.get('supplier'):
            qs = qs.filter(supplier_id=supplier)
        if active := self.request.query_params.get('active'):
            qs = qs.filter(is_active=active.lower() == 'true')
        expired = self.request.query_params.get('expired')
        if expired == 'true':
            qs = qs.filter(expiry_date__lte=timezone.localdate())
        elif expired == 'false':
            qs = qs.filter(expiry_date__gt=timezone.localdate())
        return qs

    @decorators.action(detail=False, methods=('get',), url_path='near-expiry')
    def near_expiry(self, request):
        try:
            days = min(max(int(request.query_params.get('days', 90)), 1), 365)
        except ValueError:
            return response.Response({'days': 'Days must be an integer.'}, status=status.HTTP_400_BAD_REQUEST)
        end = timezone.localdate() + timedelta(days=days)
        qs = self.filter_queryset(self.get_queryset()).filter(
            expiry_date__gte=timezone.localdate(), expiry_date__lte=end, quantity_available__gt=0,
        )
        return paginated_response(self, qs)

    @decorators.action(detail=False, methods=('get',), url_path='sellable')
    def sellable(self, request):
        qs = self.filter_queryset(self.get_queryset()).filter(
            is_active=True, expiry_date__gt=timezone.localdate(), quantity_available__gt=0,
            medicine__is_active=True,
        ).order_by('expiry_date', 'created_at')
        return paginated_response(self, qs)


class PurchaseViewSet(AuditModelViewSetMixin, viewsets.ModelViewSet):
    queryset = Purchase.objects.select_related('supplier', 'created_by').prefetch_related('lines__medicine', 'lines__batch')
    serializer_class = PurchaseSerializer
    permission_classes = (RolePermission,)
    read_roles = ('administrator', 'pharmacy', 'finance', 'manager')
    write_roles = ('administrator', 'pharmacy')
    action_roles = {'void': ('administrator',)}
    http_method_names = ('get', 'post', 'head', 'options')
    search_fields = ('purchase_number', 'invoice_number', 'supplier__name')
    ordering_fields = ('purchase_date', 'total_amount', 'paid_amount', 'created_at')

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if supplier := params.get('supplier'):
            qs = qs.filter(supplier_id=supplier)
        if purchase_status := params.get('status'):
            qs = qs.filter(status=purchase_status)
        if params.get('outstanding') == 'true':
            money = DecimalField(max_digits=14, decimal_places=2)
            qs = qs.filter(status=Purchase.Status.POSTED).annotate(
                later_paid=Coalesce(
                    Sum('supplier_payments__amount', filter=Q(supplier_payments__is_void=False)),
                    Value(Decimal('0.00')), output_field=money,
                ),
            ).annotate(
                outstanding_amount=ExpressionWrapper(
                    F('total_amount') - F('paid_amount') - F('later_paid'), output_field=money,
                )
            ).filter(outstanding_amount__gt=0)
        return filter_date_range(qs, params, 'purchase_date')

    @decorators.action(detail=True, methods=('post',))
    @transaction.atomic
    def void(self, request, pk=None):
        purchase = Purchase.objects.select_for_update().get(pk=self.get_object().pk)
        if purchase.status == Purchase.Status.VOID:
            return response.Response({'detail': 'Purchase is already void.'}, status=status.HTTP_400_BAD_REQUEST)
        reason_serializer = VoidTransactionSerializer(data=request.data)
        reason_serializer.is_valid(raise_exception=True)
        reason = reason_serializer.validated_data['reason']
        lines = list(purchase.lines.select_related('batch').order_by('batch_id'))
        if purchase.supplier_payments.filter(is_void=False).exists():
            return response.Response(
                {'detail': 'Void supplier payments allocated to this purchase before voiding the purchase.'},
                status=status.HTTP_409_CONFLICT,
            )
        locked_batches = {
            batch.pk: batch for batch in MedicineBatch.objects.select_for_update()
            .filter(pk__in=[line.batch_id for line in lines]).order_by('pk')
        }
        if any(locked_batches[line.batch_id].quantity_available != line.quantity for line in lines):
            return response.Response(
                {'detail': 'Purchase cannot be voided after any quantity from its batches has moved.'},
                status=status.HTTP_409_CONFLICT,
            )
        for line in lines:
            change_stock(
                batch=locked_batches[line.batch_id], quantity_change=-line.quantity, movement_type=StockMovement.MovementType.VOID,
                reference=f'purchase:{purchase.pk}:void:{line.pk}', reason=reason, user=request.user,
            )
        if purchase.paid_amount:
            original_payment = WalletTransaction.objects.get(reference=f'purchase:{purchase.pk}:payment')
            post_wallet_entry(
                wallet=get_system_wallet(Wallet.Kind.PHARMACY), entry_type=WalletTransaction.EntryType.CREDIT,
                amount=purchase.paid_amount, category='purchase_reversal',
                description=f'Void purchase {purchase.purchase_number}: {reason[:140]}',
                reference=f'purchase:{purchase.pk}:void-payment', user=request.user, source=purchase,
                reversal_of=original_payment,
            )
        purchase.status = Purchase.Status.VOID
        purchase.voided_by = request.user
        purchase.void_reason = reason
        purchase.save(update_fields=('status', 'voided_by', 'void_reason', 'updated_at'))
        self._audit('update', purchase, {'status': {'from': 'posted', 'to': 'void'}, 'reason': reason})
        return response.Response(self.get_serializer(purchase).data)


class SaleViewSet(AuditModelViewSetMixin, viewsets.ModelViewSet):
    queryset = Sale.objects.select_related('patient', 'created_by').prefetch_related('lines__batch__medicine')
    serializer_class = SaleSerializer
    permission_classes = (RolePermission,)
    read_roles = ('administrator', 'pharmacy', 'finance', 'manager')
    write_roles = ('administrator', 'pharmacy')
    action_roles = {
        'void': ('administrator',),
        'summary': ('administrator', 'manager', 'finance'),
    }
    http_method_names = ('get', 'post', 'head', 'options')
    search_fields = ('sale_number', 'patient__medical_record_number', 'patient__first_name', 'patient__last_name')
    ordering_fields = ('sale_date', 'total_amount', 'paid_amount', 'created_at')

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if patient := params.get('patient'):
            qs = qs.filter(patient_id=patient)
        if sale_status := params.get('status'):
            qs = qs.filter(status=sale_status)
        if params.get('prescription') == 'true':
            qs = qs.exclude(prescription_reference='')
        return filter_date_range(qs, params, 'sale_date')

    @decorators.action(detail=False, methods=('post',), url_path='fefo-allocation')
    def fefo_allocation(self, request):
        medicine_id = request.data.get('medicine')
        try:
            quantity = Decimal(str(request.data.get('quantity', '0')))
        except (InvalidOperation, TypeError):
            quantity = Decimal('0')
        if not medicine_id or quantity <= 0:
            return response.Response({'detail': 'Medicine and a positive quantity are required.'}, status=status.HTTP_400_BAD_REQUEST)
        remaining = quantity
        allocations = []
        batches = MedicineBatch.objects.filter(
            medicine_id=medicine_id, medicine__is_active=True, is_active=True,
            expiry_date__gt=timezone.localdate(), quantity_available__gt=0,
        ).order_by('expiry_date', 'created_at')
        for batch in batches:
            allocated = min(remaining, batch.quantity_available)
            allocations.append({
                'batch': str(batch.pk), 'batch_number': batch.batch_number,
                'expiry_date': batch.expiry_date, 'quantity': allocated, 'unit_price': batch.sale_price,
            })
            remaining -= allocated
            if remaining == 0:
                break
        if remaining > 0:
            return response.Response({
                'detail': 'Insufficient usable stock.', 'requested': quantity,
                'available': quantity - remaining, 'allocations': allocations,
            }, status=status.HTTP_409_CONFLICT)
        return response.Response({'requested': quantity, 'allocations': allocations})

    @decorators.action(detail=False, methods=('get',), url_path='summary')
    def summary(self, request):
        qs = self.get_queryset().filter(status=Sale.Status.POSTED)
        totals = qs.aggregate(
            sales_count=Count('id'), revenue=Sum('total_amount'), discount=Sum('discount_amount'),
        )
        profit = sum((sale.profit for sale in qs.prefetch_related('lines')), Decimal('0'))
        return response.Response({
            'sales_count': totals['sales_count'] or 0,
            'revenue': totals['revenue'] or Decimal('0'),
            'discount': totals['discount'] or Decimal('0'),
            'gross_profit': profit,
        })

    @decorators.action(detail=True, methods=('post',))
    @transaction.atomic
    def void(self, request, pk=None):
        sale = Sale.objects.select_for_update().get(pk=self.get_object().pk)
        if sale.status == Sale.Status.VOID:
            return response.Response({'detail': 'Sale is already void.'}, status=status.HTTP_400_BAD_REQUEST)
        reason_serializer = VoidTransactionSerializer(data=request.data)
        reason_serializer.is_valid(raise_exception=True)
        reason = reason_serializer.validated_data['reason']
        lines = list(sale.lines.select_related('batch').order_by('batch_id'))
        locked_batches = {
            batch.pk: batch for batch in MedicineBatch.objects.select_for_update()
            .filter(pk__in=[line.batch_id for line in lines]).order_by('pk')
        }
        if any(
            locked_batches[line.batch_id].quantity_available + line.quantity > locked_batches[line.batch_id].quantity_received
            for line in lines
        ):
            return response.Response(
                {'detail': 'Sale cannot be voided because restoring its stock would exceed a batch received quantity.'},
                status=status.HTTP_409_CONFLICT,
            )
        for line in lines:
            change_stock(
                batch=locked_batches[line.batch_id], quantity_change=line.quantity, movement_type=StockMovement.MovementType.VOID,
                reference=f'sale:{sale.pk}:void:{line.pk}', reason=reason, user=request.user,
            )
        if sale.paid_amount:
            original_payment = WalletTransaction.objects.get(reference=f'sale:{sale.pk}:payment')
            post_wallet_entry(
                wallet=get_system_wallet(Wallet.Kind.PHARMACY), entry_type=WalletTransaction.EntryType.DEBIT,
                amount=sale.paid_amount, category='sale_reversal',
                description=f'Void sale {sale.sale_number}: {reason[:150]}',
                reference=f'sale:{sale.pk}:void-payment', user=request.user, source=sale,
                reversal_of=original_payment,
            )
        sale.status = Sale.Status.VOID
        sale.voided_by = request.user
        sale.void_reason = reason
        sale.save(update_fields=('status', 'voided_by', 'void_reason', 'updated_at'))
        self._audit('update', sale, {'status': {'from': 'posted', 'to': 'void'}, 'reason': reason})
        return response.Response(self.get_serializer(sale).data)


class StockMovementViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = StockMovement.objects.select_related('batch__medicine', 'created_by')
    serializer_class = StockMovementSerializer
    permission_classes = (RolePermission,)
    read_roles = PHARMACY_ROLES
    write_roles = ('administrator', 'pharmacy')
    action_roles = {'adjust': ('administrator',)}
    search_fields = ('reference', 'reason', 'batch__batch_number', 'batch__medicine__name')
    ordering_fields = ('created_at', 'quantity_change')

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if batch := params.get('batch'):
            qs = qs.filter(batch_id=batch)
        if medicine := params.get('medicine'):
            qs = qs.filter(batch__medicine_id=medicine)
        if movement_type := params.get('movement_type'):
            qs = qs.filter(movement_type=movement_type)
        return filter_date_range(qs, params, 'created_at')

    @decorators.action(detail=False, methods=('post',), permission_classes=(RolePermission,))
    def adjust(self, request):
        serializer = StockAdjustmentSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        movement = serializer.save()
        return response.Response(self.get_serializer(movement).data, status=status.HTTP_201_CREATED)


class SupplierPaymentViewSet(AuditModelViewSetMixin, viewsets.ModelViewSet):
    queryset = SupplierPayment.objects.select_related('supplier', 'purchase', 'wallet', 'created_by')
    serializer_class = SupplierPaymentSerializer
    permission_classes = (RolePermission,)
    read_roles = ('administrator', 'pharmacy', 'finance', 'manager')
    write_roles = ('administrator', 'pharmacy', 'finance')
    action_roles = {'void': ('administrator', 'finance')}
    http_method_names = ('get', 'post', 'head', 'options')
    search_fields = ('supplier__name', 'reference_number', 'purchase__purchase_number')
    ordering_fields = ('payment_date', 'amount', 'created_at')

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if supplier := params.get('supplier'):
            qs = qs.filter(supplier_id=supplier)
        if purchase := params.get('purchase'):
            qs = qs.filter(purchase_id=purchase)
        if wallet := params.get('wallet'):
            qs = qs.filter(wallet_id=wallet)
        if is_void := params.get('is_void'):
            qs = qs.filter(is_void=is_void.lower() == 'true')
        return filter_date_range(qs, params, 'payment_date')

    @decorators.action(detail=True, methods=('post',))
    @transaction.atomic
    def void(self, request, pk=None):
        payment = SupplierPayment.objects.select_for_update().get(pk=self.get_object().pk)
        if payment.is_void:
            return response.Response({'detail': 'Payment is already void.'}, status=status.HTTP_400_BAD_REQUEST)
        reason_serializer = VoidTransactionSerializer(data=request.data)
        reason_serializer.is_valid(raise_exception=True)
        reason = reason_serializer.validated_data['reason']
        original_payment = WalletTransaction.objects.get(reference=f'supplier-payment:{payment.pk}')
        post_wallet_entry(
            wallet=payment.wallet, entry_type=WalletTransaction.EntryType.CREDIT, amount=payment.amount,
            category='supplier_payment_reversal', description=f'Void supplier payment: {reason[:160]}',
            reference=f'supplier-payment:{payment.pk}:void', user=request.user, source=payment,
            reversal_of=original_payment,
        )
        payment.is_void = True
        payment.voided_by = request.user
        payment.void_reason = reason
        payment.save(update_fields=('is_void', 'voided_by', 'void_reason', 'updated_at'))
        self._audit('update', payment, {'is_void': {'from': 'False', 'to': 'True'}, 'reason': reason})
        return response.Response(self.get_serializer(payment).data)
