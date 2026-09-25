from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from departments.models import Department, Service
from finance.models import Wallet
from laboratory.models import LabOrder, LabOrderItem, LabTest
from patients.models import Patient
from pharmacy.models import Medicine, MedicineBatch, MedicineCategory, Supplier


class HospitalWorkflowTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='admin', password='Strong-Test-Password-123!', role=User.Role.ADMINISTRATOR,
            first_name='System', last_name='Admin', must_change_password=False,
        )
        self.client.force_authenticate(self.user)
        self.department = Department.objects.create(code='OPD', name='Outpatient')
        self.service = Service.objects.create(
            department=self.department, code='CONSULT', name='Consultation', standard_fee=Decimal('100.00'),
        )
        self.patient = Patient.objects.create(first_name='Ahmad', last_name='Khan', father_name='Karim')

    def test_reception_payment_posts_to_wallet_and_rejects_overpayment(self):
        payload = {
            'patient': str(self.patient.pk),
            'department': str(self.department.pk),
            'visit_date': timezone.now().isoformat(),
            'discount_amount': '10.00',
            'discount_reason': 'Approved assistance',
            'paid_amount': '90.00',
            'service_lines': [
                {'service': str(self.service.pk), 'quantity': 1, 'unit_price': '100.00'},
            ],
        }
        result = self.client.post('/api/v1/receptions/', payload, format='json')
        self.assertEqual(result.status_code, status.HTTP_201_CREATED, result.data)
        wallet = Wallet.objects.get(kind=Wallet.Kind.RECEPTION)
        self.assertEqual(wallet.balance, Decimal('90.00'))
        self.assertEqual(wallet.transactions.count(), 1)

        payload['paid_amount'] = '91.00'
        invalid = self.client.post('/api/v1/receptions/', payload, format='json')
        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)

    def test_pharmacy_purchase_sale_stock_due_and_wallet_are_consistent(self):
        category = MedicineCategory.objects.create(name='Antibiotics')
        supplier = Supplier.objects.create(name='Trusted Supplier')
        medicine = Medicine.objects.create(
            category=category, code='AMOX500', name='Amoxicillin', strength='500mg',
            dosage_form='capsule', default_sale_price=Decimal('10.00'),
        )
        purchase = self.client.post('/api/v1/purchases/', {
            'supplier': str(supplier.pk), 'invoice_number': 'INV-001',
            'purchase_date': timezone.localdate().isoformat(), 'paid_amount': '20.00',
            'lines': [{
                'medicine': str(medicine.pk), 'batch_number': 'B-001',
                'expiry_date': (timezone.localdate() + timedelta(days=365)).isoformat(),
                'quantity': '10.000', 'unit_cost': '5.00', 'sale_price': '10.00',
            }],
        }, format='json')
        self.assertEqual(purchase.status_code, status.HTTP_201_CREATED, purchase.data)
        batch = MedicineBatch.objects.get(batch_number='B-001')
        self.assertEqual(batch.quantity_available, Decimal('10.000'))
        supplier.refresh_from_db()
        self.assertEqual(supplier.amount_due, Decimal('30.00'))

        sale = self.client.post('/api/v1/sales/', {
            'patient': str(self.patient.pk), 'sale_date': timezone.now().isoformat(),
            'discount_amount': '0.00', 'paid_amount': '20.00',
            'lines': [{'batch': str(batch.pk), 'quantity': '2.000', 'unit_price': '10.00'}],
        }, format='json')
        self.assertEqual(sale.status_code, status.HTTP_201_CREATED, sale.data)
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_available, Decimal('8.000'))
        wallet = Wallet.objects.get(kind=Wallet.Kind.PHARMACY)
        self.assertEqual(wallet.balance, Decimal('0.00'))
        self.assertEqual(wallet.transactions.count(), 2)

        voided = self.client.post(f'/api/v1/sales/{sale.data["id"]}/void/', {'reason': 'Entry correction'}, format='json')
        self.assertEqual(voided.status_code, status.HTTP_200_OK, voided.data)
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_available, Decimal('10.000'))
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal('-20.00'))

        oversell = self.client.post('/api/v1/sales/', {
            'sale_date': timezone.now().isoformat(), 'paid_amount': '0.00',
            'lines': [{'batch': str(batch.pk), 'quantity': '11.000', 'unit_price': '10.00'}],
        }, format='json')
        self.assertEqual(oversell.status_code, status.HTTP_400_BAD_REQUEST)

    def test_laboratory_result_completes_order(self):
        lab_service = Service.objects.create(
            department=self.department, code='CBC', name='CBC Service',
            standard_fee=Decimal('20.00'), is_laboratory=True,
        )
        test = LabTest.objects.create(code='CBC', name='Complete Blood Count', service=lab_service, unit='g/dL')
        created = self.client.post('/api/v1/lab-orders/', {
            'patient': str(self.patient.pk), 'ordered_at': timezone.now().isoformat(),
            'clinical_notes': 'Routine test', 'items': [{'test': str(test.pk)}],
        }, format='json')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        order = LabOrder.objects.get(pk=created.data['id'])
        item = order.items.get()
        result = self.client.patch(
            f'/api/v1/lab-orders/{order.pk}/results/{item.pk}/',
            {'result': '13.5', 'result_unit': 'g/dL', 'reference_range': '12-16', 'is_abnormal': False},
            format='json',
        )
        self.assertEqual(result.status_code, status.HTTP_200_OK, result.data)
        order.refresh_from_db()
        self.assertEqual(order.status, LabOrder.Status.COMPLETED)

    def test_role_permission_blocks_reception_user_from_supplier_data(self):
        reception_user = User.objects.create_user(
            username='reception', password='Strong-Test-Password-123!', role=User.Role.RECEPTION,
        )
        self.client.force_authenticate(reception_user)
        result = self.client.get('/api/v1/suppliers/')
        self.assertEqual(result.status_code, status.HTTP_403_FORBIDDEN)

    def test_dashboard_returns_prd_summary_sections(self):
        result = self.client.get('/api/v1/dashboard/')
        self.assertEqual(result.status_code, status.HTTP_200_OK, result.data)
        self.assertEqual(set(result.data), {'as_of', 'today', 'overall', 'wallets', 'today_receptions'})
