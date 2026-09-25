from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from core.models import AuditLog

from departments.models import Department

from .models import User


class UserModelTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(code='ACC', name='Accounts test')

    def test_user_manager_requires_password(self):
        with self.assertRaisesMessage(ValueError, 'A password is required.'):
            User.objects.create_user(username='no-password')

    def test_superuser_is_an_administrator_without_forced_password_change(self):
        user = User.objects.create_superuser(username='root', password='Strong-Test-Password-123!')

        self.assertEqual(user.role, User.Role.ADMINISTRATOR)
        self.assertFalse(user.must_change_password)
        self.assertEqual(set(user.assigned_roles), {value for value, _label in User.Role.choices})

    def test_user_can_have_multiple_section_roles(self):
        user = User.objects.create_user(
            username='multi-role', password='Strong-Test-Password-123!',
            roles=[User.Role.RECEPTION, User.Role.FINANCE, User.Role.LABORATORY],
        )

        self.assertEqual(user.role, User.Role.RECEPTION)
        self.assertTrue(user.has_role(User.Role.RECEPTION, User.Role.FINANCE))
        self.assertTrue(user.has_role(User.Role.LABORATORY))
        self.assertFalse(user.has_role(User.Role.PHARMACY))

    def test_active_user_cannot_use_inactive_department(self):
        self.department.is_active = False
        self.department.save()

        with self.assertRaises(ValidationError):
            User.objects.create_user(
                username='inactive-department', password='Strong-Test-Password-123!',
                department=self.department,
            )

    def test_temporary_and_personal_password_changes_set_correct_flag(self):
        user = User.objects.create_user(username='passwords', password='Strong-Test-Password-123!')

        user.change_password('Another-Strong-Password-123!')
        self.assertFalse(user.must_change_password)
        user.set_temporary_password('Temporary-Strong-Password-123!')
        self.assertTrue(user.must_change_password)

    def test_user_cannot_deactivate_self(self):
        user = User.objects.create_user(username='self', password='Strong-Test-Password-123!')

        with self.assertRaises(ValidationError):
            user.deactivate(by=user)

    def test_final_administrator_cannot_be_deactivated(self):
        administrator = User.objects.create_user(
            username='only-admin', password='Strong-Test-Password-123!', role=User.Role.ADMINISTRATOR,
        )

        with self.assertRaises(ValidationError):
            administrator.deactivate()

    def test_final_administrator_cannot_be_demoted(self):
        administrator = User.objects.create_user(
            username='only-admin', password='Strong-Test-Password-123!', role=User.Role.ADMINISTRATOR,
        )
        administrator.role = User.Role.RECEPTION

        with self.assertRaises(ValidationError):
            administrator.save()

    def test_administrator_can_be_deactivated_when_another_is_active(self):
        first = User.objects.create_user(
            username='first-admin', password='Strong-Test-Password-123!', role=User.Role.ADMINISTRATOR,
        )
        User.objects.create_user(
            username='second-admin', password='Strong-Test-Password-123!', role=User.Role.ADMINISTRATOR,
        )

        first.deactivate()
        self.assertFalse(first.is_active)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class UserAPITests(APITestCase):
    password = 'Strong-Test-Password-123!'

    def setUp(self):
        self.department = Department.objects.create(code='API', name='API department')
        self.admin = User.objects.create_user(
            username='api-admin', password=self.password, role=User.Role.ADMINISTRATOR,
            must_change_password=False,
        )
        self.other_admin = User.objects.create_user(
            username='other-admin', password=self.password, role=User.Role.ADMINISTRATOR,
            must_change_password=False,
        )
        self.user = User.objects.create_user(
            username='api-user', password=self.password, role=User.Role.RECEPTION,
            department=self.department, must_change_password=False,
        )

    @staticmethod
    def results(api_response):
        return api_response.data.get('results', api_response.data)

    def authenticate(self, user=None):
        self.client.force_authenticate(user or self.admin)

    def token_pair(self, username='api-user', password=None):
        return self.client.post(
            '/api/v1/auth/token/',
            {'username': username, 'password': password or self.password},
            format='json',
        )

    def test_me_and_change_password_require_authentication(self):
        self.assertEqual(self.client.get('/api/v1/users/me/').status_code, status.HTTP_401_UNAUTHORIZED)
        result = self.client.post(
            '/api/v1/users/change-password/',
            {'current_password': self.password, 'new_password': 'Different-Strong-Password-123!'},
            format='json',
        )
        self.assertEqual(result.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_non_administrator_cannot_manage_users(self):
        self.authenticate(self.user)
        self.assertEqual(self.client.get('/api/v1/users/').status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_create_account_with_multiple_sections(self):
        self.authenticate()
        result = self.client.post('/api/v1/users/', {
            'username': 'multi-section-api',
            'password': self.password,
            'roles': [User.Role.RECEPTION, User.Role.FINANCE, User.Role.LABORATORY],
        }, format='json')

        self.assertEqual(result.status_code, status.HTTP_201_CREATED, result.data)
        self.assertEqual(result.data['roles'], ['reception', 'finance', 'laboratory'])
        created = User.objects.get(username='multi-section-api')
        self.assertTrue(created.has_role(User.Role.FINANCE))
        self.assertTrue(created.has_role(User.Role.LABORATORY))

    def test_administrator_always_receives_all_sections_and_access_is_immutable(self):
        self.authenticate()
        created = self.client.post('/api/v1/users/', {
            'username': 'all-sections-admin',
            'password': self.password,
            'roles': [User.Role.ADMINISTRATOR],
        }, format='json')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertEqual(set(created.data['roles']), {value for value, _label in User.Role.choices})

        changed = self.client.patch(
            f'/api/v1/users/{created.data["id"]}/',
            {'roles': [User.Role.RECEPTION]}, format='json',
        )
        self.assertEqual(changed.status_code, status.HTTP_400_BAD_REQUEST)

    def test_temporary_password_user_is_restricted_until_password_change(self):
        self.user.must_change_password = True
        self.user.save(update_fields=('must_change_password',))
        self.authenticate(self.user)
        self.assertEqual(self.client.get('/api/v1/departments/').status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get('/api/v1/users/me/').status_code, status.HTTP_200_OK)
        changed = self.client.post(
            '/api/v1/users/change-password/',
            {'current_password': self.password, 'new_password': 'Personal-Strong-Password-123!'},
            format='json',
        )
        self.assertEqual(changed.status_code, status.HTTP_204_NO_CONTENT, changed.data)
        self.user.refresh_from_db()
        self.assertFalse(self.user.must_change_password)
        self.assertEqual(self.client.get('/api/v1/departments/').status_code, status.HTTP_200_OK)

    def test_password_change_rejects_reuse_and_revokes_existing_access_token(self):
        tokens = self.token_pair()
        self.assertEqual(tokens.status_code, status.HTTP_200_OK, tokens.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens.data['access']}")
        reused = self.client.post(
            '/api/v1/users/change-password/',
            {'current_password': self.password, 'new_password': self.password}, format='json',
        )
        self.assertEqual(reused.status_code, status.HTTP_400_BAD_REQUEST)
        changed = self.client.post(
            '/api/v1/users/change-password/',
            {'current_password': self.password, 'new_password': 'Changed-Strong-Password-123!'},
            format='json',
        )
        self.assertEqual(changed.status_code, status.HTTP_204_NO_CONTENT, changed.data)
        self.assertEqual(self.client.get('/api/v1/users/me/').status_code, status.HTTP_401_UNAUTHORIZED)

    def test_password_change_allows_password_similar_to_username(self):
        self.authenticate(self.user)
        changed = self.client.post(
            '/api/v1/users/change-password/',
            {'current_password': self.password, 'new_password': self.user.username},
            format='json',
        )

        self.assertEqual(changed.status_code, status.HTTP_204_NO_CONTENT, changed.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.user.username))

    def test_password_change_allows_common_password(self):
        common_password_user = User.objects.create_user(
            username='common-password-user',
            password='Initial-Strong-Password-123!',
            role=User.Role.RECEPTION,
        )
        self.authenticate(common_password_user)

        changed = self.client.post(
            '/api/v1/users/change-password/',
            {'current_password': 'Initial-Strong-Password-123!', 'new_password': 'password'},
            format='json',
        )

        self.assertEqual(changed.status_code, status.HTTP_204_NO_CONTENT, changed.data)
        common_password_user.refresh_from_db()
        self.assertTrue(common_password_user.check_password('password'))

    def test_admin_reset_sets_temporary_password_and_generic_patch_cannot_change_it(self):
        self.authenticate()
        patched = self.client.patch(
            f'/api/v1/users/{self.user.pk}/', {'password': 'Patched-Strong-Password-123!'}, format='json',
        )
        self.assertEqual(patched.status_code, status.HTTP_400_BAD_REQUEST)
        reset = self.client.post(
            f'/api/v1/users/{self.user.pk}/reset-password/',
            {'new_password': 'Temporary-Strong-Password-123!'}, format='json',
        )
        self.assertEqual(reset.status_code, status.HTTP_204_NO_CONTENT, reset.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.must_change_password)
        self.assertTrue(self.user.check_password('Temporary-Strong-Password-123!'))

    def test_activate_deactivate_and_final_admin_rules(self):
        self.authenticate()
        deactivated = self.client.post(f'/api/v1/users/{self.user.pk}/deactivate/', {}, format='json')
        self.assertEqual(deactivated.status_code, status.HTTP_200_OK, deactivated.data)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        activated = self.client.post(f'/api/v1/users/{self.user.pk}/activate/', {}, format='json')
        self.assertEqual(activated.status_code, status.HTTP_200_OK, activated.data)
        self.other_admin.deactivate()
        rejected = self.client.post(f'/api/v1/users/{self.admin.pk}/deactivate/', {}, format='json')
        self.assertEqual(rejected.status_code, status.HTTP_400_BAD_REQUEST)

    def test_filters_and_read_only_account_flags(self):
        self.user.must_change_password = True
        self.user.save(update_fields=('must_change_password',))
        self.authenticate()
        listed = self.client.get(
            f'/api/v1/users/?role=reception&department={self.department.pk}&must_change_password=true'
        )
        self.assertEqual([item['id'] for item in self.results(listed)], [self.user.pk])
        patched = self.client.patch(
            f'/api/v1/users/{self.user.pk}/', {'is_active': False, 'must_change_password': False}, format='json',
        )
        self.assertEqual(patched.status_code, status.HTTP_200_OK, patched.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertTrue(self.user.must_change_password)

    def test_login_logout_and_password_changes_are_audited_and_refresh_is_blacklisted(self):
        tokens = self.token_pair()
        self.assertEqual(tokens.status_code, status.HTTP_200_OK, tokens.data)
        self.assertTrue(AuditLog.objects.filter(actor=self.user, action=AuditLog.Action.LOGIN).exists())
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens.data['access']}")
        logged_out = self.client.post('/api/v1/users/logout/', {'refresh': tokens.data['refresh']}, format='json')
        self.assertEqual(logged_out.status_code, status.HTTP_204_NO_CONTENT, logged_out.data)
        self.assertTrue(AuditLog.objects.filter(actor=self.user, action=AuditLog.Action.LOGOUT).exists())
        refreshed = self.client.post('/api/v1/auth/token/refresh/', {'refresh': tokens.data['refresh']}, format='json')
        self.assertEqual(refreshed.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_failed_final_admin_update_does_not_partially_save(self):
        self.other_admin.deactivate()
        self.authenticate()
        result = self.client.patch(
            f'/api/v1/users/{self.admin.pk}/',
            {'first_name': 'Should rollback', 'role': User.Role.RECEPTION}, format='json',
        )
        self.assertEqual(result.status_code, status.HTTP_400_BAD_REQUEST)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.first_name, '')
        self.assertEqual(self.admin.role, User.Role.ADMINISTRATOR)
