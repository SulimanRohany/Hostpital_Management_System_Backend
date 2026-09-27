import sqlite3
import tempfile
from pathlib import Path

from django.db import connection
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITransactionTestCase

from accounts.models import User
from patients.models import Patient


class DatabaseRestoreAPITests(APITransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.settings_override = override_settings(
            DATABASE_BACKUP_DIR=Path(self.temp_directory.name),
            DATABASE_RESTORE_MAX_BYTES=20 * 1024 * 1024,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.admin = User.objects.create_user(
            username='restore-admin', password='test-password',
            role=User.Role.ADMINISTRATOR, must_change_password=False,
        )
        self.client.force_authenticate(self.admin)

    def database_upload(self, name='hospital.sqlite3'):
        backup_path = Path(self.temp_directory.name) / name
        connection.ensure_connection()
        backup = sqlite3.connect(backup_path)
        try:
            connection.connection.backup(backup)
        finally:
            backup.close()
        return backup_path.open('rb')

    def test_restores_uploaded_backup_and_keeps_pre_restore_recovery_copy(self):
        patient = Patient.objects.create(
            first_name='Before', last_name='Restore', father_name='Parent',
        )
        upload = self.database_upload()
        patient.first_name = 'Changed'
        patient.save(update_fields=['first_name'])

        with upload:
            result = self.client.post(
                '/api/v1/database-restore/',
                {'backup': upload, 'confirmation': 'RESTORE'},
                format='multipart',
            )

        self.assertEqual(result.status_code, status.HTTP_200_OK, result.data)
        patient.refresh_from_db()
        self.assertEqual(patient.first_name, 'Before')
        recovery = Path(self.temp_directory.name) / result.data['recovery_backup']
        self.assertTrue(recovery.exists())
        self.assertTrue(recovery.name.startswith('pre-restore-'))

    def test_rejects_non_sqlite_file_without_changing_data(self):
        patient = Patient.objects.create(
            first_name='Safe', last_name='Patient', father_name='Parent',
        )
        invalid = Path(self.temp_directory.name) / 'invalid.sqlite3'
        invalid.write_bytes(b'not a database')

        with invalid.open('rb') as upload:
            result = self.client.post(
                '/api/v1/database-restore/',
                {'backup': upload, 'confirmation': 'RESTORE'},
                format='multipart',
            )

        self.assertEqual(result.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(Patient.objects.filter(pk=patient.pk, first_name='Safe').exists())

    def test_requires_explicit_confirmation(self):
        with self.database_upload() as upload:
            result = self.client.post(
                '/api/v1/database-restore/',
                {'backup': upload, 'confirmation': 'restore'},
                format='multipart',
            )
        self.assertEqual(result.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('confirmation', result.data)
