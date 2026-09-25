from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from core.models import AuditLog
from core.mixins import client_ip
from laboratory.models import LabTest
from .models import Department, Service, ServiceFeeHistory


class DepartmentModelTests(TestCase):
    def test_normalizes_text_fields(self):
        department = Department.objects.create(code='  opd  ', name='  Outpatient  ', description='  General care  ')

        self.assertEqual(department.code, 'OPD')
        self.assertEqual(department.name, 'Outpatient')
        self.assertEqual(department.description, 'General care')

    def test_rejects_case_insensitive_duplicates(self):
        Department.objects.create(code='OPD', name='Outpatient')

        with self.assertRaises(ValidationError):
            Department.objects.create(code='opd', name='Another department')
        with self.assertRaises(ValidationError):
            Department.objects.create(code='OTHER', name='outpatient')

    def test_cannot_deactivate_department_with_active_services_implicitly(self):
        department = Department.objects.create(code='OPD', name='Outpatient')
        Service.objects.create(department=department, code='CONSULT', name='Consultation')
        department.is_active = False

        with self.assertRaises(ValidationError):
            department.save()

    def test_explicit_cascade_deactivates_department_and_services(self):
        department = Department.objects.create(code='OPD', name='Outpatient')
        service = Service.objects.create(department=department, code='CONSULT', name='Consultation')

        department.deactivate(deactivate_services=True)

        department.refresh_from_db()
        service.refresh_from_db()
        self.assertFalse(department.is_active)
        self.assertFalse(service.is_active)
        self.assertFalse(department.has_active_services)
        self.assertTrue(department.can_be_deactivated)


class ServiceModelTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(code='OPD', name='Outpatient')

    def test_normalizes_fields_and_reports_availability(self):
        service = Service.objects.create(
            department=self.department, code='  consult  ', name='  Consultation  ', standard_fee=Decimal('10.00')
        )

        self.assertEqual(service.code, 'CONSULT')
        self.assertEqual(service.name, 'Consultation')
        self.assertTrue(service.is_available)

    def test_rejects_case_insensitive_duplicates(self):
        Service.objects.create(department=self.department, code='CONSULT', name='Consultation')

        with self.assertRaises(ValidationError):
            Service.objects.create(department=self.department, code='consult', name='Another service')
        with self.assertRaises(ValidationError):
            Service.objects.create(department=self.department, code='OTHER', name='consultation')

    def test_database_rejects_negative_fee(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Service.objects.bulk_create([
                Service(
                    department=self.department, code='NEGATIVE', name='Negative fee', standard_fee=Decimal('-1.00')
                )
            ])

    def test_active_service_requires_active_department(self):
        self.department.is_active = False
        self.department.save()

        with self.assertRaises(ValidationError):
            Service.objects.create(department=self.department, code='CONSULT', name='Consultation')

    def test_active_laboratory_service_requires_clinical_department(self):
        self.department.is_clinical = False
        self.department.save()

        with self.assertRaises(ValidationError):
            Service.objects.create(
                department=self.department, code='CBC', name='Complete blood count', is_laboratory=True
            )

    def test_linked_laboratory_service_cannot_change_type_or_be_deactivated(self):
        service = Service.objects.create(
            department=self.department, code='CBC', name='Complete blood count', is_laboratory=True
        )
        test = LabTest.objects.create(code='CBC', name='Complete blood count', service=service)

        service.is_laboratory = False
        with self.assertRaises(ValidationError):
            service.save()

        service.refresh_from_db()
        service.is_active = False
        with self.assertRaises(ValidationError):
            service.save()

        test = LabTest.objects.get(pk=test.pk)
        test.is_active = False
        test.save()
        service.refresh_from_db()
        service.deactivate()
        self.assertFalse(service.is_active)

    def test_service_identity_is_immutable_after_use(self):
        lab_service = Service.objects.create(
            department=self.department, code='LAB', name='Laboratory service', is_laboratory=True
        )
        LabTest.objects.create(code='LAB', name='Laboratory test', service=lab_service)

        lab_service.code = 'CHANGED'
        with self.assertRaises(ValidationError):
            lab_service.save()

        other_department = Department.objects.create(code='OTHER', name='Other')
        lab_service.refresh_from_db()
        lab_service.department = other_department
        with self.assertRaises(ValidationError):
            lab_service.save()

        lab_service.refresh_from_db()
        lab_service.name = 'Renamed laboratory service'
        with self.assertRaises(ValidationError):
            lab_service.save()

        lab_service.refresh_from_db()
        lab_service.change_standard_fee(new_fee=Decimal('25.00'), reason='Tariff update')
        self.assertEqual(lab_service.standard_fee, Decimal('25.00'))

    def test_fee_changes_are_historic_and_require_reason_through_domain_method(self):
        service = Service.objects.create(
            department=self.department, code='XRAY', name='X-ray', standard_fee=Decimal('10.00')
        )
        self.assertEqual(service.fee_history.count(), 1)

        service.change_standard_fee(new_fee='15.00', reason='Annual tariff review')

        history = service.fee_history.first()
        self.assertEqual(history.previous_fee, Decimal('10.00'))
        self.assertEqual(history.new_fee, Decimal('15.00'))
        self.assertEqual(history.reason, 'Annual tariff review')
        with self.assertRaises(ValidationError):
            history.delete()

        service.standard_fee = Decimal('20.00')
        with self.assertRaises(ValidationError):
            service.save()

    def test_catalog_records_cannot_be_hard_deleted_or_bulk_lifecycle_updated(self):
        service = Service.objects.create(department=self.department, code='SAFE', name='Safe service')
        with self.assertRaises(ValidationError):
            service.delete()
        with self.assertRaises(ValidationError):
            self.department.delete()
        with self.assertRaises(ValidationError):
            Service.objects.filter(pk=service.pk).update(is_active=False)

    def test_laboratory_service_requires_active_test_to_be_available(self):
        service = Service.objects.create(
            department=self.department, code='LAB-AVAIL', name='Lab availability', is_laboratory=True
        )
        self.assertFalse(service.is_available)
        LabTest.objects.create(code='LAB-AVAIL', name='Lab availability', service=service)
        self.assertTrue(service.is_available)


class DepartmentAPITests(APITestCase):
    def setUp(self):
        self.administrator = User.objects.create_user(
            username='department-admin', password='test-password',
            role=User.Role.ADMINISTRATOR, must_change_password=False,
        )
        self.manager = User.objects.create_user(
            username='department-manager', password='test-password',
            role=User.Role.MANAGER, must_change_password=False,
        )
        self.client.force_authenticate(self.administrator)
        self.department = Department.objects.create(code='OPD', name='Outpatient')
        self.service = Service.objects.create(
            department=self.department, code='CONSULT', name='Consultation',
            standard_fee=Decimal('100.00'),
        )

    def test_serializers_expose_calculated_state(self):
        response = self.client.get(f'/api/v1/departments/{self.department.pk}/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['has_active_services'])
        self.assertFalse(response.data['can_be_deactivated'])
        self.assertEqual(response.data['deactivation_blockers'], ['active services'])
        self.assertEqual(response.data['service_count'], 1)
        self.assertEqual(response.data['active_service_count'], 1)

        response = self.client.get(f'/api/v1/services/{self.service.pk}/')
        self.assertTrue(response.data['is_available'])
        self.assertEqual(response.data['department_name'], 'Outpatient')

    def test_create_normalizes_and_rejects_case_insensitive_duplicates(self):
        response = self.client.post('/api/v1/departments/', {
            'code': '  rad  ', 'name': '  Radiology  ', 'description': '  Imaging  ',
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['code'], 'RAD')
        self.assertEqual(response.data['name'], 'Radiology')

        service_response = self.client.post('/api/v1/services/', {
            'department': str(self.department.pk), 'code': 'XRAY-NEW',
            'name': 'New X-ray', 'standard_fee': '75.00',
        }, format='json')
        self.assertEqual(service_response.status_code, status.HTTP_201_CREATED)
        initial_fee = ServiceFeeHistory.objects.get(service_id=service_response.data['id'])
        self.assertEqual(initial_fee.changed_by, self.administrator)

        duplicate = self.client.post('/api/v1/departments/', {
            'code': 'opd', 'name': 'Another department',
        })
        self.assertEqual(duplicate.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('code', duplicate.data['errors'])

    def test_filters_are_validated_and_applied(self):
        Department.objects.create(code='OLD', name='Inactive', is_active=False)

        response = self.client.get('/api/v1/departments/?is_active=true')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([item['code'] for item in response.data['results']], ['OPD'])

        invalid = self.client.get('/api/v1/departments/?is_active=not-a-boolean')
        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.client.get(
            f'/api/v1/services/?department={self.department.pk}&is_available=true&is_discountable=true'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)

        invalid = self.client.get('/api/v1/services/?department=not-a-uuid')
        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)

        unknown = self.client.get('/api/v1/services/?unsupported=true')
        self.assertEqual(unknown.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('unsupported', unknown.data['errors'])

    def test_is_active_is_read_only_outside_lifecycle_actions(self):
        response = self.client.patch(
            f'/api/v1/services/{self.service.pk}/', {'is_active': False}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('is_active', response.data['errors'])
        self.service.refresh_from_db()
        self.assertTrue(self.service.is_active)

    def test_department_deactivation_requires_explicit_cascade_and_is_audited(self):
        blocked = self.client.post(
            f'/api/v1/departments/{self.department.pk}/deactivate/', {}, format='json'
        )
        self.assertEqual(blocked.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.client.post(
            f'/api/v1/departments/{self.department.pk}/deactivate/',
            {'deactivate_services': True}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.department.refresh_from_db()
        self.service.refresh_from_db()
        self.assertFalse(self.department.is_active)
        self.assertFalse(self.service.is_active)
        self.assertEqual(response.data['service_count'], 1)
        self.assertEqual(response.data['active_service_count'], 0)
        self.assertFalse(response.data['has_active_services'])
        audit = AuditLog.objects.filter(object_id=str(self.department.pk), action='update').latest('created_at')
        self.assertEqual(audit.changes['is_active']['to'], False)
        self.assertIn(str(self.service.pk), audit.changes['deactivated_service_ids'])

    def test_service_lifecycle_rules_and_manager_permission(self):
        self.manager.department = self.department
        self.manager.save(update_fields=('department',))
        self.client.force_authenticate(self.manager)
        response = self.client.post(f'/api/v1/services/{self.service.pk}/deactivate/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.service.refresh_from_db()
        self.assertFalse(self.service.is_active)

        self.manager.department = None
        self.manager.save(update_fields=('department',))
        self.department.deactivate()
        self.client.force_authenticate(self.administrator)
        response = self.client.post(f'/api/v1/services/{self.service.pk}/activate/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_manager_is_department_scoped_and_cannot_edit_catalog_fields(self):
        other_department = Department.objects.create(code='OTHER-MGR', name='Other manager department')
        other_service = Service.objects.create(
            department=other_department, code='OTHER-MGR-SVC', name='Other manager service'
        )
        self.manager.department = self.department
        self.manager.save(update_fields=('department',))
        self.client.force_authenticate(self.manager)

        patch_response = self.client.patch(
            f'/api/v1/services/{self.service.pk}/', {'standard_fee': '1.00'}, format='json'
        )
        self.assertEqual(patch_response.status_code, status.HTTP_403_FORBIDDEN)
        outside_response = self.client.post(f'/api/v1/services/{other_service.pk}/deactivate/')
        self.assertEqual(outside_response.status_code, status.HTTP_404_NOT_FOUND)
        own_response = self.client.post(f'/api/v1/services/{self.service.pk}/deactivate/')
        self.assertEqual(own_response.status_code, status.HTTP_200_OK)

    def test_lifecycle_change_rolls_back_if_audit_write_fails(self):
        with patch('departments.views.ServiceViewSet._audit', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.client.post(f'/api/v1/services/{self.service.pk}/deactivate/')
        self.service.refresh_from_db()
        self.assertTrue(self.service.is_active)

    def test_fee_update_requires_reason_and_records_actor(self):
        rejected = self.client.patch(
            f'/api/v1/services/{self.service.pk}/', {'standard_fee': '125.00'}, format='json'
        )
        self.assertEqual(rejected.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('fee_change_reason', rejected.data['errors'])

        response = self.client.patch(f'/api/v1/services/{self.service.pk}/', {
            'standard_fee': '125.00', 'fee_change_reason': 'Approved tariff adjustment',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        history = ServiceFeeHistory.objects.filter(service=self.service).latest('changed_at')
        self.assertEqual(history.changed_by, self.administrator)
        self.assertEqual(history.reason, 'Approved tariff adjustment')

        response = self.client.get(f'/api/v1/services/{self.service.pk}/fee-history/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        latest = response.data['results'][0]
        self.assertEqual(latest['new_fee'], '125.00')
        self.assertEqual(latest['changed_by'], self.administrator.pk)
        self.assertEqual(latest['changed_by_name'], self.administrator.display_name)

    def test_active_department_user_blocks_deactivation(self):
        self.service.deactivate()
        user = User.objects.create_user(
            username='department-user', password='test-password', department=self.department,
            must_change_password=False,
        )
        response = self.client.post(f'/api/v1/departments/{self.department.pk}/deactivate/', {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        detail = self.client.get(f'/api/v1/departments/{self.department.pk}/')
        self.assertIn('active users', detail.data['deactivation_blockers'])
        user.deactivate()
        response = self.client.post(f'/api/v1/departments/{self.department.pk}/deactivate/', {})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_repeated_lifecycle_requests_are_idempotent_and_do_not_add_audit_noise(self):
        self.service.deactivate()
        before = AuditLog.objects.filter(object_id=str(self.service.pk), action='update').count()

        first = self.client.post(f'/api/v1/services/{self.service.pk}/deactivate/')
        second = self.client.post(f'/api/v1/services/{self.service.pk}/deactivate/')

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(
            AuditLog.objects.filter(object_id=str(self.service.pk), action='update').count(), before
        )

    def test_hard_delete_and_full_update_are_disabled(self):
        self.assertEqual(
            self.client.delete(f'/api/v1/services/{self.service.pk}/').status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )
        self.assertEqual(
            self.client.put(f'/api/v1/services/{self.service.pk}/', {}).status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    def test_non_administrator_cannot_change_departments(self):
        self.client.force_authenticate(self.manager)
        response = self.client.patch(
            f'/api/v1/departments/{self.department.pk}/', {'name': 'Changed'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class AuditClientIPTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_ignores_forwarded_header_from_untrusted_peer(self):
        request = self.factory.get('/', HTTP_X_FORWARDED_FOR='203.0.113.10', REMOTE_ADDR='198.51.100.8')
        self.assertEqual(client_ip(request), '198.51.100.8')

    @override_settings(AUDIT_TRUSTED_PROXY_NETWORKS=('10.0.0.0/8',))
    def test_uses_nearest_untrusted_hop_behind_trusted_proxies(self):
        request = self.factory.get(
            '/', HTTP_X_FORWARDED_FOR='198.51.100.7, 10.1.1.2', REMOTE_ADDR='10.2.2.3'
        )
        self.assertEqual(client_ip(request), '198.51.100.7')
