from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from core.models import AuditLog, HospitalSettings


def logo_file():
    content = BytesIO()
    Image.new('RGB', (24, 24), '#0e8795').save(content, format='PNG')
    return SimpleUploadedFile('hospital-logo.png', content.getvalue(), content_type='image/png')


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'}})
class HospitalSettingsApiTests(APITestCase):
    def setUp(self):
        self.administrator = User.objects.create_user(
            username='settings-admin', password='Strong-Test-Password-123!',
            role=User.Role.ADMINISTRATOR, must_change_password=False,
        )
        self.receptionist = User.objects.create_user(
            username='settings-reception', password='Strong-Test-Password-123!',
            role=User.Role.RECEPTION, must_change_password=False,
        )

    def test_settings_are_publicly_readable_with_safe_defaults(self):
        result = self.client.get('/api/v1/settings/')
        self.assertEqual(result.status_code, status.HTTP_200_OK)
        self.assertEqual(result.data['hospital_name'], 'Hospital +')
        self.assertFalse(result.data['has_logo'])
        self.assertEqual(HospitalSettings.objects.count(), 1)

    def test_administrator_can_update_name_and_logo(self):
        self.client.force_authenticate(self.administrator)
        result = self.client.patch(
            '/api/v1/settings/',
            {'hospital_name': 'Kabul Care Hospital', 'logo': logo_file()},
            format='multipart',
        )
        self.assertEqual(result.status_code, status.HTTP_200_OK, result.data)
        self.assertEqual(result.data['hospital_name'], 'Kabul Care Hospital')
        self.assertTrue(result.data['has_logo'])
        self.assertEqual(AuditLog.objects.filter(actor=self.administrator, action=AuditLog.Action.UPDATE).count(), 1)
        logo = self.client.get('/api/v1/settings/logo/')
        self.assertEqual(logo.status_code, status.HTTP_200_OK)
        self.assertEqual(logo['Content-Type'], 'image/png')

    def test_non_administrator_cannot_update_settings(self):
        self.client.force_authenticate(self.receptionist)
        result = self.client.patch('/api/v1/settings/', {'hospital_name': 'Not allowed'}, format='multipart')
        self.assertEqual(result.status_code, status.HTTP_403_FORBIDDEN)
