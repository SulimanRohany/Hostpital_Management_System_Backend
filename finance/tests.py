from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError as APIValidationError
from rest_framework.test import APITestCase

from accounts.models import User

from .models import Expense, ExpenseCategory, Turnover, Wallet, WalletTransaction
from .services import create_expense, create_turnover, get_system_wallet, post_wallet_entry


class FinanceIntegrityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='finance-integrity', password='test-password', role=User.Role.FINANCE,
            must_change_password=False,
        )
        self.source = Wallet.objects.create(code=' cash ', name=' Main Cash ', allow_negative=False)
        self.destination = Wallet.objects.create(code='vault', name='Vault', allow_negative=False)
        self.category = ExpenseCategory.objects.create(name=' Supplies ', description='  General supplies  ')
        self.credit = post_wallet_entry(
            wallet=self.source,
            entry_type=WalletTransaction.EntryType.CREDIT,
            amount=Decimal('200.00'),
            category=WalletTransaction.Category.RECEPTION_INCOME,
            description='Opening receipt',
            reference='test:opening-credit',
            user=self.user,
        )

    def test_master_data_is_normalized_and_balance_is_ledger_controlled(self):
        self.source.refresh_from_db()
        self.category.refresh_from_db()
        self.assertEqual(self.source.code, 'CASH')
        self.assertEqual(self.source.name, 'Main Cash')
        self.assertEqual(self.category.name, 'Supplies')
        self.assertEqual(self.category.description, 'General supplies')

        self.source.balance = Decimal('999.00')
        with self.assertRaises(ValidationError):
            self.source.save()
        with self.assertRaises(ValidationError):
            Wallet.objects.filter(pk=self.source.pk).update(balance=Decimal('999.00'))

    def test_ledger_is_service_only_immutable_and_idempotent(self):
        direct = WalletTransaction(
            wallet=self.source,
            entry_type=WalletTransaction.EntryType.CREDIT,
            amount=Decimal('1.00'),
            balance_after=Decimal('201.00'),
            transaction_date=timezone.now(),
            category=WalletTransaction.Category.RECEPTION_INCOME,
            description='Bypass attempt',
            reference='test:bypass',
            created_by=self.user,
        )
        with self.assertRaises(ValidationError):
            direct.save()
        with self.assertRaises(ValidationError):
            WalletTransaction.objects.bulk_create([direct])
        with self.assertRaises(ValidationError):
            WalletTransaction.objects.filter(pk=self.credit.pk).update(description='Changed')
        with self.assertRaises(ValidationError):
            self.credit.delete()

        repeated = post_wallet_entry(
            wallet=self.source,
            entry_type=WalletTransaction.EntryType.CREDIT,
            amount=Decimal('200.00'),
            category=WalletTransaction.Category.RECEPTION_INCOME,
            description='Opening receipt retry',
            reference='test:opening-credit',
            user=self.user,
        )
        self.assertEqual(repeated.pk, self.credit.pk)
        with self.assertRaises(APIValidationError):
            post_wallet_entry(
                wallet=self.source,
                entry_type=WalletTransaction.EntryType.DEBIT,
                amount=Decimal('200.00'),
                category=WalletTransaction.Category.RECEPTION_REFUND,
                description='Conflicting retry',
                reference='test:opening-credit',
                user=self.user,
            )

    def test_inactive_wallet_and_unknown_category_are_rejected(self):
        self.destination.is_active = False
        self.destination.save(update_fields=('is_active', 'updated_at'))
        with self.assertRaises(APIValidationError):
            post_wallet_entry(
                wallet=self.destination,
                entry_type=WalletTransaction.EntryType.CREDIT,
                amount=Decimal('1.00'),
                category=WalletTransaction.Category.RECEPTION_INCOME,
                description='Invalid posting',
                reference='test:inactive',
                user=self.user,
            )
        with self.assertRaises(APIValidationError):
            post_wallet_entry(
                wallet=self.source,
                entry_type=WalletTransaction.EntryType.CREDIT,
                amount=Decimal('1.00'),
                category='free_form_category',
                description='Invalid category',
                reference='test:category',
                user=self.user,
            )

    def test_expense_is_atomic_immutable_and_voided_by_linked_reversal(self):
        expense = create_expense(
            user=self.user,
            category=self.category,
            wallet=self.source,
            expense_date=timezone.localdate(),
            amount=Decimal('30.00'),
            purpose='Clinical supplies',
        )
        self.source.refresh_from_db()
        self.assertEqual(self.source.balance, Decimal('170.00'))
        expense.amount = Decimal('40.00')
        with self.assertRaises(ValidationError):
            expense.save()
        with self.assertRaises(ValidationError):
            Expense.objects.filter(pk=expense.pk).delete()

        voided = expense.void(user=self.user, reason='Duplicate entry')
        self.source.refresh_from_db()
        self.assertEqual(self.source.balance, Decimal('200.00'))
        self.assertTrue(voided.is_void)
        self.assertEqual(voided.voided_by, self.user)
        self.assertIsNotNone(voided.voided_at)
        original = WalletTransaction.objects.get(reference=f'expense:{expense.pk}')
        reversal = WalletTransaction.objects.get(reference=f'expense:{expense.pk}:void')
        self.assertEqual(reversal.reverses, original)
        with self.assertRaises(APIValidationError):
            voided.void(user=self.user, reason='Again')

    def test_turnover_posts_both_entries_blocks_overlap_and_tracks_receipt(self):
        now = timezone.now()
        turnover = create_turnover(
            user=self.user,
            source_wallet=self.source,
            destination_wallet=self.destination,
            amount=Decimal('50.00'),
            period_start=now - timedelta(hours=2),
            period_end=now - timedelta(hours=1),
            notes='Shift handover',
        )
        self.source.refresh_from_db()
        self.destination.refresh_from_db()
        self.assertEqual(self.source.balance, Decimal('150.00'))
        self.assertEqual(self.destination.balance, Decimal('50.00'))
        self.assertEqual(
            WalletTransaction.objects.filter(source_id=str(turnover.pk), category='turnover').count(), 2
        )

        with self.assertRaises(APIValidationError):
            create_turnover(
                user=self.user,
                source_wallet=self.source,
                destination_wallet=self.destination,
                amount=Decimal('10.00'),
                period_start=now - timedelta(minutes=90),
                period_end=now - timedelta(minutes=30),
            )

        received = turnover.receive(user=self.user)
        self.assertEqual(received.status, Turnover.Status.RECEIVED)
        self.assertEqual(received.received_by, self.user)
        self.assertIsNotNone(received.received_at)
        received.notes = 'Changed'
        with self.assertRaises(ValidationError):
            received.save()
        with self.assertRaises(APIValidationError):
            received.receive(user=self.user)


class FinanceAPITests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='finance-api-admin', password='test-password', role=User.Role.ADMINISTRATOR,
            must_change_password=False,
        )
        self.client.force_authenticate(self.admin)
        self.source = Wallet.objects.create(code='API-CASH', name='API Cash')
        self.destination = Wallet.objects.create(code='API-VAULT', name='API Vault')
        self.category = ExpenseCategory.objects.create(name='API Supplies')
        post_wallet_entry(
            wallet=self.source,
            entry_type=WalletTransaction.EntryType.CREDIT,
            amount=Decimal('300.00'),
            category=WalletTransaction.Category.RECEPTION_INCOME,
            description='API test funding',
            reference='api:test:funding',
            user=self.admin,
        )

    def create_expense(self, amount='25.00'):
        return self.client.post('/api/v1/expenses/', {
            'category': str(self.category.pk),
            'wallet': str(self.source.pk),
            'expense_date': timezone.localdate().isoformat(),
            'amount': amount,
            'purpose': 'API test supplies',
        }, format='json')

    def test_expense_create_filter_void_and_protected_category_delete(self):
        created = self.create_expense()
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertEqual(created.data['created_by_name'], self.admin.display_name)

        invalid_date = self.client.get('/api/v1/expenses/?start=not-a-date')
        self.assertEqual(invalid_date.status_code, status.HTTP_400_BAD_REQUEST, invalid_date.data)
        reversed_range = self.client.get('/api/v1/expenses/?start=2026-09-20&end=2026-09-01')
        self.assertEqual(reversed_range.status_code, status.HTTP_400_BAD_REQUEST, reversed_range.data)

        missing_reason = self.client.post(f'/api/v1/expenses/{created.data["id"]}/void/', {}, format='json')
        self.assertEqual(missing_reason.status_code, status.HTTP_400_BAD_REQUEST, missing_reason.data)
        voided = self.client.post(
            f'/api/v1/expenses/{created.data["id"]}/void/', {'reason': 'Duplicate'}, format='json'
        )
        self.assertEqual(voided.status_code, status.HTTP_200_OK, voided.data)
        self.assertTrue(voided.data['is_void'])
        self.assertEqual(voided.data['voided_by_name'], self.admin.display_name)

        category_delete = self.client.delete(f'/api/v1/expense-categories/{self.category.pk}/')
        self.assertEqual(category_delete.status_code, status.HTTP_400_BAD_REQUEST, category_delete.data)

    def test_turnover_create_receive_and_validated_filters(self):
        now = timezone.now()
        created = self.client.post('/api/v1/turnovers/', {
            'source_wallet': str(self.source.pk),
            'destination_wallet': str(self.destination.pk),
            'amount': '40.00',
            'period_start': (now - timedelta(hours=2)).isoformat(),
            'period_end': (now - timedelta(hours=1)).isoformat(),
            'notes': 'API shift handover',
        }, format='json')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertEqual(created.data['status'], Turnover.Status.HANDED_OVER)

        received = self.client.post(f'/api/v1/turnovers/{created.data["id"]}/receive/', {}, format='json')
        self.assertEqual(received.status_code, status.HTTP_200_OK, received.data)
        self.assertEqual(received.data['status'], Turnover.Status.RECEIVED)
        self.assertEqual(received.data['received_by_name'], self.admin.display_name)

        invalid_status = self.client.get('/api/v1/turnovers/?status=unknown')
        self.assertEqual(invalid_status.status_code, status.HTTP_400_BAD_REQUEST, invalid_status.data)
        invalid_wallet = self.client.get('/api/v1/wallet-transactions/?wallet=not-a-uuid')
        self.assertEqual(invalid_wallet.status_code, status.HTTP_400_BAD_REQUEST, invalid_wallet.data)

    def test_system_wallet_creation_is_reserved_and_pharmacy_visibility_is_scoped(self):
        reserved = self.client.post('/api/v1/wallets/', {
            'code': 'SECOND-PHARMACY',
            'name': 'Second Pharmacy',
            'kind': Wallet.Kind.PHARMACY,
        }, format='json')
        self.assertEqual(reserved.status_code, status.HTTP_400_BAD_REQUEST, reserved.data)

        pharmacy_wallet = get_system_wallet(Wallet.Kind.PHARMACY)
        self.assertEqual(pharmacy_wallet.kind, Wallet.Kind.PHARMACY)
        self.assertEqual(Wallet.objects.filter(kind=Wallet.Kind.PHARMACY).count(), 1)
        pharmacist = User.objects.create_user(
            username='finance-api-pharmacist', password='test-password', role=User.Role.PHARMACY,
            must_change_password=False,
        )
        self.client.force_authenticate(pharmacist)
        wallets = self.client.get('/api/v1/wallets/')
        self.assertEqual(wallets.status_code, status.HTTP_200_OK, wallets.data)
        results = wallets.data.get('results', wallets.data)
        self.assertEqual([item['id'] for item in results], [str(pharmacy_wallet.pk)], wallets.data)

    def test_wallet_list_provisions_visible_system_wallets(self):
        self.assertFalse(Wallet.objects.filter(kind=Wallet.Kind.PHARMACY).exists())
        pharmacist = User.objects.create_user(
            username='finance-api-pharmacy-wallet-list', password='test-password', role=User.Role.PHARMACY,
            must_change_password=False,
        )
        self.client.force_authenticate(pharmacist)

        wallets = self.client.get('/api/v1/wallets/')

        self.assertEqual(wallets.status_code, status.HTTP_200_OK, wallets.data)
        results = wallets.data.get('results', wallets.data)
        self.assertEqual(len(results), 1, wallets.data)
        self.assertEqual(results[0]['kind'], Wallet.Kind.PHARMACY)
        self.assertTrue(results[0]['is_active'])

    def test_finance_wallet_list_provisions_only_system_wallets(self):
        wallets = self.client.get('/api/v1/wallets/?ordering=name')

        self.assertEqual(wallets.status_code, status.HTTP_200_OK, wallets.data)
        self.assertEqual(
            set(Wallet.objects.exclude(kind=Wallet.Kind.CUSTOM).values_list('kind', flat=True)),
            {Wallet.Kind.RECEPTION, Wallet.Kind.PHARMACY, Wallet.Kind.MANAGER},
        )

    def test_custom_wallet_filter_does_not_provision_a_system_wallet(self):
        system_wallet_count = Wallet.objects.exclude(kind=Wallet.Kind.CUSTOM).count()

        wallets = self.client.get(f'/api/v1/wallets/?kind={Wallet.Kind.CUSTOM}')

        self.assertEqual(wallets.status_code, status.HTTP_200_OK, wallets.data)
        self.assertEqual(
            Wallet.objects.exclude(kind=Wallet.Kind.CUSTOM).count(),
            system_wallet_count,
        )
