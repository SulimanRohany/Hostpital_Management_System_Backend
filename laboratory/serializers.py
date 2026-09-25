from datetime import timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from .models import LabOrder, LabOrderItem, LabTest


def as_serializer_error(exc):
    return serializers.ValidationError(
        getattr(exc, 'message_dict', None) or getattr(exc, 'messages', None) or str(exc)
    )


class LabTestSerializer(serializers.ModelSerializer):
    # Model validation normalizes these values, so uniqueness must be checked after
    # normalization rather than by ModelSerializer's default exact-match validator.
    code = serializers.CharField(max_length=30, validators=[])
    name = serializers.CharField(max_length=150, validators=[])

    class Meta:
        model = LabTest
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')

    def validate(self, attrs):
        attrs['code'] = attrs.get('code', getattr(self.instance, 'code', '')).strip().upper()
        attrs['name'] = attrs.get('name', getattr(self.instance, 'name', '')).strip()
        for field in ('specimen_type', 'unit', 'reference_range', 'instructions'):
            if field in attrs:
                attrs[field] = attrs[field].strip()
        errors = {}
        queryset = LabTest.objects.all()
        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.filter(code__iexact=attrs['code']).exists():
            errors['code'] = 'A laboratory test with this code already exists.'
        if queryset.filter(name__iexact=attrs['name']).exists():
            errors['name'] = 'A laboratory test with this name already exists.'
        service = attrs.get('service', getattr(self.instance, 'service', None))
        if service and not service.is_laboratory:
            errors['service'] = 'The linked service must be marked as a laboratory service.'
        elif service and not service.is_active:
            errors['service'] = 'The linked laboratory service must be active.'
        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def create(self, validated_data):
        try:
            return super().create(validated_data)
        except DjangoValidationError as exc:
            raise as_serializer_error(exc) from exc

    def update(self, instance, validated_data):
        try:
            return super().update(instance, validated_data)
        except DjangoValidationError as exc:
            raise as_serializer_error(exc) from exc


class LabOrderItemCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = LabOrderItem
        fields = ('test',)


class LabOrderItemSerializer(serializers.ModelSerializer):
    resulted_by_name = serializers.CharField(source='resulted_by.get_full_name', read_only=True)

    class Meta:
        model = LabOrderItem
        fields = '__all__'
        read_only_fields = (
            'id', 'order', 'test', 'test_code', 'test_name', 'specimen_type', 'result',
            'result_unit', 'reference_range', 'is_abnormal', 'resulted_by', 'resulted_at',
            'created_at', 'updated_at',
        )


class LabOrderValidationMixin:
    def validate_order_fields(self, attrs):
        visit = attrs.get('visit', getattr(self.instance, 'visit', None))
        patient = attrs.get('patient', getattr(self.instance, 'patient', None))
        if visit and patient and visit.patient_id != patient.pk:
            raise serializers.ValidationError({'visit': 'The selected visit belongs to another patient.'})
        if not self.instance and patient and not patient.is_active:
            raise serializers.ValidationError({'patient': 'Inactive patients cannot receive new laboratory orders.'})
        if visit and not self.instance and visit.status in (visit.Status.COMPLETED, visit.Status.CANCELLED):
            raise serializers.ValidationError({'visit': 'Laboratory orders cannot be added to a completed or cancelled visit.'})
        ordered_at = attrs.get('ordered_at', getattr(self.instance, 'ordered_at', None))
        if ordered_at and ordered_at > timezone.now() + timedelta(minutes=5):
            raise serializers.ValidationError({'ordered_at': 'Order time cannot be in the future.'})
        if 'clinical_notes' in attrs:
            attrs['clinical_notes'] = attrs['clinical_notes'].strip()
        return attrs


class LabOrderCreateSerializer(LabOrderValidationMixin, serializers.ModelSerializer):
    items = LabOrderItemCreateSerializer(many=True)

    class Meta:
        model = LabOrder
        fields = ('patient', 'visit', 'ordered_at', 'clinical_notes', 'items')

    def validate(self, attrs):
        attrs = self.validate_order_fields(attrs)
        items = attrs.get('items') or []
        if not items:
            raise serializers.ValidationError({'items': 'At least one test is required.'})
        test_ids = [item['test'].pk for item in items]
        if len(test_ids) != len(set(test_ids)):
            raise serializers.ValidationError({'items': 'The same test cannot be ordered twice.'})
        if any(not item['test'].is_active for item in items):
            raise serializers.ValidationError({'items': 'Inactive tests cannot be ordered.'})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        items = validated_data.pop('items')
        try:
            return LabOrder.create_with_items(
                items=items, ordered_by=self.context['request'].user, **validated_data
            )
        except DjangoValidationError as exc:
            raise as_serializer_error(exc) from exc


class LabOrderUpdateSerializer(LabOrderValidationMixin, serializers.ModelSerializer):
    class Meta:
        model = LabOrder
        fields = ('patient', 'visit', 'ordered_at', 'clinical_notes')

    def validate(self, attrs):
        if 'items' in self.initial_data:
            raise serializers.ValidationError({
                'items': 'Order items cannot be changed through this endpoint.'
            })
        if self.instance.status != LabOrder.Status.ORDERED or self.instance.items.filter(resulted_at__isnull=False).exists():
            raise serializers.ValidationError('Only unprocessed laboratory orders can be edited.')
        return self.validate_order_fields(attrs)

    def update(self, instance, validated_data):
        try:
            return super().update(instance, validated_data)
        except DjangoValidationError as exc:
            raise as_serializer_error(exc) from exc


class LabOrderSerializer(serializers.ModelSerializer):
    items = LabOrderItemSerializer(many=True, read_only=True)
    patient_name = serializers.CharField(source='patient.full_name', read_only=True)
    ordered_by_name = serializers.CharField(source='ordered_by.get_full_name', read_only=True)
    collected_by_name = serializers.CharField(source='collected_by.get_full_name', read_only=True)
    cancelled_by_name = serializers.CharField(source='cancelled_by.get_full_name', read_only=True)

    class Meta:
        model = LabOrder
        fields = '__all__'
        read_only_fields = (
            'id', 'order_number', 'patient', 'visit', 'ordered_by', 'ordered_at', 'status',
            'clinical_notes', 'collected_by', 'collected_at', 'cancelled_by', 'cancelled_at',
            'cancellation_reason', 'created_at', 'updated_at',
        )


class LabResultSerializer(serializers.ModelSerializer):
    correction_reason = serializers.CharField(
        min_length=3, max_length=1000, trim_whitespace=True, required=False, allow_blank=True, write_only=True
    )

    class Meta:
        model = LabOrderItem
        fields = ('result', 'result_unit', 'reference_range', 'is_abnormal', 'correction_reason')
        extra_kwargs = {
            'result': {'required': True, 'allow_blank': False, 'trim_whitespace': True},
            'result_unit': {'trim_whitespace': True},
            'reference_range': {'trim_whitespace': True},
        }

    def validate(self, attrs):
        correction_reason = attrs.get('correction_reason')
        if correction_reason is not None:
            attrs['correction_reason'] = correction_reason.strip()
        if self.instance.resulted_at and not attrs.get('correction_reason', '').strip():
            raise serializers.ValidationError({
                'correction_reason': 'A reason is required when correcting an existing result.'
            })
        return attrs

    def update(self, instance, validated_data):
        validated_data.pop('correction_reason', None)
        try:
            return instance.record_result(
                user=self.context['request'].user,
                result=validated_data['result'],
                result_unit=validated_data.get('result_unit'),
                reference_range=validated_data.get('reference_range'),
                is_abnormal=validated_data.get('is_abnormal', instance.is_abnormal),
            )
        except DjangoValidationError as exc:
            raise as_serializer_error(exc) from exc


class LabOrderCancellationSerializer(serializers.Serializer):
    reason = serializers.CharField(min_length=3, max_length=1000, trim_whitespace=True)


class LabOrderFilterSerializer(serializers.Serializer):
    date = serializers.DateField(required=False)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    patient = serializers.UUIDField(required=False)
    status = serializers.ChoiceField(choices=LabOrder.Status.choices, required=False)

    def validate(self, attrs):
        if attrs.get('date') and (attrs.get('date_from') or attrs.get('date_to')):
            raise serializers.ValidationError('Use either date or a date range, not both.')
        if attrs.get('date_from') and attrs.get('date_to') and attrs['date_from'] > attrs['date_to']:
            raise serializers.ValidationError({'date_to': 'End date must not be before start date.'})
        return attrs
