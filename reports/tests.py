from datetime import timedelta
from decimal import Decimal
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from departments.models import Department
from patients.models import Patient
from pharmacy.models import Medicine, MedicineBatch, MedicineCategory, Sale, SaleLine, Supplier
from reception.models import Visit


class ReportsAPITests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='reports-admin', password='test-password', role=User.Role.ADMINISTRATOR,
            must_change_password=False,
        )
        self.receptionist = User.objects.create_user(
            username='reports-reception', password='test-password', role=User.Role.RECEPTION,
            must_change_password=False,
        )
        self.hr_user = User.objects.create_user(
            username='reports-hr', password='test-password', role=User.Role.HR,
            must_change_password=False,
        )
        self.client.force_authenticate(self.admin)
        self.patient = Patient.objects.create(first_name='Report', last_name='Patient', father_name='Parent')
        self.department = Department.objects.create(code='REPORT', name='Reports Department')
        self.category = MedicineCategory.objects.create(name='Report medicines')
        self.supplier = Supplier.objects.create(name='Report supplier')
        self.medicine = Medicine.objects.create(
            category=self.category, code='REP-MED', name='Report Medicine', reorder_level=Decimal('5.000'),
        )

    def make_batch(self, *, number='REP-B1', days=30, quantity='4.000', active=True):
        return MedicineBatch.objects.create(
            medicine=self.medicine, supplier=self.supplier, batch_number=number,
            expiry_date=timezone.localdate() + timedelta(days=days),
            purchase_price=Decimal('2.00'), sale_price=Decimal('5.00'),
            quantity_received=Decimal(quantity), quantity_available=Decimal(quantity), is_active=active,
        )

    def test_reception_report_uses_inclusive_dates_and_excludes_cancelled_visits(self):
        today = timezone.localdate()
        Visit.objects.create(
            patient=self.patient, department=self.department, visit_date=timezone.now(),
            total_amount=Decimal('100.00'), discount_amount=Decimal('10.00'),
            discount_reason='Assistance', paid_amount=Decimal('60.00'), created_by=self.admin,
        )
        Visit.objects.create(
            patient=self.patient, department=self.department, visit_date=timezone.now(),
            total_amount=Decimal('50.00'), status=Visit.Status.CANCELLED,
            cancelled_by=self.admin, cancelled_at=timezone.now(), cancellation_reason='Correction',
            created_by=self.admin,
        )

        result = self.client.get('/api/v1/reports/reception/', {'start': today, 'end': today})

        self.assertEqual(result.status_code, status.HTTP_200_OK, result.data)
        self.assertEqual(result.data['summary']['visits'], 1)
        self.assertEqual(result.data['summary']['patients'], 1)
        self.assertEqual(result.data['summary']['net'], Decimal('90.00'))
        self.assertEqual(result.data['summary']['outstanding'], Decimal('30.00'))

    def test_pharmacy_profit_is_margin_after_discount(self):
        batch = self.make_batch(quantity='10.000')
        sale = Sale.objects.create(
            sale_date=timezone.now(), subtotal=Decimal('10.00'), discount_amount=Decimal('1.00'),
            discount_reason='Promotion', total_amount=Decimal('9.00'), paid_amount=Decimal('9.00'),
            created_by=self.admin,
        )
        SaleLine.objects.create(
            sale=sale, batch=batch, quantity=Decimal('2.000'),
            unit_price=Decimal('5.00'), unit_cost=Decimal('2.00'),
        )

        result = self.client.get('/api/v1/reports/pharmacy/')

        self.assertEqual(result.status_code, status.HTTP_200_OK, result.data)
        self.assertEqual(result.data['profit'], Decimal('5.00'))
        self.assertEqual(result.data['sales_discounts'], Decimal('1.00'))

    def test_stock_report_separates_usable_near_expiry_and_expired_stock(self):
        self.make_batch(number='USABLE', days=120, quantity='4.000')
        self.make_batch(number='NEAR', days=10, quantity='1.000')
        self.make_batch(number='EXPIRED', days=-1, quantity='3.000')

        result = self.client.get('/api/v1/reports/stock/', {'expiry_days': 30})

        self.assertEqual(result.status_code, status.HTTP_200_OK, result.data)
        self.assertEqual(result.data['summary']['usable_batches'], 2)
        self.assertEqual(result.data['summary']['usable_quantity'], Decimal('5.000'))
        self.assertEqual(result.data['summary']['stock_value'], Decimal('10.00'))
        self.assertEqual(result.data['summary']['near_expiry_batches'], 1)
        self.assertEqual(result.data['summary']['expired_batches'], 1)
        self.assertEqual(result.data['summary']['low_stock_medicines'], 1)

    def test_invalid_filters_and_role_permissions_are_enforced(self):
        invalid_range = self.client.get('/api/v1/reports/reception/', {
            'start': '2026-09-20', 'end': '2026-09-19',
        })
        self.assertEqual(invalid_range.status_code, status.HTTP_400_BAD_REQUEST)

        invalid_expiry = self.client.get('/api/v1/reports/stock/', {'expiry_days': -1})
        self.assertEqual(invalid_expiry.status_code, status.HTTP_400_BAD_REQUEST)

        self.client.force_authenticate(self.receptionist)
        self.assertEqual(self.client.get('/api/v1/reports/reception/').status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get('/api/v1/reports/financial/').status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.hr_user)
        self.assertEqual(self.client.get('/api/v1/reports/income-trend/').status_code, status.HTTP_403_FORBIDDEN)

    def test_temporary_password_is_rejected_on_dashboard(self):
        temporary = User.objects.create_user(
            username='reports-temporary', password='test-password', role=User.Role.MANAGER,
            must_change_password=True,
        )
        self.client.force_authenticate(temporary)
        self.assertEqual(self.client.get('/api/v1/dashboard/').status_code, status.HTTP_403_FORBIDDEN)
