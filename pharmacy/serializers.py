import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import serializers

from core.mixins import ImmutableTransactionMixin
from finance.models import Wallet, WalletTransaction
from finance.services import get_system_wallet, post_wallet_entry
from .models import (
    Medicine, MedicineBatch, MedicineCategory, Purchase, PurchaseLine, Sale, SaleLine,
    StockMovement, Supplier, SupplierPayment,
)
from .services import change_stock


class MedicineCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = MedicineCategory
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')


class SupplierSerializer(serializers.ModelSerializer):
    amount_due = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = Supplier
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')


class MedicineSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    stock_quantity = serializers.SerializerMethodField()

    class Meta:
        model = Medicine
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')

    def validate_category(self, value):
        if not value.is_active:
            raise serializers.ValidationError('Inactive categories cannot be assigned to medicines.')
        return value

    def get_stock_quantity(self, obj):
        if hasattr(obj, 'usable_stock_total'):
            value = obj.usable_stock_total
        else:
            value = obj.stock_quantity
        return f'{value:.3f}'


class MedicineBatchSerializer(serializers.ModelSerializer):
    medicine_name = serializers.CharField(source='medicine.name', read_only=True)
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)

    class Meta:
        model = MedicineBatch
        fields = '__all__'
        read_only_fields = ('id', 'quantity_received', 'quantity_available', 'created_at', 'updated_at')

    def validate(self, attrs):
        instance = self.instance
        purchase_price = instance.purchase_price if instance else attrs.get('purchase_price')
        sale_price = attrs.get('sale_price', instance.sale_price if instance else None)
        expiry_date = attrs.get('expiry_date', instance.expiry_date if instance else None)
        is_active = attrs.get('is_active', instance.is_active if instance else True)
        if sale_price is not None and purchase_price is not None and sale_price < purchase_price:
            raise serializers.ValidationError({'sale_price': 'Sale price cannot be below purchase cost.'})
        if expiry_date and expiry_date <= timezone.localdate() and is_active:
            raise serializers.ValidationError({'is_active': 'Expired batches cannot be active.'})
        if instance and instance.quantity_available > 0 and attrs.get('is_active') is False:
            raise serializers.ValidationError({'is_active': 'A batch with stock cannot be deactivated; adjust or dispose of its stock first.'})
        return attrs


class PurchaseLineSerializer(serializers.Serializer):
    medicine = serializers.PrimaryKeyRelatedField(queryset=Medicine.objects.filter(is_active=True))
    batch_number = serializers.CharField(max_length=80)
    expiry_date = serializers.DateField()
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal('0.001'))
    unit_cost = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0'))
    sale_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0'))
    line_total = serializers.SerializerMethodField(read_only=True)

    def validate_batch_number(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Batch number cannot be blank.')
        return value

    def get_line_total(self, obj):
        quantity = obj.quantity if isinstance(obj, PurchaseLine) else obj.get('quantity', 0)
        unit_cost = obj.unit_cost if isinstance(obj, PurchaseLine) else obj.get('unit_cost', 0)
        return quantity * unit_cost

    def to_representation(self, instance):
        if isinstance(instance, PurchaseLine):
            return {
                'id': str(instance.id),
                'medicine': str(instance.medicine_id),
                'medicine_name': str(instance.medicine),
                'batch': str(instance.batch_id),
                'batch_number': instance.batch.batch_number,
                'expiry_date': instance.batch.expiry_date,
                'quantity': instance.quantity,
                'unit_cost': instance.unit_cost,
                'sale_price': instance.batch.sale_price,
                'line_total': instance.line_total,
            }
        return super().to_representation(instance)


class PurchaseSerializer(ImmutableTransactionMixin, serializers.ModelSerializer):
    lines = PurchaseLineSerializer(many=True)
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    amount_due = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = Purchase
        fields = '__all__'
        read_only_fields = (
            'id', 'purchase_number', 'total_amount', 'status', 'created_by',
            'voided_by', 'void_reason', 'created_at', 'updated_at',
        )

    def validate(self, attrs):
        lines = attrs.get('lines', [])
        if not lines:
            raise serializers.ValidationError({'lines': 'At least one purchase line is required.'})
        if not attrs['supplier'].is_active:
            raise serializers.ValidationError({'supplier': 'Inactive suppliers cannot be used.'})
        if attrs['purchase_date'] > timezone.localdate():
            raise serializers.ValidationError({'purchase_date': 'Purchase date cannot be in the future.'})
        attrs['invoice_number'] = attrs['invoice_number'].strip()
        if not attrs['invoice_number']:
            raise serializers.ValidationError({'invoice_number': 'Invoice number cannot be blank.'})
        if Purchase.objects.filter(
            supplier=attrs['supplier'], invoice_number__iexact=attrs['invoice_number'],
        ).exists():
            raise serializers.ValidationError({'invoice_number': 'This supplier invoice already exists.'})
        keys = [(line['medicine'].pk, line['batch_number'].casefold()) for line in lines]
        if len(keys) != len(set(keys)):
            raise serializers.ValidationError({'lines': 'Medicine and batch combinations must be unique.'})
        conflicts = [
            line['batch_number'] for line in lines
            if MedicineBatch.objects.filter(
                medicine=line['medicine'], batch_number__iexact=line['batch_number'],
            ).exists()
        ]
        if conflicts:
            raise serializers.ValidationError({'lines': f'Batch already exists for this medicine: {conflicts[0]}.'})
        if any(line['expiry_date'] <= attrs['purchase_date'] for line in lines):
            raise serializers.ValidationError({'lines': 'Expiry date must be after the purchase date.'})
        if any(line['sale_price'] < line['unit_cost'] for line in lines):
            raise serializers.ValidationError({'lines': 'Sale price cannot be below purchase cost.'})
        total = sum((line['quantity'] * line['unit_cost'] for line in lines), Decimal('0'))
        if attrs.get('paid_amount', 0) > total:
            raise serializers.ValidationError({'paid_amount': 'Paid amount cannot exceed purchase total.'})
        attrs['_total'] = total
        return attrs

    def create(self, validated_data):
        try:
            with transaction.atomic():
                return self._create_purchase(validated_data)
        except IntegrityError as exc:
            raise serializers.ValidationError({
                'detail': 'The supplier invoice or one of its medicine batches already exists.'
            }) from exc

    def _create_purchase(self, validated_data):
        lines = validated_data.pop('lines')
        total = validated_data.pop('_total')
        user = self.context['request'].user
        purchase = Purchase.objects.create(total_amount=total, created_by=user, **validated_data)
        for index, line in enumerate(lines):
            batch = MedicineBatch.objects.create(
                medicine=line['medicine'], supplier=purchase.supplier, batch_number=line['batch_number'],
                expiry_date=line['expiry_date'], purchase_price=line['unit_cost'], sale_price=line['sale_price'],
                quantity_received=line['quantity'], quantity_available=Decimal('0'),
            )
            PurchaseLine.objects.create(
                purchase=purchase, medicine=line['medicine'], batch=batch,
                quantity=line['quantity'], unit_cost=line['unit_cost'],
            )
            change_stock(
                batch=batch, quantity_change=line['quantity'], movement_type=StockMovement.MovementType.PURCHASE,
                reference=f'purchase:{purchase.pk}:{index}', reason=f'Purchase {purchase.purchase_number}', user=user,
            )
        if purchase.paid_amount:
            wallet = get_system_wallet(Wallet.Kind.PHARMACY)
            post_wallet_entry(
                wallet=wallet, entry_type=WalletTransaction.EntryType.DEBIT, amount=purchase.paid_amount,
                category='pharmacy_purchase', description=f'Purchase {purchase.purchase_number}',
                reference=f'purchase:{purchase.pk}:payment', user=user, source=purchase,
            )
        return purchase

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['lines'] = [
            {
                'id': str(line.id), 'medicine': str(line.medicine_id), 'medicine_name': str(line.medicine),
                'batch': str(line.batch_id), 'batch_number': line.batch.batch_number,
                'expiry_date': line.batch.expiry_date, 'quantity': line.quantity,
                'unit_cost': line.unit_cost, 'sale_price': line.batch.sale_price, 'line_total': line.line_total,
            } for line in instance.lines.select_related('medicine', 'batch')
        ]
        return data


class SaleLineSerializer(serializers.Serializer):
    batch = serializers.PrimaryKeyRelatedField(queryset=MedicineBatch.objects.filter(is_active=True))
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal('0.001'))
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0'))


class SaleSerializer(ImmutableTransactionMixin, serializers.ModelSerializer):
    lines = SaleLineSerializer(many=True)
    patient_name = serializers.CharField(source='patient.full_name', read_only=True)
    profit = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = Sale
        fields = '__all__'
        read_only_fields = (
            'id', 'sale_number', 'subtotal', 'total_amount', 'status', 'created_by',
            'voided_by', 'void_reason', 'created_at', 'updated_at',
        )

    def validate(self, attrs):
        lines = attrs.get('lines', [])
        if not lines:
            raise serializers.ValidationError({'lines': 'At least one sale line is required.'})
        if attrs['sale_date'] > timezone.now() + timedelta(minutes=5):
            raise serializers.ValidationError({'sale_date': 'Sale date cannot be in the future.'})
        ids = [line['batch'].pk for line in lines]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError({'lines': 'The same batch cannot be added twice.'})
        today = timezone.localdate()
        request = self.context.get('request')
        can_override_price = bool(request and (request.user.is_superuser or request.user.has_role('administrator')))
        for line in lines:
            if not line['batch'].is_active:
                raise serializers.ValidationError({'lines': f'Batch {line["batch"].batch_number} is inactive.'})
            if line['batch'].expiry_date <= today:
                raise serializers.ValidationError({'lines': f'Batch {line["batch"].batch_number} is expired.'})
            if line['quantity'] > line['batch'].quantity_available:
                raise serializers.ValidationError({'lines': f'Insufficient stock in batch {line["batch"].batch_number}.'})
            if line['unit_price'] < line['batch'].purchase_price:
                raise serializers.ValidationError({'lines': f'Sale price cannot be below cost for batch {line["batch"].batch_number}.'})
            if line['unit_price'] != line['batch'].sale_price and not can_override_price:
                raise serializers.ValidationError({
                    'lines': f'Only an administrator may override the configured price for batch {line["batch"].batch_number}.'
                })
            if not line['batch'].medicine.is_active:
                raise serializers.ValidationError({'lines': f'Medicine {line["batch"].medicine} is inactive.'})
        patient = attrs.get('patient')
        if patient and not patient.is_active:
            raise serializers.ValidationError({'patient': 'Inactive patients cannot be assigned to sales.'})
        if 'prescription_reference' in attrs:
            attrs['prescription_reference'] = attrs['prescription_reference'].strip()
        if any(line['batch'].medicine.requires_prescription for line in lines):
            if not attrs.get('patient'):
                raise serializers.ValidationError({'patient': 'A patient is required for prescription medicine sales.'})
            if not attrs.get('prescription_reference', '').strip():
                raise serializers.ValidationError({'prescription_reference': 'A prescription reference is required.'})
        subtotal = sum((line['quantity'] * line['unit_price'] for line in lines), Decimal('0'))
        discount = attrs.get('discount_amount', Decimal('0'))
        if discount > subtotal:
            raise serializers.ValidationError({'discount_amount': 'Discount cannot exceed subtotal.'})
        if discount and not attrs.get('discount_reason', '').strip():
            raise serializers.ValidationError({'discount_reason': 'A reason is required for a discount.'})
        total = subtotal - discount
        if attrs.get('paid_amount', Decimal('0')) != total:
            raise serializers.ValidationError({'paid_amount': 'Pharmacy sales must be paid in full; credit sales require a receivables workflow.'})
        attrs['_subtotal'] = subtotal
        attrs['_total'] = total
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        lines = validated_data.pop('lines')
        subtotal = validated_data.pop('_subtotal')
        total = validated_data.pop('_total')
        user = self.context['request'].user
        requested = {line['batch'].pk: line for line in lines}
        locked_batches = {
            batch.pk: batch for batch in MedicineBatch.objects.select_for_update()
            .select_related('medicine').filter(pk__in=requested).order_by('pk')
        }
        today = timezone.localdate()
        can_override_price = user.is_superuser or user.has_role('administrator')
        for batch_id, line in requested.items():
            batch = locked_batches.get(batch_id)
            if not batch or not batch.is_active or not batch.medicine.is_active:
                raise serializers.ValidationError({'lines': 'A selected batch or medicine is no longer active.'})
            if batch.expiry_date <= today:
                raise serializers.ValidationError({'lines': f'Batch {batch.batch_number} is expired.'})
            if line['quantity'] > batch.quantity_available:
                raise serializers.ValidationError({'lines': f'Insufficient stock in batch {batch.batch_number}.'})
            if line['unit_price'] < batch.purchase_price:
                raise serializers.ValidationError({'lines': f'Sale price cannot be below cost for batch {batch.batch_number}.'})
            if line['unit_price'] != batch.sale_price and not can_override_price:
                raise serializers.ValidationError({'lines': f'Price override is not allowed for batch {batch.batch_number}.'})
        sale = Sale.objects.create(subtotal=subtotal, total_amount=total, created_by=user, **validated_data)
        for index, line in enumerate(lines):
            batch = locked_batches[line['batch'].pk]
            sale_line = SaleLine.objects.create(
                sale=sale, batch=batch, quantity=line['quantity'], unit_price=line['unit_price'], unit_cost=batch.purchase_price,
            )
            change_stock(
                batch=batch, quantity_change=-line['quantity'], movement_type=StockMovement.MovementType.SALE,
                reference=f'sale:{sale.pk}:{index}', reason=f'Sale {sale.sale_number}', user=user,
            )
        if sale.paid_amount:
            post_wallet_entry(
                wallet=get_system_wallet(Wallet.Kind.PHARMACY), entry_type=WalletTransaction.EntryType.CREDIT,
                amount=sale.paid_amount, category='pharmacy_sale', description=f'Sale {sale.sale_number}',
                reference=f'sale:{sale.pk}:payment', user=user, source=sale, transaction_date=sale.sale_date,
            )
        return sale

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['lines'] = [
            {
                'id': str(line.id), 'batch': str(line.batch_id), 'batch_number': line.batch.batch_number,
                'medicine': str(line.batch.medicine_id), 'medicine_name': str(line.batch.medicine),
                'quantity': line.quantity, 'unit_price': line.unit_price,
                'unit_cost': line.unit_cost, 'line_total': line.line_total,
            } for line in instance.lines.select_related('batch__medicine')
        ]
        request = self.context.get('request')
        may_view_cost = bool(request and (request.user.is_superuser or request.user.has_role('administrator', 'manager', 'finance')))
        if not may_view_cost:
            data.pop('profit', None)
            for line in data['lines']:
                line.pop('unit_cost', None)
        return data


class StockMovementSerializer(serializers.ModelSerializer):
    medicine_name = serializers.CharField(source='batch.medicine.name', read_only=True)
    batch_number = serializers.CharField(source='batch.batch_number', read_only=True)

    class Meta:
        model = StockMovement
        fields = '__all__'
        read_only_fields = tuple(field.name for field in StockMovement._meta.fields)


class StockAdjustmentSerializer(serializers.Serializer):
    class Category:
        COUNT_CORRECTION = 'count_correction'
        DAMAGE = 'damage'
        EXPIRY = 'expiry'
        LOSS = 'loss'
        RETURN = 'return'

    batch = serializers.PrimaryKeyRelatedField(queryset=MedicineBatch.objects.filter(is_active=True))
    quantity_change = serializers.DecimalField(max_digits=12, decimal_places=3)
    category = serializers.ChoiceField(choices=(
        Category.COUNT_CORRECTION, Category.DAMAGE, Category.EXPIRY, Category.LOSS, Category.RETURN,
    ), required=False, default=Category.COUNT_CORRECTION)
    reason = serializers.CharField(max_length=220, trim_whitespace=True)

    def validate_quantity_change(self, value):
        if value == 0:
            raise serializers.ValidationError('Quantity change cannot be zero.')
        return value

    def validate(self, attrs):
        batch = attrs['batch']
        if attrs['quantity_change'] > 0 and batch.expiry_date <= timezone.localdate():
            raise serializers.ValidationError({'batch': 'Expired batches cannot receive positive stock adjustments.'})
        request = self.context['request']
        if attrs['quantity_change'] > 0 and not (request.user.is_superuser or request.user.has_role('administrator')):
            raise serializers.ValidationError({'quantity_change': 'Only an administrator may make a positive adjustment.'})
        return attrs

    def create(self, validated_data):
        category = validated_data.pop('category')
        validated_data['reason'] = f'[{category}] {validated_data["reason"]}'
        movement_type = (
            StockMovement.MovementType.ADJUSTMENT_IN if validated_data['quantity_change'] > 0
            else StockMovement.MovementType.ADJUSTMENT_OUT
        )
        return change_stock(
            **validated_data, movement_type=movement_type,
            reference=f'adjustment:{uuid.uuid4()}', user=self.context['request'].user,
        )


class SupplierPaymentSerializer(ImmutableTransactionMixin, serializers.ModelSerializer):
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    wallet_name = serializers.CharField(source='wallet.name', read_only=True)

    class Meta:
        model = SupplierPayment
        fields = '__all__'
        read_only_fields = (
            'id', 'created_by', 'is_void', 'voided_by', 'void_reason', 'created_at', 'updated_at',
        )

    def validate(self, attrs):
        purchase = attrs.get('purchase')
        supplier = attrs['supplier']
        if purchase and purchase.supplier_id != supplier.pk:
            raise serializers.ValidationError({'purchase': 'Purchase belongs to another supplier.'})
        if purchase and purchase.status != Purchase.Status.POSTED:
            raise serializers.ValidationError({'purchase': 'Payments may only be allocated to posted purchases.'})
        if not supplier.is_active:
            raise serializers.ValidationError({'supplier': 'Inactive suppliers cannot receive payments.'})
        if not attrs['wallet'].is_active:
            raise serializers.ValidationError({'wallet': 'Inactive wallets cannot be used.'})
        if attrs['wallet'].kind not in (Wallet.Kind.PHARMACY, Wallet.Kind.MANAGER):
            raise serializers.ValidationError({'wallet': 'Supplier payments require a pharmacy or manager wallet.'})
        if attrs['payment_date'] > timezone.localdate():
            raise serializers.ValidationError({'payment_date': 'Payment date cannot be in the future.'})
        supplier_due = max(supplier.amount_due, Decimal('0'))
        due = min(purchase.amount_due, supplier_due) if purchase else supplier_due
        if attrs['amount'] > due:
            raise serializers.ValidationError({'amount': f'Payment exceeds the current due amount ({due}).'})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        user = self.context['request'].user
        purchase = validated_data.get('purchase')
        supplier = validated_data['supplier']
        locked_supplier = Supplier.objects.select_for_update().get(pk=supplier.pk)
        supplier_due = max(locked_supplier.amount_due, Decimal('0'))
        if purchase:
            locked_due_source = Purchase.objects.select_for_update().get(pk=purchase.pk)
            if locked_due_source.status == Purchase.Status.VOID:
                raise serializers.ValidationError({'purchase': 'Payments cannot be allocated to a void purchase.'})
            current_due = min(locked_due_source.amount_due, supplier_due)
        else:
            current_due = supplier_due
        if validated_data['amount'] > current_due:
            raise serializers.ValidationError({'amount': f'Payment exceeds the current due amount ({current_due}).'})
        payment = SupplierPayment.objects.create(created_by=user, **validated_data)
        post_wallet_entry(
            wallet=payment.wallet, entry_type=WalletTransaction.EntryType.DEBIT, amount=payment.amount,
            category='supplier_payment', description=f'Payment to {payment.supplier.name}',
            reference=f'supplier-payment:{payment.pk}', user=user, source=payment,
            transaction_date=timezone.make_aware(datetime.combine(payment.payment_date, time.min)),
        )
        return payment


class VoidTransactionSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255, trim_whitespace=True, allow_blank=False)
