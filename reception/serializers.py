from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from accounts.models import User
from .models import Visit, VisitPayment, VisitQueueEntry, VisitService


PRIVILEGED_ROLES = (User.Role.ADMINISTRATOR, User.Role.MANAGER)


class VisitServiceWriteSerializer(serializers.ModelSerializer):
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0.00'), required=False)

    class Meta:
        model = VisitService
        fields = ('service', 'quantity', 'unit_price', 'price_override_reason')


class VisitServiceReadSerializer(serializers.ModelSerializer):
    line_total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    is_price_override = serializers.SerializerMethodField()

    class Meta:
        model = VisitService
        fields = (
            'id', 'service', 'service_code', 'service_name', 'quantity', 'standard_fee', 'unit_price',
            'line_total', 'is_price_override', 'price_override_reason', 'price_overridden_by',
        )

    def get_is_price_override(self, obj):
        return obj.unit_price != obj.standard_fee


class VisitPaymentSerializer(serializers.ModelSerializer):
    received_by_name = serializers.CharField(source='received_by.get_full_name', read_only=True)

    class Meta:
        model = VisitPayment
        fields = (
            'id', 'payment_type', 'amount', 'method', 'transaction_reference', 'notes',
            'received_by', 'received_by_name', 'paid_at', 'created_at',
        )
        read_only_fields = fields


class VisitQueueSerializer(serializers.ModelSerializer):
    patient_name = serializers.CharField(source='visit.patient.full_name', read_only=True)
    visit_number = serializers.CharField(source='visit.visit_number', read_only=True)
    department_name = serializers.CharField(source='department.name', read_only=True)

    class Meta:
        model = VisitQueueEntry
        fields = (
            'id', 'visit', 'visit_number', 'patient_name', 'department', 'department_name', 'queue_date',
            'token_number', 'priority', 'status', 'called_at', 'serving_at', 'finished_at', 'created_at', 'updated_at',
        )
        read_only_fields = fields


class ProviderValidationMixin:
    def validate_provider(self, provider):
        if provider is None:
            return provider
        department = self.initial_data.get('department')
        department_id = str(department or getattr(self.instance, 'department_id', ''))
        if not provider.is_active:
            raise serializers.ValidationError('The selected provider is inactive.')
        if not provider.is_superuser and not provider.has_role(User.Role.CLINICIAN):
            raise serializers.ValidationError('The selected provider must have the clinician role.')
        if provider.department_id and str(provider.department_id) != department_id:
            raise serializers.ValidationError('The selected provider does not belong to this department.')
        employee = getattr(provider, 'employee_profile', None)
        if employee and employee.status != employee.Status.ACTIVE:
            raise serializers.ValidationError('The selected provider is not currently available for work.')
        return provider


class VisitCreateSerializer(ProviderValidationMixin, serializers.ModelSerializer):
    service_lines = VisitServiceWriteSerializer(many=True)
    initial_payment_method = serializers.ChoiceField(
        choices=VisitPayment.Method.choices, default=VisitPayment.Method.CASH, write_only=True
    )
    initial_payment_reference = serializers.CharField(max_length=120, required=False, allow_blank=True, write_only=True)
    queue_priority = serializers.IntegerField(min_value=0, max_value=100, default=0, write_only=True)

    class Meta:
        model = Visit
        fields = (
            'patient', 'department', 'provider', 'visit_type', 'room', 'referral_source', 'visit_date',
            'discount_amount', 'discount_reason', 'paid_amount', 'notes', 'service_lines',
            'initial_payment_method', 'initial_payment_reference', 'queue_priority',
        )

    def validate(self, attrs):
        lines = attrs.get('service_lines') or []
        if not lines:
            raise serializers.ValidationError({'service_lines': 'At least one service is required.'})
        if not attrs['patient'].is_active:
            raise serializers.ValidationError({'patient': 'Inactive patients cannot be registered.'})
        if not attrs['department'].is_active:
            raise serializers.ValidationError({'department': 'Inactive departments cannot receive visits.'})
        if attrs['visit_date'] > timezone.now() + timedelta(minutes=5):
            raise serializers.ValidationError({'visit_date': 'Visit date cannot be in the future.'})
        service_ids = [line['service'].pk for line in lines]
        if len(service_ids) != len(set(service_ids)):
            raise serializers.ValidationError({'service_lines': 'The same service cannot be added twice.'})
        department = attrs['department']
        if any(line['service'].department_id != department.pk for line in lines):
            raise serializers.ValidationError({'service_lines': 'All services must belong to the selected department.'})
        if any(not line['service'].is_active for line in lines):
            raise serializers.ValidationError({'service_lines': 'Inactive services cannot be selected.'})

        user = self.context['request'].user
        provider = attrs.get('provider')
        if provider and hasattr(provider, 'employee_profile'):
            employee = provider.employee_profile
            unavailable = employee.attendance_records.filter(
                date=timezone.localdate(attrs['visit_date']), status__in=('absent', 'leave')
            ).exists()
            if unavailable:
                raise serializers.ValidationError({'provider': 'The selected provider is unavailable on the visit date.'})
        normalized_lines = []
        for line in lines:
            standard_fee = line['service'].standard_fee
            supplied_price = line.get('unit_price', standard_fee)
            reason = line.get('price_override_reason', '').strip()
            if supplied_price != standard_fee:
                if not user.has_role(*PRIVILEGED_ROLES):
                    raise serializers.ValidationError({'service_lines': 'Only managers may override service prices.'})
                if not reason:
                    raise serializers.ValidationError({'service_lines': 'A reason is required for every price override.'})
                line['price_overridden_by'] = user
            elif reason:
                raise serializers.ValidationError({'service_lines': 'A price override reason is only valid when the price changes.'})
            line['unit_price'] = supplied_price
            normalized_lines.append(line)

        total = sum((line['unit_price'] * line['quantity'] for line in normalized_lines), Decimal('0.00'))
        discount = attrs.get('discount_amount', Decimal('0.00'))
        paid = attrs.get('paid_amount', Decimal('0.00'))
        if discount > total:
            raise serializers.ValidationError({'discount_amount': 'Discount cannot exceed the service total.'})
        if discount and not attrs.get('discount_reason', '').strip():
            raise serializers.ValidationError({'discount_reason': 'A reason is required when a discount is applied.'})
        if discount and any(not line['service'].is_discountable for line in normalized_lines):
            raise serializers.ValidationError({'discount_amount': 'This visit contains a service that cannot be discounted.'})
        discount_percent = (discount * Decimal('100') / total) if total else Decimal('0')
        max_percent = Decimal(settings.RECEPTION_MAX_DISCOUNT_PERCENT)
        if discount_percent > max_percent and not user.has_role(*PRIVILEGED_ROLES):
            raise serializers.ValidationError({
                'discount_amount': f'Discounts above {max_percent}% require manager or administrator approval.'
            })
        if discount and user.has_role(*PRIVILEGED_ROLES):
            attrs['discount_approved_by'] = user
            attrs['discount_approved_at'] = timezone.now()
        if paid > total - discount:
            raise serializers.ValidationError({'paid_amount': 'Paid amount cannot exceed the net amount.'})
        method = attrs.get('initial_payment_method', VisitPayment.Method.CASH)
        reference = attrs.get('initial_payment_reference', '').strip()
        if paid and method != VisitPayment.Method.CASH and not reference:
            raise serializers.ValidationError({'initial_payment_reference': 'A transaction reference is required.'})
        if attrs.get('visit_type') == Visit.VisitType.EMERGENCY:
            attrs['queue_priority'] = VisitQueueEntry.PRIORITY_EMERGENCY
        attrs['_calculated_total'] = total
        attrs['service_lines'] = normalized_lines
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        lines = validated_data.pop('service_lines')
        total = validated_data.pop('_calculated_total')
        initial_payment = validated_data.pop('paid_amount', Decimal('0.00'))
        payment_method = validated_data.pop('initial_payment_method', VisitPayment.Method.CASH)
        payment_reference = validated_data.pop('initial_payment_reference', '')
        queue_priority = validated_data.pop('queue_priority', VisitQueueEntry.PRIORITY_NORMAL)
        user = self.context['request'].user
        visit = Visit.objects.create(total_amount=total, paid_amount=Decimal('0.00'), created_by=user, **validated_data)
        for line in lines:
            VisitService(visit=visit, **line).save(skip_recalculate=True)
        visit.recalculate_total()
        VisitQueueEntry.create_for_visit(visit, priority=queue_priority)
        if initial_payment:
            visit.collect_payment(
                amount=initial_payment, user=user, method=payment_method,
                reference=payment_reference, paid_at=visit.visit_date,
            )
            visit.refresh_from_db()
        return visit


class VisitUpdateSerializer(ProviderValidationMixin, serializers.ModelSerializer):
    class Meta:
        model = Visit
        fields = ('provider', 'room', 'referral_source', 'notes')


class VisitDetailSerializer(serializers.ModelSerializer):
    service_lines = VisitServiceReadSerializer(many=True, read_only=True)
    payments = VisitPaymentSerializer(many=True, read_only=True)
    queue_entry = VisitQueueSerializer(read_only=True)
    patient_name = serializers.CharField(source='patient.full_name', read_only=True)
    department_name = serializers.CharField(source='department.name', read_only=True)
    provider_name = serializers.CharField(source='provider.get_full_name', read_only=True)
    net_amount = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    balance_due = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    payment_status = serializers.CharField(read_only=True)

    class Meta:
        model = Visit
        fields = '__all__'


class CollectPaymentSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal('0.01'))
    method = serializers.ChoiceField(choices=VisitPayment.Method.choices, default=VisitPayment.Method.CASH)
    transaction_reference = serializers.CharField(max_length=120, required=False, allow_blank=True, trim_whitespace=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        if attrs['method'] != VisitPayment.Method.CASH and not attrs.get('transaction_reference', ''):
            raise serializers.ValidationError({'transaction_reference': 'A transaction reference is required.'})
        return attrs


class CancellationSerializer(serializers.Serializer):
    reason = serializers.CharField(min_length=3, max_length=1000, trim_whitespace=True)


class QueueTransitionSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=VisitQueueEntry.Status.choices)


class VisitFilterSerializer(serializers.Serializer):
    date = serializers.DateField(required=False)
    visit_date_from = serializers.DateField(required=False)
    visit_date_to = serializers.DateField(required=False)
    department = serializers.UUIDField(required=False)
    patient = serializers.UUIDField(required=False)
    provider = serializers.UUIDField(required=False)
    status = serializers.ChoiceField(choices=Visit.Status.choices, required=False)
    visit_type = serializers.ChoiceField(choices=Visit.VisitType.choices, required=False)
    payment_status = serializers.ChoiceField(
        choices=('unpaid', 'partially_paid', 'paid', 'refunded'), required=False
    )

    def validate(self, attrs):
        if attrs.get('date') and (attrs.get('visit_date_from') or attrs.get('visit_date_to')):
            raise serializers.ValidationError('Use either date or a date range, not both.')
        if attrs.get('visit_date_from') and attrs.get('visit_date_to') and attrs['visit_date_from'] > attrs['visit_date_to']:
            raise serializers.ValidationError({'visit_date_to': 'End date must not be before start date.'})
        return attrs


class QueueFilterSerializer(serializers.Serializer):
    date = serializers.DateField(required=False, default=timezone.localdate)
    department = serializers.UUIDField(required=False)
    status = serializers.ChoiceField(choices=VisitQueueEntry.Status.choices, required=False)
