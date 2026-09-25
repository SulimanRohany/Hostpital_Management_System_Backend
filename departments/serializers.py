from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from rest_framework import serializers

from .models import Department, Service, ServiceFeeHistory


def as_serializer_error(exc):
    if isinstance(exc, IntegrityError):
        return serializers.ValidationError(
            {'detail': 'The submitted department or service conflicts with an existing record.'}
        )
    return serializers.ValidationError(
        getattr(exc, 'message_dict', None) or getattr(exc, 'messages', None) or str(exc)
    )


class ValidatedModelSerializer(serializers.ModelSerializer):
    """Convert model/database validation failures into consistent API errors."""

    def create(self, validated_data):
        try:
            with transaction.atomic():
                return super().create(validated_data)
        except (DjangoValidationError, IntegrityError) as exc:
            raise as_serializer_error(exc) from exc

    def update(self, instance, validated_data):
        try:
            with transaction.atomic():
                return super().update(instance, validated_data)
        except (DjangoValidationError, IntegrityError) as exc:
            raise as_serializer_error(exc) from exc


class DepartmentWriteSerializer(ValidatedModelSerializer):
    # Model validation runs after normalization; disable exact-match validators.
    code = serializers.CharField(max_length=20, validators=[])
    name = serializers.CharField(max_length=120, validators=[])

    class Meta:
        model = Department
        fields = ('id', 'code', 'name', 'description', 'is_clinical', 'is_active', 'created_at', 'updated_at')
        read_only_fields = ('id', 'is_active', 'created_at', 'updated_at')

    def validate(self, attrs):
        if 'is_active' in self.initial_data:
            raise serializers.ValidationError({
                'is_active': 'Use the activate or deactivate endpoint to change department status.'
            })
        code = attrs.get('code', getattr(self.instance, 'code', '')).strip().upper()
        name = attrs.get('name', getattr(self.instance, 'name', '')).strip()
        attrs['code'] = code
        attrs['name'] = name
        if 'description' in attrs:
            attrs['description'] = attrs['description'].strip()

        errors = {}
        if not code:
            errors['code'] = 'Department code cannot be blank.'
        if not name:
            errors['name'] = 'Department name cannot be blank.'
        queryset = Department.objects.all()
        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)
        if code and queryset.filter(code__iexact=code).exists():
            errors['code'] = 'A department with this code already exists.'
        if name and queryset.filter(name__iexact=name).exists():
            errors['name'] = 'A department with this name already exists.'
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class DepartmentSerializer(DepartmentWriteSerializer):
    has_active_services = serializers.SerializerMethodField()
    can_be_deactivated = serializers.SerializerMethodField()
    deactivation_blockers = serializers.SerializerMethodField()
    service_count = serializers.IntegerField(read_only=True, default=0)
    active_service_count = serializers.IntegerField(read_only=True, default=0)

    class Meta(DepartmentWriteSerializer.Meta):
        fields = DepartmentWriteSerializer.Meta.fields + (
            'has_active_services', 'can_be_deactivated', 'deactivation_blockers',
            'service_count', 'active_service_count',
        )

    def get_has_active_services(self, obj):
        annotated = getattr(obj, '_has_active_services', None)
        return obj.has_active_services if annotated is None else annotated

    def get_can_be_deactivated(self, obj):
        return not self.get_deactivation_blockers(obj)

    def get_deactivation_blockers(self, obj):
        annotations = (
            ('_has_active_services', 'active services'),
            ('_has_active_users', 'active users'),
            ('_has_current_employees', 'current employees'),
            ('_has_current_assignments', 'current employment assignments'),
            ('_has_open_visits', 'open visits'),
            ('_has_active_queue_entries', 'active queue entries'),
        )
        if all(hasattr(obj, attribute) for attribute, _ in annotations):
            return [label for attribute, label in annotations if getattr(obj, attribute)]
        return list(obj.deactivation_blockers)


class ServiceWriteSerializer(ValidatedModelSerializer):
    code = serializers.CharField(max_length=30, validators=[])
    name = serializers.CharField(max_length=150, validators=[])
    fee_change_reason = serializers.CharField(write_only=True, required=False, allow_blank=False, max_length=255)

    class Meta:
        model = Service
        fields = (
            'id', 'department', 'code', 'name', 'standard_fee', 'is_laboratory',
            'is_discountable', 'is_active', 'fee_change_reason', 'created_at', 'updated_at',
        )
        read_only_fields = ('id', 'is_active', 'created_at', 'updated_at')

    def validate(self, attrs):
        if 'is_active' in self.initial_data:
            raise serializers.ValidationError({
                'is_active': 'Use the activate or deactivate endpoint to change service status.'
            })
        code = attrs.get('code', getattr(self.instance, 'code', '')).strip().upper()
        name = attrs.get('name', getattr(self.instance, 'name', '')).strip()
        department = attrs.get('department', getattr(self.instance, 'department', None))
        is_laboratory = attrs.get('is_laboratory', getattr(self.instance, 'is_laboratory', False))
        is_active = getattr(self.instance, 'is_active', True)
        fee_change_reason = attrs.pop('fee_change_reason', None)
        attrs['code'] = code
        attrs['name'] = name

        errors = {}
        if not code:
            errors['code'] = 'Service code cannot be blank.'
        if not name:
            errors['name'] = 'Service name cannot be blank.'
        queryset = Service.objects.all()
        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)
        if code and queryset.filter(code__iexact=code).exists():
            errors['code'] = 'A service with this code already exists.'
        if department and name and queryset.filter(department=department, name__iexact=name).exists():
            errors['name'] = 'A service with this name already exists in the selected department.'
        if department and is_active and not department.is_active:
            errors['department'] = 'An active service cannot belong to an inactive department.'
        if department and is_active and is_laboratory and not department.is_clinical:
            errors['is_laboratory'] = 'An active laboratory service must belong to a clinical department.'
        if self.instance and 'standard_fee' in attrs and attrs['standard_fee'] != self.instance.standard_fee:
            if not fee_change_reason:
                errors['fee_change_reason'] = 'A reason is required when changing a standard fee.'
            else:
                self._fee_change_reason = fee_change_reason.strip()
        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def update(self, instance, validated_data):
        instance._fee_change_reason = getattr(self, '_fee_change_reason', '')
        request = self.context.get('request')
        instance._fee_changed_by = request.user if request and request.user.is_authenticated else None
        try:
            return super().update(instance, validated_data)
        finally:
            instance.__dict__.pop('_fee_change_reason', None)
            instance.__dict__.pop('_fee_changed_by', None)

    def create(self, validated_data):
        instance = Service(**validated_data)
        request = self.context.get('request')
        instance._fee_changed_by = request.user if request and request.user.is_authenticated else None
        try:
            with transaction.atomic():
                instance.save()
        except (DjangoValidationError, IntegrityError) as exc:
            raise as_serializer_error(exc) from exc
        finally:
            instance.__dict__.pop('_fee_changed_by', None)
        return instance


class ServiceSerializer(ServiceWriteSerializer):
    department_name = serializers.CharField(source='department.name', read_only=True)
    is_available = serializers.BooleanField(read_only=True)

    class Meta(ServiceWriteSerializer.Meta):
        fields = ServiceWriteSerializer.Meta.fields + ('department_name', 'is_available')


class ServiceFeeHistorySerializer(serializers.ModelSerializer):
    changed_by_name = serializers.CharField(source='changed_by.display_name', read_only=True, default=None)

    class Meta:
        model = ServiceFeeHistory
        fields = (
            'id', 'service', 'previous_fee', 'new_fee', 'reason',
            'changed_by', 'changed_by_name', 'changed_at',
        )
        read_only_fields = fields


class DepartmentLifecycleSerializer(serializers.Serializer):
    deactivate_services = serializers.BooleanField(default=False)


class DepartmentFilterSerializer(serializers.Serializer):
    is_active = serializers.BooleanField(required=False)
    is_clinical = serializers.BooleanField(required=False)


class ServiceFilterSerializer(serializers.Serializer):
    department = serializers.UUIDField(required=False)
    is_active = serializers.BooleanField(required=False)
    is_laboratory = serializers.BooleanField(required=False)
    is_discountable = serializers.BooleanField(required=False)
    is_available = serializers.BooleanField(required=False)
