from datetime import datetime, time
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from .models import Expense, Turnover, Wallet, WalletTransaction


SYSTEM_WALLETS = {
    Wallet.Kind.RECEPTION: ('RECEPTION', 'Reception Wallet', False),
    Wallet.Kind.PHARMACY: ('PHARMACY', 'Pharmacy Wallet', True),
    Wallet.Kind.MANAGER: ('MANAGER', 'Manager Wallet', False),
}
SYSTEM_WALLET_KINDS = tuple(SYSTEM_WALLETS)


def _api_validation_error(exc):
    if hasattr(exc, 'message_dict'):
        return serializers.ValidationError(exc.message_dict)
    return serializers.ValidationError(exc.messages)


def get_system_wallet(kind):
    try:
        code, name, allow_negative = SYSTEM_WALLETS[kind]
    except KeyError as exc:
        raise serializers.ValidationError({'kind': 'Unknown system wallet kind.'}) from exc
    wallet, _ = Wallet.objects.get_or_create(
        kind=kind,
        defaults={'code': code, 'name': name, 'allow_negative': allow_negative},
    )
    return wallet


@transaction.atomic
def post_wallet_entry(
    *, wallet, entry_type, amount, category, description, reference, user, source=None,
    transaction_date=None, reversal_of=None,
):
    try:
        amount = Decimal(amount)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise serializers.ValidationError({'amount': 'Enter a valid amount.'}) from exc
    if amount <= 0:
        raise serializers.ValidationError({'amount': 'Amount must be greater than zero.'})
    if entry_type not in WalletTransaction.EntryType.values:
        raise serializers.ValidationError({'entry_type': 'Unknown wallet entry type.'})
    if category not in WalletTransaction.Category.values:
        raise serializers.ValidationError({'category': 'Unknown wallet transaction category.'})

    normalized_reference = (reference or '').strip()
    if not normalized_reference:
        raise serializers.ValidationError({'reference': 'A transaction reference is required.'})
    existing = WalletTransaction.objects.filter(reference=normalized_reference).first()
    if existing:
        expected_source_type = source._meta.label_lower if source else ''
        expected_source_id = str(source.pk) if source else ''
        expected_reversal_id = reversal_of.pk if reversal_of else None
        same_entry = (
            existing.wallet_id == wallet.pk
            and existing.entry_type == entry_type
            and existing.amount == amount
            and existing.category == category
            and existing.source_type == expected_source_type
            and existing.source_id == expected_source_id
            and existing.reverses_id == expected_reversal_id
        )
        if not same_entry:
            raise serializers.ValidationError({'reference': 'This reference is already used by a different transaction.'})
        return existing

    locked = Wallet.objects.select_for_update().get(pk=wallet.pk)
    if not locked.is_active:
        raise serializers.ValidationError({'wallet': f'{locked.name} is inactive.'})
    new_balance = (
        locked.balance + amount
        if entry_type == WalletTransaction.EntryType.CREDIT
        else locked.balance - amount
    )
    if new_balance < 0 and not locked.allow_negative:
        raise serializers.ValidationError({'wallet': f'{locked.name} has insufficient balance.'})
    if reversal_of:
        reversal_of = WalletTransaction.objects.select_for_update().get(pk=reversal_of.pk)
        if hasattr(reversal_of, 'reversal_entry'):
            raise serializers.ValidationError({'reversal_of': 'This transaction has already been reversed.'})

    entry = WalletTransaction(
        wallet=locked,
        entry_type=entry_type,
        amount=amount,
        balance_after=new_balance,
        transaction_date=transaction_date or timezone.now(),
        category=category,
        description=description,
        reference=normalized_reference,
        source_type=source._meta.label_lower if source else '',
        source_id=str(source.pk) if source else '',
        reverses=reversal_of,
        created_by=user,
    )
    try:
        entry.save(_allow_ledger_create=True)
        locked.balance = new_balance
        locked.save(update_fields=('balance', 'updated_at'), _allow_balance_update=True)
    except DjangoValidationError as exc:
        raise _api_validation_error(exc) from exc
    return entry


@transaction.atomic
def create_expense(*, user, category, wallet, expense_date, amount, purpose, **extra_fields):
    expense = Expense(
        created_by=user,
        category=category,
        wallet=wallet,
        expense_date=expense_date,
        amount=amount,
        purpose=purpose,
        **extra_fields,
    )
    try:
        expense.save(_allow_financial_create=True)
    except DjangoValidationError as exc:
        raise _api_validation_error(exc) from exc
    post_wallet_entry(
        wallet=expense.wallet,
        entry_type=WalletTransaction.EntryType.DEBIT,
        amount=expense.amount,
        category=WalletTransaction.Category.EXPENSE,
        description=f'Expense: {expense.purpose[:180]}',
        reference=f'expense:{expense.pk}',
        user=user,
        source=expense,
        transaction_date=timezone.make_aware(datetime.combine(expense.expense_date, time.min)),
    )
    return expense


@transaction.atomic
def void_expense(*, expense, user, reason, at=None):
    locked = Expense.objects.select_for_update().get(pk=expense.pk)
    if locked.is_void:
        raise serializers.ValidationError({'detail': 'Expense is already void.'})
    reason = (reason or '').strip()
    if not reason:
        raise serializers.ValidationError({'reason': 'A void reason is required.'})
    original = WalletTransaction.objects.get(reference=f'expense:{locked.pk}')
    voided_at = at or timezone.now()
    post_wallet_entry(
        wallet=locked.wallet,
        entry_type=WalletTransaction.EntryType.CREDIT,
        amount=locked.amount,
        category=WalletTransaction.Category.EXPENSE_REVERSAL,
        description=f'Void expense: {reason[:180]}',
        reference=f'expense:{locked.pk}:void',
        user=user,
        source=locked,
        transaction_date=voided_at,
        reversal_of=original,
    )
    locked.is_void = True
    locked.void_reason = reason
    locked.voided_by = user
    locked.voided_at = voided_at
    try:
        locked.save(
            update_fields=('is_void', 'void_reason', 'voided_by', 'voided_at', 'updated_at'),
            _allow_void=True,
        )
    except DjangoValidationError as exc:
        raise _api_validation_error(exc) from exc
    return locked


@transaction.atomic
def create_turnover(
    *, user, source_wallet, destination_wallet, amount, period_start, period_end, notes='',
):
    wallet_ids = sorted((source_wallet.pk, destination_wallet.pk), key=str)
    locked_wallets = {
        wallet.pk: wallet
        for wallet in Wallet.objects.select_for_update().filter(pk__in=wallet_ids).order_by('pk')
    }
    source = locked_wallets[source_wallet.pk]
    destination = locked_wallets[destination_wallet.pk]
    turnover = Turnover(
        source_wallet=source,
        destination_wallet=destination,
        amount=amount,
        period_start=period_start,
        period_end=period_end,
        handed_over_by=user,
        notes=notes,
    )
    try:
        turnover.save(_allow_financial_create=True)
    except DjangoValidationError as exc:
        raise _api_validation_error(exc) from exc
    post_wallet_entry(
        wallet=source,
        entry_type=WalletTransaction.EntryType.DEBIT,
        amount=turnover.amount,
        category=WalletTransaction.Category.TURNOVER,
        description=f'Turnover to {destination.name}',
        reference=f'turnover:{turnover.pk}:debit',
        user=user,
        source=turnover,
    )
    post_wallet_entry(
        wallet=destination,
        entry_type=WalletTransaction.EntryType.CREDIT,
        amount=turnover.amount,
        category=WalletTransaction.Category.TURNOVER,
        description=f'Turnover from {source.name}',
        reference=f'turnover:{turnover.pk}:credit',
        user=user,
        source=turnover,
    )
    return turnover


@transaction.atomic
def receive_turnover(*, turnover, user, at=None):
    locked = Turnover.objects.select_for_update().get(pk=turnover.pk)
    if locked.status == Turnover.Status.RECEIVED:
        raise serializers.ValidationError({'detail': 'Turnover has already been received.'})
    locked.status = Turnover.Status.RECEIVED
    locked.received_by = user
    locked.received_at = at or timezone.now()
    try:
        locked.save(
            update_fields=('status', 'received_by', 'received_at'),
            _allow_receive=True,
        )
    except DjangoValidationError as exc:
        raise _api_validation_error(exc) from exc
    return locked
