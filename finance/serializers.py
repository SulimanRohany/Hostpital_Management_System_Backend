from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from core.mixins import ImmutableTransactionMixin
from .models import Expense, ExpenseCategory, Turnover, Wallet, WalletTransaction
from .services import create_expense, create_turnover


def translate_model_validation(exc):
    detail = getattr(exc, 'message_dict', None) or getattr(exc, 'messages', None) or str(exc)
    return serializers.ValidationError(detail)


class ModelValidationSerializerMixin:
    """Return model-domain validation as an API validation response."""

    def _save_with_model_validation(self, method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except DjangoValidationError as exc:
            raise translate_model_validation(exc) from exc

    def create(self, validated_data):
        return self._save_with_model_validation(super().create, validated_data)

    def update(self, instance, validated_data):
        return self._save_with_model_validation(super().update, instance, validated_data)


class WalletSerializer(ModelValidationSerializerMixin, serializers.ModelSerializer):
    kind_display = serializers.CharField(source='get_kind_display', read_only=True)

    class Meta:
        model = Wallet
        fields = (
            'id', 'code', 'name', 'kind', 'kind_display', 'balance', 'allow_negative',
            'is_active', 'created_at', 'updated_at',
        )
        read_only_fields = ('id', 'kind_display', 'balance', 'created_at', 'updated_at')

    def validate(self, attrs):
        instance = self.instance
        code = (attrs.get('code', getattr(instance, 'code', '')) or '').strip().upper()
        name = (attrs.get('name', getattr(instance, 'name', '')) or '').strip()
        kind = attrs.get('kind', getattr(instance, 'kind', Wallet.Kind.CUSTOM))
        errors = {}
        code_conflict = Wallet.objects.filter(code__iexact=code)
        name_conflict = Wallet.objects.filter(name__iexact=name)
        if instance:
            code_conflict = code_conflict.exclude(pk=instance.pk)
            name_conflict = name_conflict.exclude(pk=instance.pk)
        if code_conflict.exists():
            errors['code'] = 'A wallet with this code already exists.'
        if name_conflict.exists():
            errors['name'] = 'A wallet with this name already exists.'
        if not instance and kind != Wallet.Kind.CUSTOM:
            errors['kind'] = 'System wallets are provisioned automatically; create a custom wallet instead.'
        if instance and instance.kind != Wallet.Kind.CUSTOM:
            if code != instance.code:
                errors['code'] = 'A system wallet code cannot be changed.'
            if name != instance.name:
                errors['name'] = 'A system wallet name cannot be changed.'
            if kind != instance.kind:
                errors['kind'] = 'A system wallet kind cannot be changed.'
            if 'allow_negative' in attrs and attrs['allow_negative'] != instance.allow_negative:
                errors['allow_negative'] = 'A system wallet negative-balance policy cannot be changed.'
        if errors:
            raise serializers.ValidationError(errors)
        attrs['code'] = code
        attrs['name'] = name
        return attrs


class WalletTransactionSerializer(ImmutableTransactionMixin, serializers.ModelSerializer):
    wallet_name = serializers.CharField(source='wallet.name', read_only=True)
    entry_type_display = serializers.CharField(source='get_entry_type_display', read_only=True)
    category_display = serializers.CharField(source='get_category_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.display_name', read_only=True)
    reverses_reference = serializers.CharField(source='reverses.reference', read_only=True, default=None)
    reversed_by_reference = serializers.SerializerMethodField()

    class Meta:
        model = WalletTransaction
        fields = (
            'id', 'wallet', 'wallet_name', 'entry_type', 'entry_type_display', 'amount',
            'balance_after', 'transaction_date', 'category', 'category_display', 'description',
            'reference', 'source_type', 'source_id', 'reverses', 'reverses_reference',
            'reversed_by_reference', 'created_by', 'created_by_name', 'created_at',
        )
        read_only_fields = fields

    def get_reversed_by_reference(self, obj):
        try:
            return obj.reversal_entry.reference
        except WalletTransaction.DoesNotExist:
            return None


class ExpenseCategorySerializer(ModelValidationSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = ExpenseCategory
        fields = ('id', 'name', 'description', 'is_active', 'created_at', 'updated_at')
        read_only_fields = ('id', 'created_at', 'updated_at')

    def validate_name(self, value):
        name = (value or '').strip()
        conflict = ExpenseCategory.objects.filter(name__iexact=name)
        if self.instance:
            conflict = conflict.exclude(pk=self.instance.pk)
        if conflict.exists():
            raise serializers.ValidationError('An expense category with this name already exists.')
        return name


class ExpenseSerializer(ImmutableTransactionMixin, serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    wallet_name = serializers.CharField(source='wallet.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.display_name', read_only=True)
    voided_by_name = serializers.CharField(source='voided_by.display_name', read_only=True, default=None)

    class Meta:
        model = Expense
        fields = (
            'id', 'category', 'category_name', 'wallet', 'wallet_name', 'expense_date',
            'amount', 'payee', 'purpose', 'receipt_number', 'attachment', 'created_by',
            'created_by_name', 'is_void', 'void_reason', 'voided_by', 'voided_by_name',
            'voided_at', 'created_at', 'updated_at',
        )
        read_only_fields = (
            'id', 'created_by', 'created_by_name', 'is_void', 'void_reason', 'voided_by',
            'voided_by_name', 'voided_at', 'created_at', 'updated_at',
        )

    def validate(self, attrs):
        errors = {}
        if not attrs['category'].is_active:
            errors['category'] = 'Inactive expense categories cannot be used.'
        if not attrs['wallet'].is_active:
            errors['wallet'] = 'Inactive wallets cannot be used.'
        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def create(self, validated_data):
        return create_expense(user=self.context['request'].user, **validated_data)


class TurnoverSerializer(ImmutableTransactionMixin, serializers.ModelSerializer):
    source_wallet_name = serializers.CharField(source='source_wallet.name', read_only=True)
    destination_wallet_name = serializers.CharField(source='destination_wallet.name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    handed_over_by_name = serializers.CharField(source='handed_over_by.display_name', read_only=True)
    received_by_name = serializers.CharField(source='received_by.display_name', read_only=True, default=None)

    class Meta:
        model = Turnover
        fields = (
            'id', 'source_wallet', 'source_wallet_name', 'destination_wallet',
            'destination_wallet_name', 'amount', 'period_start', 'period_end', 'handed_over_by',
            'handed_over_by_name', 'status', 'status_display', 'received_by', 'received_by_name',
            'received_at', 'notes', 'created_at',
        )
        read_only_fields = (
            'id', 'handed_over_by', 'handed_over_by_name', 'status', 'status_display',
            'received_by', 'received_by_name', 'received_at', 'created_at',
        )

    def validate(self, attrs):
        errors = {}
        source = attrs['source_wallet']
        destination = attrs['destination_wallet']
        if source == destination:
            errors['destination_wallet'] = 'Source and destination wallets must be different.'
        if not source.is_active:
            errors['source_wallet'] = 'The source wallet is inactive.'
        if not destination.is_active:
            errors['destination_wallet'] = 'The destination wallet is inactive.'
        if attrs['period_end'] <= attrs['period_start']:
            errors['period_end'] = 'Period end must be after period start.'
        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def create(self, validated_data):
        return create_turnover(user=self.context['request'].user, **validated_data)


class VoidExpenseSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=1000, trim_whitespace=True, allow_blank=False)


class ReceiveTurnoverSerializer(serializers.Serializer):
    pass


class WalletFilterSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=Wallet.Kind.choices, required=False)
    is_active = serializers.BooleanField(required=False)


class DateRangeFilterSerializer(serializers.Serializer):
    start = serializers.DateField(required=False)
    end = serializers.DateField(required=False)

    def validate(self, attrs):
        if attrs.get('start') and attrs.get('end') and attrs['end'] < attrs['start']:
            raise serializers.ValidationError({'end': 'End date cannot be before start date.'})
        return attrs


class WalletTransactionFilterSerializer(DateRangeFilterSerializer):
    wallet = serializers.PrimaryKeyRelatedField(queryset=Wallet.objects.all(), required=False)
    entry_type = serializers.ChoiceField(choices=WalletTransaction.EntryType.choices, required=False)
    category = serializers.ChoiceField(choices=WalletTransaction.Category.choices, required=False)
    source_type = serializers.CharField(max_length=50, required=False, trim_whitespace=True)
    source_id = serializers.CharField(max_length=64, required=False, trim_whitespace=True)


class ExpenseFilterSerializer(DateRangeFilterSerializer):
    category = serializers.PrimaryKeyRelatedField(queryset=ExpenseCategory.objects.all(), required=False)
    wallet = serializers.PrimaryKeyRelatedField(queryset=Wallet.objects.all(), required=False)
    created_by = serializers.PrimaryKeyRelatedField(
        queryset=Expense._meta.get_field('created_by').remote_field.model.objects.all(), required=False
    )
    is_void = serializers.BooleanField(required=False)


class TurnoverFilterSerializer(DateRangeFilterSerializer):
    source_wallet = serializers.PrimaryKeyRelatedField(queryset=Wallet.objects.all(), required=False)
    destination_wallet = serializers.PrimaryKeyRelatedField(queryset=Wallet.objects.all(), required=False)
    status = serializers.ChoiceField(choices=Turnover.Status.choices, required=False)
