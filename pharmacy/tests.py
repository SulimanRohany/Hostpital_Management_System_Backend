from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from patients.models import Patient

from finance.services import get_system_wallet

from .models import Medicine, MedicineBatch, MedicineCategory, Purchase, PurchaseLine, Sale, Supplier, SupplierPayment


class PharmacyModelIntegrityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='pharmacist', password='test-password')
        self.category = MedicineCategory.objects.create(name='General')
        self.supplier = Supplier.objects.create(name='Supplier A')
        self.medicine = Medicine.objects.create(category=self.category, code='MED-1', name='Medicine')

    def make_batch(self, **overrides):
        values = {
            'medicine': self.medicine,
            'supplier': self.supplier,
            'batch_number': 'B-1',
            'expiry_date': timezone.localdate() + timedelta(days=30),
            'purchase_price': Decimal('5'),
            'sale_price': Decimal('8'),
            'quantity_received': Decimal('10'),
            'quantity_available': Decimal('10'),
        }
        values.update(overrides)
        return MedicineBatch.objects.create(**values)

    def test_usable_stock_excludes_expired_and_inactive_batches(self):
        self.make_batch()
        self.make_batch(batch_number='B-2', expiry_date=timezone.localdate() - timedelta(days=1))
        self.make_batch(batch_number='B-3', is_active=False)
        self.assertEqual(self.medicine.physical_stock, Decimal('30'))
        self.assertEqual(self.medicine.usable_stock, Decimal('10'))
        self.assertEqual(self.medicine.expired_stock, Decimal('10'))

    def test_posted_batch_identity_and_purchase_lines_are_immutable(self):
        batch = self.make_batch()
        batch.purchase_price = Decimal('6')
        with self.assertRaises(ValidationError):
            batch.save()

        purchase = Purchase.objects.create(
            supplier=self.supplier, invoice_number='INV-1', purchase_date=timezone.localdate(),
            total_amount=Decimal('50'), created_by=self.user,
        )
        line = PurchaseLine.objects.create(
            purchase=purchase, medicine=self.medicine, batch=batch,
            quantity=Decimal('10'), unit_cost=Decimal('5'),
        )
        line.unit_cost = Decimal('4')
        with self.assertRaises(ValidationError):
            line.save()
        with self.assertRaises(ValidationError):
            line.delete()

    def test_purchase_line_rejects_a_different_medicine_than_its_batch(self):
        batch = self.make_batch()
        other = Medicine.objects.create(category=self.category, code='MED-2', name='Other')
        purchase = Purchase.objects.create(
            supplier=self.supplier, invoice_number='INV-2', purchase_date=timezone.localdate(),
            total_amount=Decimal('50'), created_by=self.user,
        )
        with self.assertRaises(ValidationError):
            PurchaseLine.objects.create(
                purchase=purchase, medicine=other, batch=batch,
                quantity=Decimal('10'), unit_cost=Decimal('5'),
            )


class PrescriptionSaleTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='admin-pharmacy', password='test-password', role=User.Role.ADMINISTRATOR,
            must_change_password=False,
        )
        self.client.force_authenticate(self.user)
        category = MedicineCategory.objects.create(name='Controlled')
        supplier = Supplier.objects.create(name='Supplier Rx')
        self.patient = Patient.objects.create(first_name='Test', last_name='Patient', father_name='Parent')
        medicine = Medicine.objects.create(
            category=category, code='RX-1', name='Prescription medicine', requires_prescription=True,
        )
        purchase = self.client.post('/api/v1/purchases/', {
            'supplier': str(supplier.pk), 'invoice_number': 'RX-INV-1',
            'purchase_date': timezone.localdate().isoformat(), 'paid_amount': '0.00',
            'lines': [{
                'medicine': str(medicine.pk), 'batch_number': 'RX-B-1',
                'expiry_date': (timezone.localdate() + timedelta(days=90)).isoformat(),
                'quantity': '10.000', 'unit_cost': '2.00', 'sale_price': '4.00',
            }],
        }, format='json')
        self.assertEqual(purchase.status_code, status.HTTP_201_CREATED, purchase.data)
        self.batch = MedicineBatch.objects.get(batch_number='RX-B-1')

    def sale_payload(self):
        return {
            'patient': str(self.patient.pk), 'sale_date': timezone.now().isoformat(),
            'discount_amount': '0.00', 'paid_amount': '4.00',
            'lines': [{'batch': str(self.batch.pk), 'quantity': '1.000', 'unit_price': '4.00'}],
        }

    def test_prescription_medicine_requires_reference(self):
        response = self.client.post('/api/v1/sales/', self.sale_payload(), format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('prescription_reference', response.data['errors'])

        payload = self.sale_payload()
        payload['prescription_reference'] = 'RX-2026-001'
        response = self.client.post('/api/v1/sales/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


class PharmacyApiHardeningTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='pharmacy-admin', password='test-password', role=User.Role.ADMINISTRATOR,
            must_change_password=False,
        )
        self.pharmacist = User.objects.create_user(
            username='pharmacist-api', password='test-password', role=User.Role.PHARMACY,
            must_change_password=False,
        )
        self.category = MedicineCategory.objects.create(name='API medicines')
        self.supplier = Supplier.objects.create(name='API supplier')
        self.medicine = Medicine.objects.create(
            category=self.category, code='API-MED-1', name='API Medicine', reorder_level=Decimal('3'),
        )
        self.batch = MedicineBatch.objects.create(
            medicine=self.medicine, supplier=self.supplier, batch_number='API-B-1',
            expiry_date=timezone.localdate() + timedelta(days=20), purchase_price=Decimal('2'),
            sale_price=Decimal('5'), quantity_received=Decimal('10'), quantity_available=Decimal('10'),
        )

    def sale_payload(self, unit_price='5.00'):
        return {
            'sale_date': timezone.now().isoformat(), 'discount_amount': '0.00',
            'paid_amount': unit_price,
            'lines': [{'batch': str(self.batch.pk), 'quantity': '1.000', 'unit_price': unit_price}],
        }

    def test_pharmacist_cannot_override_price_or_see_cost_and_profit(self):
        self.client.force_authenticate(self.pharmacist)
        rejected = self.client.post('/api/v1/sales/', self.sale_payload('4.00'), format='json')
        self.assertEqual(rejected.status_code, status.HTTP_400_BAD_REQUEST, rejected.data)

        created = self.client.post('/api/v1/sales/', self.sale_payload(), format='json')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertNotIn('profit', created.data)
        self.assertNotIn('unit_cost', created.data['lines'][0])

    def test_duplicate_batch_is_rejected_case_insensitively(self):
        self.client.force_authenticate(self.admin)
        result = self.client.post('/api/v1/purchases/', {
            'supplier': str(self.supplier.pk), 'invoice_number': 'API-INV-2',
            'purchase_date': timezone.localdate().isoformat(), 'paid_amount': '0.00',
            'lines': [{
                'medicine': str(self.medicine.pk), 'batch_number': 'api-b-1',
                'expiry_date': (timezone.localdate() + timedelta(days=60)).isoformat(),
                'quantity': '1.000', 'unit_cost': '2.00', 'sale_price': '5.00',
            }],
        }, format='json')
        self.assertEqual(result.status_code, status.HTTP_400_BAD_REQUEST, result.data)

    def test_fefo_allocation_uses_earliest_expiry_first(self):
        earlier = MedicineBatch.objects.create(
            medicine=self.medicine, supplier=self.supplier, batch_number='API-B-EARLY',
            expiry_date=timezone.localdate() + timedelta(days=5), purchase_price=Decimal('2'),
            sale_price=Decimal('5'), quantity_received=Decimal('2'), quantity_available=Decimal('2'),
        )
        self.client.force_authenticate(self.pharmacist)
        result = self.client.post('/api/v1/sales/fefo-allocation/', {
            'medicine': str(self.medicine.pk), 'quantity': '3.000',
        }, format='json')
        self.assertEqual(result.status_code, status.HTTP_200_OK, result.data)
        self.assertEqual(result.data['allocations'][0]['batch'], str(earlier.pk))
        self.assertEqual(result.data['allocations'][0]['quantity'], Decimal('2'))

    def test_sensitive_actions_require_their_specific_roles(self):
        self.client.force_authenticate(self.pharmacist)
        adjustment = self.client.post('/api/v1/stock-movements/adjust/', {
            'batch': str(self.batch.pk), 'quantity_change': '-1.000',
            'category': 'damage', 'reason': 'Broken package',
        }, format='json')
        self.assertEqual(adjustment.status_code, status.HTTP_403_FORBIDDEN)

        sale = Sale.objects.create(
            sale_date=timezone.now(), subtotal=Decimal('0'), total_amount=Decimal('0'),
            paid_amount=Decimal('0'), created_by=self.admin,
        )
        voided = self.client.post(f'/api/v1/sales/{sale.pk}/void/', {'reason': 'Correction'}, format='json')
        self.assertEqual(voided.status_code, status.HTTP_403_FORBIDDEN)

    def test_low_stock_endpoint_is_paginated_and_uses_usable_stock(self):
        self.batch.expiry_date = timezone.localdate() - timedelta(days=1)
        self.batch.save(update_fields=('expiry_date', 'updated_at'))
        self.client.force_authenticate(self.pharmacist)
        result = self.client.get('/api/v1/medicines/low-stock/')
        self.assertEqual(result.status_code, status.HTTP_200_OK, result.data)
        self.assertIn('results', result.data)
        self.assertEqual(result.data['results'][0]['stock_quantity'], '0.000')

    def test_supplier_payment_cannot_exceed_supplier_balance_when_allocated_to_purchase(self):
        self.client.force_authenticate(self.admin)
        purchase = Purchase.objects.create(
            supplier=self.supplier,
            invoice_number='PAYMENT-LIMIT-1',
            purchase_date=timezone.localdate(),
            total_amount=Decimal('100.00'),
            paid_amount=Decimal('0.00'),
            created_by=self.admin,
        )
        wallet = get_system_wallet('pharmacy')

        first = self.client.post('/api/v1/supplier-payments/', {
            'supplier': str(self.supplier.pk),
            'wallet': str(wallet.pk),
            'payment_date': timezone.localdate().isoformat(),
            'amount': '60.00',
        }, format='json')
        self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.data)

        overpayment = self.client.post('/api/v1/supplier-payments/', {
            'supplier': str(self.supplier.pk),
            'purchase': str(purchase.pk),
            'wallet': str(wallet.pk),
            'payment_date': timezone.localdate().isoformat(),
            'amount': '50.00',
        }, format='json')

        self.assertEqual(overpayment.status_code, status.HTTP_400_BAD_REQUEST, overpayment.data)
        self.assertIn('amount', overpayment.data['errors'])
        self.assertEqual(SupplierPayment.objects.filter(is_void=False).count(), 1)
        self.assertEqual(self.supplier.amount_due, Decimal('40.00'))
