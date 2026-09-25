from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from departments.models import Department, Service
from laboratory.models import LabOrder, LabOrderItem, LabTest
from patients.models import Patient
from reception.models import Visit


class LaboratoryDomainTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='lab-domain', password='Strong-Test-Password-123!',
            role=User.Role.LABORATORY, must_change_password=False,
        )
        self.patient = Patient.objects.create(first_name='Lab', last_name='Patient')
        self.department = Department.objects.create(code='LAB-DOMAIN', name='Laboratory Domain')
        self.service = Service.objects.create(
            department=self.department, code='LAB-CBC', name='CBC service',
            standard_fee=Decimal('20.00'), is_laboratory=True,
        )
        self.test = LabTest.objects.create(
            code=' cbc ', name=' Complete Blood Count ', service=self.service,
            specimen_type=' Whole blood ', unit=' g/dL ', reference_range=' 12-16 ',
        )

    def create_order(self, *, tests=None, visit=None):
        return LabOrder.create_with_items(
            patient=self.patient, visit=visit, ordered_by=self.user, ordered_at=timezone.now(),
            items=[{'test': test} for test in (tests or [self.test])],
        )

    def test_test_catalog_normalizes_and_requires_active_lab_service(self):
        self.assertEqual(self.test.code, 'CBC')
        self.assertEqual(self.test.name, 'Complete Blood Count')
        invalid_service = Service.objects.create(
            department=self.department, code='NOT-LAB', name='Not laboratory', is_laboratory=False,
        )
        with self.assertRaises(ValidationError):
            LabTest.objects.create(code='BAD', name='Bad test', service=invalid_service)
        inactive_service = Service.objects.create(
            department=self.department, code='INACTIVE-LAB', name='Inactive laboratory service', is_laboratory=True,
        )
        inactive_service.deactivate()
        with self.assertRaises(ValidationError):
            LabTest.objects.create(code='INACTIVE', name='Inactive service test', service=inactive_service)

    def test_order_validates_patient_visit_time_and_requires_items(self):
        other_patient = Patient.objects.create(first_name='Other')
        visit = Visit.objects.create(
            patient=other_patient, department=self.department, visit_date=timezone.now(),
            created_by=self.user,
        )
        with self.assertRaises(ValidationError):
            self.create_order(visit=visit)
        with self.assertRaises(ValidationError):
            LabOrder.create_with_items(
                patient=self.patient, ordered_by=self.user, ordered_at=timezone.now(), items=[],
            )
        with self.assertRaises(ValidationError):
            LabOrder.create_with_items(
                patient=self.patient, ordered_by=self.user,
                ordered_at=timezone.now() + timedelta(minutes=10), items=[{'test': self.test}],
            )

    def test_order_item_snapshots_test_catalog_values(self):
        item = self.create_order().items.get()
        self.assertEqual(item.test_code, 'CBC')
        self.assertEqual(item.test_name, 'Complete Blood Count')
        self.assertEqual(item.specimen_type, 'Whole blood')
        self.assertEqual(item.result_unit, 'g/dL')
        self.assertEqual(item.reference_range, '12-16')
        self.test.name = 'Renamed CBC'
        self.test.save()
        item.refresh_from_db()
        self.assertEqual(item.test_name, 'Complete Blood Count')

    def test_collection_and_results_follow_valid_atomic_transitions(self):
        second = LabTest.objects.create(code='WBC', name='White Blood Cells', service=None, unit='10^9/L')
        order = self.create_order(tests=[self.test, second])
        order = order.collect(user=self.user)
        self.assertEqual(order.status, LabOrder.Status.COLLECTED)
        first_item = order.items.get(test=self.test)
        first_item.record_result(user=self.user, result='13.5', is_abnormal=False)
        order.refresh_from_db()
        self.assertEqual(order.status, LabOrder.Status.IN_PROGRESS)
        order.items.get(test=second).record_result(user=self.user, result='18', is_abnormal=True)
        order.refresh_from_db()
        self.assertEqual(order.status, LabOrder.Status.COMPLETED)
        with self.assertRaises(ValidationError):
            first_item.record_result(user=self.user, result='14')
        with self.assertRaises(ValidationError):
            order.cancel(user=self.user, reason='Too late')

    def test_result_entry_from_ordered_state_records_collection_audit(self):
        order = self.create_order()
        order.items.get().record_result(user=self.user, result='13')
        order.refresh_from_db()
        self.assertEqual(order.status, LabOrder.Status.COMPLETED)
        self.assertEqual(order.collected_by, self.user)
        self.assertIsNotNone(order.collected_at)

    def test_cancellation_requires_reason_and_is_terminal(self):
        order = self.create_order()
        with self.assertRaises(ValidationError):
            order.cancel(user=self.user, reason='  ')
        order = order.cancel(user=self.user, reason='Duplicate request')
        self.assertEqual(order.status, LabOrder.Status.CANCELLED)
        self.assertIsNotNone(order.cancelled_at)
        with self.assertRaises(ValidationError):
            order.collect(user=self.user)
        with self.assertRaises(ValidationError):
            order.items.get().record_result(user=self.user, result='13')

    def test_processed_orders_and_items_are_immutable(self):
        order = self.create_order().collect(user=self.user)
        order.patient = Patient.objects.create(first_name='Replacement')
        with self.assertRaises(ValidationError):
            order.save()
        item = order.items.get()
        item.test = LabTest.objects.create(code='ALT', name='Alternative')
        with self.assertRaises(ValidationError):
            item.save()
        with self.assertRaises(ValidationError):
            item.delete()

    def test_database_constraints_reject_inconsistent_audit_state(self):
        order = self.create_order()
        with self.assertRaises(IntegrityError), transaction.atomic():
            LabOrder.objects.filter(pk=order.pk).update(
                status=LabOrder.Status.CANCELLED, cancellation_reason='Missing actor and time'
            )
        item = order.items.get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            LabOrderItem.objects.filter(pk=item.pk).update(result='13')


class LaboratoryAPITests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='lab-api-admin', password='Strong-Test-Password-123!',
            role=User.Role.ADMINISTRATOR, must_change_password=False,
        )
        self.lab_user = User.objects.create_user(
            username='lab-api-user', password='Strong-Test-Password-123!',
            role=User.Role.LABORATORY, must_change_password=False,
        )
        self.reception = User.objects.create_user(
            username='lab-api-reception', password='Strong-Test-Password-123!',
            role=User.Role.RECEPTION, must_change_password=False,
        )
        self.patient = Patient.objects.create(first_name='API', last_name='Patient')
        self.department = Department.objects.create(code='LAB-API', name='Laboratory API')
        self.service = Service.objects.create(
            department=self.department, code='LAB-API-CBC', name='API CBC service',
            standard_fee=Decimal('20.00'), is_laboratory=True,
        )
        self.test = LabTest.objects.create(code='CBC-API', name='API Complete Blood Count', service=self.service)
        self.second_test = LabTest.objects.create(code='WBC-API', name='API White Blood Cells')
        self.client.force_authenticate(self.admin)

    def create_order(self, tests=None):
        result = self.client.post('/api/v1/lab-orders/', {
            'patient': str(self.patient.pk),
            'ordered_at': timezone.now().isoformat(),
            'items': [{'test': str(test.pk)} for test in (tests or [self.test])],
        }, format='json')
        self.assertEqual(result.status_code, status.HTTP_201_CREATED, result.data)
        return LabOrder.objects.get(pk=result.data['id'])

    def test_result_response_contains_recalculated_status_and_invalid_item_is_404(self):
        order = self.create_order()
        item = order.items.get()
        result = self.client.patch(
            f'/api/v1/lab-orders/{order.pk}/results/{item.pk}/', {'result': '13.5'}, format='json',
        )
        self.assertEqual(result.status_code, status.HTTP_200_OK, result.data)
        self.assertEqual(result.data['status'], LabOrder.Status.COMPLETED)
        self.assertIsNotNone(result.data['collected_at'])

        missing = self.client.patch(
            f'/api/v1/lab-orders/{order.pk}/results/{self.patient.pk}/', {'result': '14'}, format='json',
        )
        self.assertEqual(missing.status_code, status.HTTP_404_NOT_FOUND)

    def test_invalid_filters_return_400_and_date_range_is_supported(self):
        invalid_uuid = self.client.get('/api/v1/lab-orders/?patient=not-a-uuid')
        self.assertEqual(invalid_uuid.status_code, status.HTTP_400_BAD_REQUEST)
        invalid_status = self.client.get('/api/v1/lab-orders/?status=unknown')
        self.assertEqual(invalid_status.status_code, status.HTTP_400_BAD_REQUEST)
        reversed_range = self.client.get('/api/v1/lab-orders/?date_from=2026-02-02&date_to=2026-02-01')
        self.assertEqual(reversed_range.status_code, status.HTTP_400_BAD_REQUEST)

    def test_patch_rejects_items_and_processed_orders(self):
        order = self.create_order()
        result = self.client.patch(
            f'/api/v1/lab-orders/{order.pk}/', {'items': [{'test': str(self.second_test.pk)}]}, format='json',
        )
        self.assertEqual(result.status_code, status.HTTP_400_BAD_REQUEST)
        order.collect(user=self.admin)
        result = self.client.patch(
            f'/api/v1/lab-orders/{order.pk}/', {'clinical_notes': 'Changed'}, format='json',
        )
        self.assertEqual(result.status_code, status.HTTP_400_BAD_REQUEST)

    def test_result_correction_requires_reason_and_is_audited(self):
        order = self.create_order([self.test, self.second_test])
        item = order.items.get(test=self.test)
        url = f'/api/v1/lab-orders/{order.pk}/results/{item.pk}/'
        first = self.client.patch(url, {'result': '13'}, format='json')
        self.assertEqual(first.status_code, status.HTTP_200_OK, first.data)
        missing_reason = self.client.patch(url, {'result': '14'}, format='json')
        self.assertEqual(missing_reason.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('A reason is required when correcting an existing result.', str(missing_reason.data))
        blank_reason = self.client.patch(url, {'result': '14', 'correction_reason': '   '}, format='json')
        self.assertEqual(blank_reason.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('A reason is required when correcting an existing result.', str(blank_reason.data))
        corrected = self.client.patch(
            url, {'result': '14', 'correction_reason': 'Instrument calibration'}, format='json',
        )
        self.assertEqual(corrected.status_code, status.HTTP_200_OK, corrected.data)

    def test_catalog_normalizes_duplicates_and_protected_delete_is_friendly(self):
        duplicate = self.client.post('/api/v1/lab-tests/', {
            'code': ' cbc-api ', 'name': 'Different name',
        }, format='json')
        self.assertEqual(duplicate.status_code, status.HTTP_400_BAD_REQUEST)
        self.create_order()
        deleted = self.client.delete(f'/api/v1/lab-tests/{self.test.pk}/')
        self.assertEqual(deleted.status_code, status.HTTP_400_BAD_REQUEST)
        self.test.refresh_from_db()

    def test_action_permissions_are_enforced(self):
        order = self.create_order()
        item = order.items.get()
        self.client.force_authenticate(self.reception)
        result = self.client.patch(
            f'/api/v1/lab-orders/{order.pk}/results/{item.pk}/', {'result': '13'}, format='json',
        )
        self.assertEqual(result.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get('/api/v1/lab-orders/').status_code, status.HTTP_200_OK)
