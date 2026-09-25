from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from .models import Patient, PatientNote


class PatientModelTests(TestCase):
    def test_normalizes_patient_data_and_calculates_age(self):
        today = timezone.localdate()
        patient = Patient.objects.create(
            first_name='  Ahmad  ',
            last_name='  Khan ',
            national_id=' ab-123 ',
            phone='+93 (700) 123-456',
            date_of_birth=today.replace(year=today.year - 20),
            blood_group='o+',
        )

        self.assertEqual(patient.first_name, 'Ahmad')
        self.assertEqual(patient.last_name, 'Khan')
        self.assertEqual(patient.national_id, 'AB-123')
        self.assertEqual(patient.phone, '+93700123456')
        self.assertEqual(patient.blood_group, 'O+')
        self.assertEqual(patient.age, 20)
        self.assertEqual(patient.age_years, 20)

    def test_rejects_future_birth_date(self):
        with self.assertRaises(ValidationError):
            Patient.objects.create(
                first_name='Future', date_of_birth=timezone.localdate() + timedelta(days=1),
            )

    def test_national_id_is_case_insensitively_unique(self):
        Patient.objects.create(first_name='One', national_id='ABC123')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Patient.objects.create(first_name='Two', national_id='abc123')

    def test_duplicate_candidates_match_identity_data(self):
        patient = Patient.objects.create(
            first_name='Fatima', father_name='Karim', phone='0700123456',
        )
        matches = Patient.objects.possible_duplicates(
            first_name='fatima', father_name='karim',
        )
        self.assertEqual(list(matches), [patient])

    def test_deactivate_and_reactivate_track_state(self):
        user = get_user_model().objects.create_user(username='patient-admin', password='test-password')
        patient = Patient.objects.create(first_name='Active')
        patient.deactivate(by=user, reason='Duplicate registration')

        self.assertFalse(patient.is_active)
        self.assertEqual(patient.deactivated_by, user)
        self.assertEqual(patient.deactivation_reason, 'Duplicate registration')
        self.assertIsNotNone(patient.deactivated_at)

        patient.reactivate()
        self.assertTrue(patient.is_active)
        self.assertIsNone(patient.deactivated_at)


class PatientNoteModelTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='note-author', password='test-password')
        self.patient = Patient.objects.create(first_name='Note')

    def test_rejects_blank_note(self):
        with self.assertRaises(ValidationError):
            PatientNote.objects.create(patient=self.patient, author=self.user, note='   ')

    def test_note_protects_patient_from_hard_delete(self):
        PatientNote.objects.create(patient=self.patient, author=self.user, note='Clinical note')
        with self.assertRaises(ProtectedError):
            self.patient.delete()


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class PatientAPITests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='patient-api-admin', password='Strong-Test-Password-123!', role=User.Role.ADMINISTRATOR, must_change_password=False)
        self.reception = User.objects.create_user(username='patient-api-reception', password='Strong-Test-Password-123!', role=User.Role.RECEPTION, must_change_password=False)
        self.laboratory = User.objects.create_user(username='patient-api-lab', password='Strong-Test-Password-123!', role=User.Role.LABORATORY, must_change_password=False)
        self.clinician = User.objects.create_user(username='patient-api-clinician', password='Strong-Test-Password-123!', role=User.Role.CLINICIAN, must_change_password=False)
        self.patient = Patient.objects.create(first_name='API', last_name='Patient', father_name='Parent', phone='0700123456')
        self.client.force_authenticate(self.admin)

    @staticmethod
    def results(api_response):
        return api_response.data.get('results', api_response.data)

    def test_list_defaults_to_active_and_supports_status_filters(self):
        inactive = Patient.objects.create(first_name='Inactive')
        inactive.deactivate(by=self.admin, reason='Duplicate record')
        active_list = self.client.get('/api/v1/patients/')
        self.assertEqual([item['id'] for item in self.results(active_list)], [str(self.patient.pk)])
        all_patients = self.client.get('/api/v1/patients/?include_inactive=true')
        self.assertEqual(len(self.results(all_patients)), 2)
        inactive_list = self.client.get('/api/v1/patients/?is_active=false')
        self.assertEqual([item['id'] for item in self.results(inactive_list)], [str(inactive.pk)])

    def test_duplicate_check_and_confirmed_creation(self):
        checked = self.client.get('/api/v1/patients/possible-duplicates/?first_name=api&father_name=Parent')
        self.assertEqual(checked.status_code, status.HTTP_200_OK, checked.data)
        self.assertEqual(self.results(checked)[0]['id'], str(self.patient.pk))
        payload = {'first_name': 'API', 'father_name': 'Parent', 'phone': '0700999999'}
        rejected = self.client.post('/api/v1/patients/', payload, format='json')
        self.assertEqual(rejected.status_code, status.HTTP_400_BAD_REQUEST)
        payload['confirm_possible_duplicate'] = True
        created = self.client.post('/api/v1/patients/', payload, format='json')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)

    def test_phone_validation_returns_api_error(self):
        invalid = self.client.post('/api/v1/patients/', {'first_name': 'Short', 'phone': '123'}, format='json')
        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('phone', invalid.data['errors'])

    def test_deactivation_requires_reason_and_privileged_role(self):
        self.client.force_authenticate(self.reception)
        denied = self.client.delete(f'/api/v1/patients/{self.patient.pk}/', {'reason': 'Duplicate record'}, format='json')
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)
        self.client.force_authenticate(self.admin)
        missing = self.client.delete(f'/api/v1/patients/{self.patient.pk}/', {}, format='json')
        self.assertEqual(missing.status_code, status.HTTP_400_BAD_REQUEST)
        archived = self.client.delete(f'/api/v1/patients/{self.patient.pk}/', {'reason': 'Duplicate record'}, format='json')
        self.assertEqual(archived.status_code, status.HTTP_204_NO_CONTENT)
        self.patient.refresh_from_db()
        self.assertFalse(self.patient.is_active)
        self.assertEqual(self.patient.deactivation_reason, 'Duplicate record')

    def test_inactive_patient_requires_reactivation_before_update_or_note(self):
        self.patient.deactivate(by=self.admin, reason='Review')
        update = self.client.patch(f'/api/v1/patients/{self.patient.pk}/', {'last_name': 'Changed'}, format='json')
        self.assertEqual(update.status_code, status.HTTP_400_BAD_REQUEST)
        note = self.client.post('/api/v1/patient-notes/', {'patient': str(self.patient.pk), 'note': 'Not allowed'}, format='json')
        self.assertEqual(note.status_code, status.HTTP_400_BAD_REQUEST)
        reactivated = self.client.post(f'/api/v1/patients/{self.patient.pk}/reactivate/', {}, format='json')
        self.assertEqual(reactivated.status_code, status.HTTP_200_OK, reactivated.data)

    def test_confidential_notes_are_filtered_by_role(self):
        PatientNote.objects.create(patient=self.patient, author=self.admin, note='Public note', is_confidential=False)
        PatientNote.objects.create(patient=self.patient, author=self.admin, note='Confidential note', is_confidential=True)
        self.client.force_authenticate(self.laboratory)
        listed = self.client.get('/api/v1/patient-notes/')
        self.assertEqual([item['note'] for item in self.results(listed)], ['Public note'])
        self.client.force_authenticate(self.clinician)
        listed = self.client.get('/api/v1/patient-notes/')
        self.assertEqual({item['note'] for item in self.results(listed)}, {'Public note', 'Confidential note'})

    def test_note_patient_is_immutable_and_delete_archives(self):
        other = Patient.objects.create(first_name='Other')
        note = PatientNote.objects.create(patient=self.patient, author=self.admin, note='Original')
        moved = self.client.patch(f'/api/v1/patient-notes/{note.pk}/', {'patient': str(other.pk)}, format='json')
        self.assertEqual(moved.status_code, status.HTTP_400_BAD_REQUEST)
        deleted = self.client.delete(f'/api/v1/patient-notes/{note.pk}/', {'reason': 'Entered in error'}, format='json')
        self.assertEqual(deleted.status_code, status.HTTP_204_NO_CONTENT)
        note.refresh_from_db()
        self.assertTrue(note.is_archived)
        hidden = self.client.get('/api/v1/patient-notes/')
        self.assertEqual(self.results(hidden), [])
        visible = self.client.get('/api/v1/patient-notes/?include_archived=true')
        self.assertEqual(self.results(visible)[0]['archive_reason'], 'Entered in error')
