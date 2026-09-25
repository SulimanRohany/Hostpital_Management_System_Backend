from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from core.models import TimeStampedModel, UUIDModel


class ImmutableFinancialQuerySet(models.QuerySet):
    """Prevent bulk operations from bypassing per-instance ledger safeguards."""

    def update(self, **kwargs):
        raise ValidationError('Posted financial records cannot be updated in bulk.')

    def delete(self):
        raise ValidationError('Posted financial records cannot be deleted; create a reversal instead.')

    def bulk_create(self, objs, **kwargs):
        raise ValidationError('Financial records must be created through their posting service.')

    def bulk_update(self, objs, fields, **kwargs):
        raise ValidationError('Posted financial records cannot be updated in bulk.')


class WalletQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if 'balance' in kwargs:
            raise ValidationError('Wallet balances can only be changed by posting a ledger entry.')
        return super().update(**kwargs)

    def bulk_create(self, objs, **kwargs):
        for wallet in objs:
            wallet.full_clean()
        return super().bulk_create(objs, **kwargs)

    def bulk_update(self, objs, fields, **kwargs):
        if 'balance' in fields:
            raise ValidationError('Wallet balances can only be changed by posting a ledger entry.')
        return super().bulk_update(objs, fields, **kwargs)

    def delete(self):
        if self.exclude(kind=Wallet.Kind.CUSTOM).exists():
            raise ValidationError('System wallets cannot be deleted; deactivate them instead.')
        return super().delete()


class Wallet(UUIDModel, TimeStampedModel):
    class Kind(models.TextChoices):
        RECEPTION = 'reception', 'Reception'
        PHARMACY = 'pharmacy', 'Pharmacy'
        MANAGER = 'manager', 'Manager'
        CUSTOM = 'custom', 'Custom'

    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=120, unique=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.CUSTOM, db_index=True)
    balance = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal('0.00'))
    allow_negative = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True, db_index=True)

    objects = WalletQuerySet.as_manager()

    class Meta:
        ordering = ('name',)
        constraints = [
            models.UniqueConstraint(Lower('code'), name='unique_wallet_code_ci'),
            models.UniqueConstraint(Lower('name'), name='unique_wallet_name_ci'),
            models.UniqueConstraint(
                fields=('kind',), condition=~models.Q(kind='custom'), name='unique_system_wallet_kind'
            ),
            models.CheckConstraint(
                condition=models.Q(allow_negative=True) | models.Q(balance__gte=0),
                name='wallet_balance_respects_negative_policy',
            ),
        ]

    def clean(self):
        super().clean()
        self.code = (self.code or '').strip().upper()
        self.name = (self.name or '').strip()
        errors = {}
        if not self.code:
            errors['code'] = 'Wallet code cannot be blank.'
        if not self.name:
            errors['name'] = 'Wallet name cannot be blank.'
        previous = None
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                'code', 'name', 'kind', 'balance', 'allow_negative'
            ).first()
        if previous:
            if previous['balance'] != self.balance and not getattr(self, '_allow_balance_update', False):
                errors['balance'] = 'Wallet balances can only be changed by posting a ledger entry.'
            if previous['kind'] != self.Kind.CUSTOM:
                if previous['code'] != self.code:
                    errors['code'] = 'A system wallet code cannot be changed.'
                if previous['name'] != self.name:
                    errors['name'] = 'A system wallet name cannot be changed.'
                if previous['kind'] != self.kind:
                    errors['kind'] = 'A system wallet kind cannot be changed.'
                if previous['allow_negative'] != self.allow_negative:
                    errors['allow_negative'] = 'A system wallet negative-balance policy cannot be changed.'
            if previous['allow_negative'] and not self.allow_negative and self.balance < 0:
                errors['allow_negative'] = 'A wallet with a negative balance must continue to allow negative balances.'
        elif self.balance != Decimal('0.00'):
            errors['balance'] = 'A new wallet must start with a zero balance.'
        if not self.allow_negative and self.balance < 0:
            errors['balance'] = 'This wallet does not allow a negative balance.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        allow_balance_update = kwargs.pop('_allow_balance_update', False)
        self._allow_balance_update = allow_balance_update
        try:
            self.full_clean()
            return super().save(*args, **kwargs)
        finally:
            self._allow_balance_update = False

    def delete(self, *args, **kwargs):
        if self.kind != self.Kind.CUSTOM:
            raise ValidationError('System wallets cannot be deleted; deactivate them instead.')
        return super().delete(*args, **kwargs)

    def __str__(self):
        return self.name


class WalletTransaction(UUIDModel):
    class EntryType(models.TextChoices):
        CREDIT = 'credit', 'Credit'
        DEBIT = 'debit', 'Debit'

    class Category(models.TextChoices):
        RECEPTION_INCOME = 'reception_income', 'Reception income'
        RECEPTION_REFUND = 'reception_refund', 'Reception refund'
        PHARMACY_PURCHASE = 'pharmacy_purchase', 'Pharmacy purchase'
        PHARMACY_SALE = 'pharmacy_sale', 'Pharmacy sale'
        SUPPLIER_PAYMENT = 'supplier_payment', 'Supplier payment'
        PURCHASE_REVERSAL = 'purchase_reversal', 'Purchase reversal'
        SALE_REVERSAL = 'sale_reversal', 'Sale reversal'
        SUPPLIER_PAYMENT_REVERSAL = 'supplier_payment_reversal', 'Supplier payment reversal'
        EXPENSE = 'expense', 'Expense'
        EXPENSE_REVERSAL = 'expense_reversal', 'Expense reversal'
        TURNOVER = 'turnover', 'Turnover'

    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name='transactions')
    entry_type = models.CharField(max_length=10, choices=EntryType.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    balance_after = models.DecimalField(max_digits=16, decimal_places=2)
    transaction_date = models.DateTimeField(db_index=True)
    category = models.CharField(max_length=50, choices=Category.choices, db_index=True)
    description = models.CharField(max_length=255)
    reference = models.CharField(max_length=120, unique=True)
    source_type = models.CharField(max_length=50, blank=True, db_index=True)
    source_id = models.CharField(max_length=64, blank=True, db_index=True)
    reverses = models.OneToOneField(
        'self', null=True, blank=True, on_delete=models.PROTECT, related_name='reversal_entry'
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='wallet_entries')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = ImmutableFinancialQuerySet.as_manager()

    class Meta:
        ordering = ('-transaction_date', '-created_at')
        indexes = [models.Index(fields=('source_type', 'source_id'))]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name='wallet_transaction_amount_positive'),
            models.CheckConstraint(
                condition=(
                    models.Q(source_type='', source_id='')
                    | (~models.Q(source_type='') & ~models.Q(source_id=''))
                ),
                name='wallet_transaction_source_pair',
            ),
            models.CheckConstraint(
                condition=~models.Q(id=models.F('reverses_id')),
                name='wallet_transaction_not_self_reversal',
            ),
        ]

    def clean(self):
        super().clean()
        self.category = (self.category or '').strip().lower()
        self.description = (self.description or '').strip()
        self.reference = (self.reference or '').strip()
        self.source_type = (self.source_type or '').strip().lower()
        self.source_id = (self.source_id or '').strip()
        errors = {}
        if not self.description:
            errors['description'] = 'A transaction description is required.'
        if not self.reference:
            errors['reference'] = 'A transaction reference is required.'
        if bool(self.source_type) != bool(self.source_id):
            errors['source_type'] = 'Source type and source ID must be provided together.'
        if self.wallet_id:
            if not self.wallet.is_active:
                errors['wallet'] = 'Transactions cannot be posted to an inactive wallet.'
            if self.balance_after < 0 and not self.wallet.allow_negative:
                errors['balance_after'] = 'This wallet does not allow a negative balance.'
        if self.transaction_date and self.transaction_date > timezone.now() + timedelta(minutes=5):
            errors['transaction_date'] = 'Transaction date cannot be in the future.'
        if self.reverses_id:
            original = self.reverses
            if original.wallet_id != self.wallet_id:
                errors['reverses'] = 'A reversal must use the same wallet as the original entry.'
            if original.amount != self.amount:
                errors['amount'] = 'A reversal amount must equal the original entry amount.'
            if original.entry_type == self.entry_type:
                errors['entry_type'] = 'A reversal must use the opposite entry type.'
            if original.reverses_id:
                errors['reverses'] = 'A reversal entry cannot itself be reversed.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        allow_ledger_create = kwargs.pop('_allow_ledger_create', False)
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError('Wallet transactions are immutable.')
        if not allow_ledger_create:
            raise ValidationError('Wallet transactions must be created through the wallet posting service.')
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Wallet transactions cannot be deleted; post a reversal.')

    def __str__(self):
        return f'{self.wallet.code} {self.entry_type} {self.amount}'


class ExpenseCategory(UUIDModel, TimeStampedModel):
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ('name',)
        constraints = [models.UniqueConstraint(Lower('name'), name='unique_expense_category_name_ci')]

    def clean(self):
        super().clean()
        self.name = (self.name or '').strip()
        self.description = (self.description or '').strip()
        if not self.name:
            raise ValidationError({'name': 'Expense category name cannot be blank.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Expense(UUIDModel, TimeStampedModel):
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT, related_name='expenses')
    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name='expenses')
    expense_date = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    payee = models.CharField(max_length=150, blank=True)
    purpose = models.TextField()
    receipt_number = models.CharField(max_length=80, blank=True)
    attachment = models.FileField(upload_to='expenses/%Y/%m/', blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='expenses_created')
    is_void = models.BooleanField(default=False, db_index=True)
    void_reason = models.TextField(blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='expenses_voided'
    )
    voided_at = models.DateTimeField(null=True, blank=True)

    objects = ImmutableFinancialQuerySet.as_manager()

    class Meta:
        ordering = ('-expense_date', '-created_at')
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name='expense_amount_positive'),
            models.CheckConstraint(
                condition=(
                    models.Q(is_void=False, void_reason='', voided_by__isnull=True, voided_at__isnull=True)
                    | (
                        models.Q(is_void=True, voided_by__isnull=False, voided_at__isnull=False)
                        & ~models.Q(void_reason='')
                    )
                ),
                name='expense_void_metadata_consistent',
            ),
        ]

    def clean(self):
        super().clean()
        self.payee = (self.payee or '').strip()
        self.purpose = (self.purpose or '').strip()
        self.receipt_number = (self.receipt_number or '').strip()
        self.void_reason = (self.void_reason or '').strip()
        errors = {}
        if not self.purpose:
            errors['purpose'] = 'An expense purpose is required.'
        if self.expense_date and self.expense_date > timezone.localdate():
            errors['expense_date'] = 'Expense date cannot be in the future.'
        if self.category_id and not self.category.is_active:
            errors['category'] = 'Inactive expense categories cannot be used.'
        if self.wallet_id and not self.wallet.is_active:
            errors['wallet'] = 'Inactive wallets cannot be used.'
        void_metadata_complete = self.is_void and self.void_reason and self.voided_by_id and self.voided_at
        void_metadata_empty = not self.is_void and not self.void_reason and not self.voided_by_id and not self.voided_at
        if not (void_metadata_complete or void_metadata_empty):
            errors['is_void'] = 'Void status, reason, user, and timestamp must be recorded together.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        allow_create = kwargs.pop('_allow_financial_create', False)
        allow_void = kwargs.pop('_allow_void', False)
        exists = self.pk and type(self).objects.filter(pk=self.pk).exists()
        if exists and not allow_void:
            raise ValidationError('Posted expenses are immutable; void the expense instead.')
        if not exists and not allow_create:
            raise ValidationError('Expenses must be created through the expense posting service.')
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Expenses cannot be deleted; void the expense instead.')

    def void(self, *, user, reason, at=None):
        from .services import void_expense

        return void_expense(expense=self, user=user, reason=reason, at=at)

    def __str__(self):
        return f'{self.category}: {self.amount}'


class Turnover(UUIDModel):
    class Status(models.TextChoices):
        HANDED_OVER = 'handed_over', 'Handed over'
        RECEIVED = 'received', 'Received'

    source_wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name='turnovers_sent')
    destination_wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name='turnovers_received')
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    handed_over_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='turnovers_handed_over'
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.HANDED_OVER, db_index=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='turnovers_received'
    )
    received_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = ImmutableFinancialQuerySet.as_manager()

    class Meta:
        ordering = ('-created_at',)
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name='turnover_amount_positive'),
            models.CheckConstraint(condition=models.Q(period_end__gt=models.F('period_start')), name='turnover_period_valid'),
            models.CheckConstraint(condition=~models.Q(source_wallet=models.F('destination_wallet')), name='turnover_wallets_differ'),
            models.CheckConstraint(
                condition=(
                    models.Q(status='handed_over', received_by__isnull=True, received_at__isnull=True)
                    | models.Q(status='received', received_by__isnull=False, received_at__isnull=False)
                ),
                name='turnover_receipt_metadata_consistent',
            ),
            models.UniqueConstraint(
                fields=('source_wallet', 'period_start', 'period_end'), name='unique_turnover_source_period'
            ),
        ]

    def clean(self):
        super().clean()
        self.notes = (self.notes or '').strip()
        errors = {}
        if self.source_wallet_id and self.destination_wallet_id:
            if self.source_wallet_id == self.destination_wallet_id:
                errors['destination_wallet'] = 'Source and destination wallets must be different.'
            if not self.source_wallet.is_active or not self.destination_wallet.is_active:
                errors['source_wallet'] = 'Both wallets must be active.'
        if self.period_start and self.period_end:
            if self.period_end <= self.period_start:
                errors['period_end'] = 'Period end must be after period start.'
            if self.period_end > timezone.now() + timedelta(minutes=5):
                errors['period_end'] = 'Turnover period cannot end in the future.'
            if self.source_wallet_id:
                overlap = type(self).objects.filter(
                    source_wallet_id=self.source_wallet_id,
                    period_start__lt=self.period_end,
                    period_end__gt=self.period_start,
                ).exclude(pk=self.pk)
                if overlap.exists():
                    errors['period_start'] = 'A turnover for this wallet overlaps the selected period.'
        receipt_complete = self.status == self.Status.RECEIVED and self.received_by_id and self.received_at
        receipt_empty = self.status == self.Status.HANDED_OVER and not self.received_by_id and not self.received_at
        if not (receipt_complete or receipt_empty):
            errors['status'] = 'Receipt status, receiving user, and timestamp must be recorded together.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        allow_create = kwargs.pop('_allow_financial_create', False)
        allow_receive = kwargs.pop('_allow_receive', False)
        exists = self.pk and type(self).objects.filter(pk=self.pk).exists()
        if exists and not allow_receive:
            raise ValidationError('Posted turnovers are immutable.')
        if not exists and not allow_create:
            raise ValidationError('Turnovers must be created through the turnover posting service.')
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Turnovers cannot be deleted after posting.')

    def receive(self, *, user, at=None):
        from .services import receive_turnover

        return receive_turnover(turnover=self, user=user, at=at)

    def __str__(self):
        return f'{self.source_wallet} to {self.destination_wallet}: {self.amount}'
