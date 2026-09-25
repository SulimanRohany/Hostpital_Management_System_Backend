from django.contrib.auth.models import AbstractUser, UserManager as DjangoUserManager
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import models


class UserManager(DjangoUserManager):
    """Create hospital users with consistent credentials and account flags."""

    def _create_user(self, username, email, password, **extra_fields):
        if not password:
            raise ValueError('A password is required.')
        return super()._create_user(username, email, password, **extra_fields)

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault('role', self.model.Role.ADMINISTRATOR)
        extra_fields.setdefault('must_change_password', False)
        if extra_fields.get('role') != self.model.Role.ADMINISTRATOR:
            raise ValueError('A superuser must have the administrator role.')
        return super().create_superuser(username, email, password, **extra_fields)


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMINISTRATOR = 'administrator', 'System administrator'
        RECEPTION = 'reception', 'Reception'
        PHARMACY = 'pharmacy', 'Pharmacy'
        LABORATORY = 'laboratory', 'Laboratory'
        FINANCE = 'finance', 'Finance'
        MANAGER = 'manager', 'Manager'
        HR = 'hr', 'Human resources'
        CLINICIAN = 'clinician', 'Clinician'

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.RECEPTION, db_index=True)
    roles = models.JSONField(default=list, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    department = models.ForeignKey(
        'departments.Department', null=True, blank=True, on_delete=models.SET_NULL, related_name='users'
    )
    must_change_password = models.BooleanField(default=True)

    objects = UserManager()

    class Meta(AbstractUser.Meta):
        ordering = ('first_name', 'last_name', 'username')
        constraints = [
            models.CheckConstraint(
                condition=models.Q(is_superuser=False) | models.Q(role='administrator'),
                name='superuser_has_administrator_role',
            ),
        ]
        indexes = [models.Index(fields=('is_active', 'role'), name='user_active_role_idx')]

    @property
    def display_name(self):
        return self.get_full_name() or self.username

    @property
    def is_administrator(self):
        return self.has_role(self.Role.ADMINISTRATOR)

    def has_role(self, *roles):
        return self.is_superuser or bool(set(self.assigned_roles).intersection(roles))

    @property
    def assigned_roles(self):
        """All assigned sections, including the legacy primary role."""
        if self.is_superuser or self.role == self.Role.ADMINISTRATOR or self.Role.ADMINISTRATOR in (self.roles or []):
            return [value for value, _label in self.Role.choices]
        return list(dict.fromkeys([*(self.roles or []), self.role]))

    def clean(self):
        super().clean()
        errors = {}
        self.email = self.__class__.objects.normalize_email(self.email)

        valid_roles = {value for value, _label in self.Role.choices}
        previous_role = None
        if self.pk:
            previous_role = type(self).objects.filter(pk=self.pk).values_list('role', flat=True).first()
        if previous_role == self.Role.ADMINISTRATOR and self.role != self.Role.ADMINISTRATOR:
            errors['role'] = 'The system administrator role and its section access cannot be changed.'
        # Preserve compatibility for code that explicitly changes the legacy role field.
        if previous_role and self.role != previous_role and self.roles == [previous_role]:
            self.roles = [self.role]
        assigned_roles = list(dict.fromkeys(self.roles or [self.role]))
        invalid_roles = [role for role in assigned_roles if role not in valid_roles]
        if invalid_roles:
            errors['roles'] = f"Unknown roles: {', '.join(invalid_roles)}."
        elif not assigned_roles:
            errors['roles'] = 'Assign at least one role.'
        else:
            if previous_role == self.Role.ADMINISTRATOR and self.Role.ADMINISTRATOR not in assigned_roles:
                errors['roles'] = 'System administrator section access is fixed and cannot be changed.'
            if self.Role.ADMINISTRATOR in assigned_roles:
                assigned_roles = [value for value, _label in self.Role.choices]
            # Keep role as a primary-role compatibility field for reports and old clients.
            self.roles = assigned_roles
            self.role = self.Role.ADMINISTRATOR if self.Role.ADMINISTRATOR in assigned_roles else assigned_roles[0]

        if self.is_superuser and self.Role.ADMINISTRATOR not in self.roles:
            errors['roles'] = 'A superuser must have the administrator role.'
        if self.is_active and self.department_id and not self.department.is_active:
            errors['department'] = 'An active user cannot belong to an inactive department.'

        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values('is_active', 'is_superuser', 'role').first()
            was_active_administrator = previous and previous['is_active'] and (
                previous['is_superuser'] or previous['role'] == self.Role.ADMINISTRATOR
            )
            if was_active_administrator and (not self.is_active or not self.is_administrator):
                has_another = type(self).objects.filter(is_active=True).filter(
                    models.Q(is_superuser=True) | models.Q(role=self.Role.ADMINISTRATOR)
                ).exclude(pk=self.pk).exists()
                if not has_another:
                    errors['role'] = 'The final active administrator cannot be deactivated or demoted.'
            try:
                employee = self.employee_profile
            except (AttributeError, ObjectDoesNotExist):
                employee = None
            if employee and self.department_id and employee.department_id != self.department_id:
                errors['department'] = 'The user and linked employee must belong to the same department.'

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def set_temporary_password(self, raw_password):
        self.set_password(raw_password)
        self.must_change_password = True
        self.save(update_fields=('password', 'must_change_password'))

    def change_password(self, raw_password):
        self.set_password(raw_password)
        self.must_change_password = False
        self.save(update_fields=('password', 'must_change_password'))

    def deactivate(self, *, by=None):
        if by is not None and by.pk == self.pk:
            raise ValidationError({'is_active': 'You cannot deactivate your own account.'})
        if self.is_administrator and self.is_active:
            other_administrators = type(self).objects.filter(is_active=True).filter(
                models.Q(is_superuser=True) | models.Q(role=self.Role.ADMINISTRATOR)
            ).exclude(pk=self.pk)
            if not other_administrators.exists():
                raise ValidationError({'is_active': 'The final active administrator cannot be deactivated.'})
        self.is_active = False
        self.save(update_fields=('is_active',))

    def activate(self):
        self.is_active = True
        self.save(update_fields=('is_active',))

    def __str__(self):
        return self.display_name
