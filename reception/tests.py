from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from departments.models import Department, Service
from finance.models import Wallet
from laboratory.models import LabOrder
from patients.models import Patient
from reception.models import Visit, VisitPayment, VisitQueueEntry, VisitService


class ReceptionDomainTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='reception-domain', password='Strong-Test-Password-123!',
            role=User.Role.ADMINISTRATOR, must_change_password=False,
        )
        self.client.force_authenticate(self.user)
        self.patient = Patient.objects.create(first_name='Domain', last_name='Patient', father_name='Test')
        self.department = Department.objects.create(code='DOMAIN', name='Domain Department')
        self.other_department = Department.objects.create(code='OTHER', name='Other Department')
        self.service = Service.objects.create(
            department=self.department, code='DOMAIN-SVC', name='Domain Service', standard_fee=Decimal('100.00')
        )
        self.other_service = Service.objects.create(
            department=self.other_department, code='OTHER-SVC', name='Other Service', standard_fee=Decimal('50.00')
        )
        self.clinician = User.objects.create_user(
            username='clinician-domain', password='Strong-Test-Password-123!', role=User.Role.CLINICIAN,
            department=self.department, must_change_password=False,
        )

    def create_visit(self, *, paid='0.00', discount='0.00'):
        payload = {
            'patient': str(self.patient.pk), 'department': str(self.department.pk),
            'visit_date': timezone.now().isoformat(), 'discount_amount': discount,
            'discount_reason': 'Assistance' if Decimal(discount) else '', 'paid_amount': paid,
            'service_lines': [{'service': str(self.service.pk), 'quantity': 1, 'unit_price': '100.00'}],
        }
        result = self.client.post('/api/v1/receptions/', payload, format='json')
        self.assertEqual(result.status_code, status.HTTP_201_CREATED, result.data)
        return Visit.objects.get(pk=result.data['id'])

    def test_creation_snapshots_service_and_records_initial_payment(self):
        visit = self.create_visit(paid='40.00')
        line = visit.service_lines.get()
        self.assertEqual((line.service_code, line.service_name), (self.service.code, self.service.name))
        self.assertEqual(visit.payment_status, 'partially_paid')
        self.assertEqual(visit.payments.get().amount, Decimal('40.00'))
        self.assertEqual(Wallet.objects.get(kind=Wallet.Kind.RECEPTION).balance, Decimal('40.00'))

    def test_installment_endpoint_updates_ledger_and_rejects_overpayment(self):
        visit = self.create_visit(paid='20.00')
        result = self.client.post(
            f'/api/v1/receptions/{visit.pk}/payments/',
            {'amount': '80.00', 'method': 'card', 'transaction_reference': 'CARD-1'}, format='json',
        )
        self.assertEqual(result.status_code, status.HTTP_201_CREATED, result.data)
        visit.refresh_from_db()
        self.assertEqual(visit.paid_amount, Decimal('100.00'))
        self.assertEqual(visit.payment_status, 'paid')
        self.assertEqual(visit.payments.filter(payment_type=VisitPayment.PaymentType.PAYMENT).count(), 2)
        invalid = self.client.post(f'/api/v1/receptions/{visit.pk}/payments/', {'amount': '1.00'}, format='json')
        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)

    def test_status_actions_enforce_transitions_and_timestamps(self):
        visit = self.create_visit()
        visit.provider = self.clinician
        visit.save(update_fields=('provider', 'updated_at'))
        started = self.client.post(f'/api/v1/receptions/{visit.pk}/start/', {}, format='json')
        self.assertEqual(started.status_code, status.HTTP_200_OK, started.data)
        completed = self.client.post(f'/api/v1/receptions/{visit.pk}/complete/', {}, format='json')
        self.assertEqual(completed.status_code, status.HTTP_200_OK, completed.data)
        visit.refresh_from_db()
        self.assertIsNotNone(visit.started_at)
        self.assertIsNotNone(visit.completed_at)
        with self.assertRaises(ValidationError):
            visit.start()

    def test_cancellation_refunds_once_and_cancels_open_lab_orders(self):
        visit = self.create_visit(paid='100.00')
        order = LabOrder.objects.create(
            patient=self.patient, visit=visit, ordered_by=self.user, ordered_at=timezone.now()
        )
        result = self.client.post(
            f'/api/v1/receptions/{visit.pk}/cancel/', {'reason': 'Registration correction'}, format='json'
        )
        self.assertEqual(result.status_code, status.HTTP_200_OK, result.data)
        visit.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(visit.status, Visit.Status.CANCELLED)
        self.assertEqual(order.status, LabOrder.Status.CANCELLED)
        self.assertEqual(visit.payment_status, 'refunded')
        self.assertEqual(visit.payments.filter(payment_type=VisitPayment.PaymentType.REFUND).count(), 1)
        self.assertEqual(Wallet.objects.get(kind=Wallet.Kind.RECEPTION).balance, Decimal('0.00'))
        repeated = self.client.post(
            f'/api/v1/receptions/{visit.pk}/cancel/', {'reason': 'Again'}, format='json'
        )
        self.assertEqual(repeated.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(visit.payments.filter(payment_type=VisitPayment.PaymentType.REFUND).count(), 1)

    def test_completed_lab_order_blocks_visit_cancellation(self):
        visit = self.create_visit()
        LabOrder.objects.create(
            patient=self.patient, visit=visit, ordered_by=self.user, ordered_at=timezone.now(),
            status=LabOrder.Status.COMPLETED, collected_by=self.user, collected_at=timezone.now(),
        )
        result = self.client.post(f'/api/v1/receptions/{visit.pk}/cancel/', {'reason': 'No'}, format='json')
        self.assertEqual(result.status_code, status.HTTP_400_BAD_REQUEST)
        visit.refresh_from_db()
        self.assertNotEqual(visit.status, Visit.Status.CANCELLED)

    def test_model_and_database_constraints_protect_invariants(self):
        visit = self.create_visit()
        with self.assertRaises(ValidationError):
            VisitService.objects.create(visit=visit, service=self.other_service, quantity=1, unit_price=Decimal('50.00'))
        visit.paid_amount = Decimal('101.00')
        with self.assertRaises(ValidationError):
            visit.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Visit.objects.filter(pk=visit.pk).update(paid_amount=Decimal('101.00'))

    def test_financial_and_payment_records_are_immutable(self):
        visit = self.create_visit(paid='25.00')
        visit.department = self.other_department
        with self.assertRaises(ValidationError):
            visit.save()
        payment = visit.payments.get()
        payment.amount = Decimal('10.00')
        with self.assertRaises(ValidationError):
            payment.save()
        with self.assertRaises(ValidationError):
            payment.delete()

    def test_catalog_price_is_default_and_override_requires_manager_reason(self):
        reception_user = User.objects.create_user(
            username='price-reception', password='Strong-Test-Password-123!', role=User.Role.RECEPTION,
            must_change_password=False,
        )
        self.client.force_authenticate(reception_user)
        base = {
            'patient': str(self.patient.pk), 'department': str(self.department.pk),
            'visit_date': timezone.now().isoformat(), 'paid_amount': '0.00',
        }
        defaulted = self.client.post('/api/v1/receptions/', {
            **base, 'service_lines': [{'service': str(self.service.pk), 'quantity': 1}],
        }, format='json')
        self.assertEqual(defaulted.status_code, status.HTTP_201_CREATED, defaulted.data)
        self.assertEqual(Decimal(defaulted.data['service_lines'][0]['unit_price']), Decimal('100.00'))
        denied = self.client.post('/api/v1/receptions/', {
            **base, 'service_lines': [{
                'service': str(self.service.pk), 'quantity': 1, 'unit_price': '80.00',
                'price_override_reason': 'Promotion',
            }],
        }, format='json')
        self.assertEqual(denied.status_code, status.HTTP_400_BAD_REQUEST)
        self.client.force_authenticate(self.user)
        allowed = self.client.post('/api/v1/receptions/', {
            **base, 'service_lines': [{
                'service': str(self.service.pk), 'quantity': 1, 'unit_price': '80.00',
                'price_override_reason': 'Manager-approved correction',
            }],
        }, format='json')
        self.assertEqual(allowed.status_code, status.HTTP_201_CREATED, allowed.data)
        self.assertTrue(allowed.data['service_lines'][0]['is_price_override'])

    def test_discount_policy_and_non_discountable_services(self):
        reception_user = User.objects.create_user(
            username='discount-reception', password='Strong-Test-Password-123!', role=User.Role.RECEPTION,
            must_change_password=False,
        )
        self.client.force_authenticate(reception_user)
        payload = {
            'patient': str(self.patient.pk), 'department': str(self.department.pk),
            'visit_date': timezone.now().isoformat(), 'discount_amount': '11.00',
            'discount_reason': 'Requested assistance', 'paid_amount': '0.00',
            'service_lines': [{'service': str(self.service.pk), 'quantity': 1}],
        }
        denied = self.client.post('/api/v1/receptions/', payload, format='json')
        self.assertEqual(denied.status_code, status.HTTP_400_BAD_REQUEST)
        self.client.force_authenticate(self.user)
        approved = self.client.post('/api/v1/receptions/', payload, format='json')
        self.assertEqual(approved.status_code, status.HTTP_201_CREATED, approved.data)
        visit = Visit.objects.get(pk=approved.data['id'])
        self.assertEqual(visit.discount_approved_by, self.user)
        self.service.is_discountable = False
        self.service.save(update_fields=('is_discountable', 'updated_at'))
        rejected = self.client.post('/api/v1/receptions/', payload, format='json')
        self.assertEqual(rejected.status_code, status.HTTP_400_BAD_REQUEST)

    def test_filters_receipt_and_queue_workflow(self):
        visit = self.create_visit(paid='20.00')
        invalid = self.client.get('/api/v1/receptions/?status=not-real')
        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)
        listed = self.client.get('/api/v1/receptions/?payment_status=partially_paid')
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        listed_results = listed.data.get('results', listed.data)
        self.assertEqual(len(listed_results), 1)
        receipt = self.client.get(f'/api/v1/receptions/{visit.pk}/receipt/')
        self.assertEqual(receipt.status_code, status.HTTP_200_OK, receipt.data)
        self.assertEqual(receipt.data['receipt_number'], visit.visit_number)
        queue = self.client.get('/api/v1/receptions/queue/')
        self.assertEqual(queue.status_code, status.HTTP_200_OK, queue.data)
        queue_results = queue.data.get('results', queue.data)
        self.assertEqual(queue_results[0]['token_number'], 1)
        for queue_status in ('called', 'serving'):
            changed = self.client.post(
                f'/api/v1/receptions/{visit.pk}/queue-status/', {'status': queue_status}, format='json'
            )
            self.assertEqual(changed.status_code, status.HTTP_200_OK, changed.data)

    def test_clinician_can_start_and_complete_assigned_visit(self):
        visit = self.create_visit()
        visit.provider = self.clinician
        visit.save(update_fields=('provider', 'updated_at'))
        self.client.force_authenticate(self.clinician)
        started = self.client.post(f'/api/v1/receptions/{visit.pk}/start/', {}, format='json')
        self.assertEqual(started.status_code, status.HTTP_200_OK, started.data)
        completed = self.client.post(f'/api/v1/receptions/{visit.pk}/complete/', {}, format='json')
        self.assertEqual(completed.status_code, status.HTTP_200_OK, completed.data)
        visit.refresh_from_db()
        self.assertEqual(visit.queue_entry.status, VisitQueueEntry.Status.FINISHED)

    def test_clinician_only_sees_and_changes_assigned_visits(self):
        assigned = self.create_visit()
        assigned.provider = self.clinician
        assigned.save(update_fields=('provider', 'updated_at'))
        unassigned = self.create_visit()

        self.client.force_authenticate(self.clinician)
        listed = self.client.get('/api/v1/receptions/')
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        results = listed.data.get('results', listed.data)
        self.assertEqual([item['id'] for item in results], [str(assigned.pk)])

        queue = self.client.get('/api/v1/receptions/queue/')
        self.assertEqual(queue.status_code, status.HTTP_200_OK)
        queue_results = queue.data.get('results', queue.data)
        self.assertEqual([str(item['visit']) for item in queue_results], [str(assigned.pk)])

        self.assertEqual(
            self.client.get(f'/api/v1/receptions/{unassigned.pk}/').status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.post(f'/api/v1/receptions/{unassigned.pk}/start/', {}, format='json').status_code,
            status.HTTP_404_NOT_FOUND,
        )
