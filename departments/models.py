from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models.functions import Lower
from django.utils import timezone

from core.models import TimeStampedModel, UUIDModel


class CatalogQuerySet(models.QuerySet):
    """Prevent bulk operations from bypassing catalog lifecycle invariants."""

    guarded_fields = frozenset()

    def delete(self):
        raise ValidationError('Catalog records are archival; deactivate them instead of deleting them.')

    def update(self, **kwargs):
        guarded = self.guarded_fields.intersection(kwargs)
        if guarded:
            raise ValidationError(
                f'{", ".join(sorted(guarded))} must be changed through model lifecycle methods.'
            )
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        guarded = self.guarded_fields.intersection(fields)
        if guarded:
            raise ValidationError(
                f'{", ".join(sorted(guarded))} cannot be changed with bulk_update().' 
            )
        return super().bulk_update(objs, fields, batch_size=batch_size)


class DepartmentQuerySet(CatalogQuerySet):
    guarded_fields = frozenset({'is_active', 'is_clinical', 'code'})


class ServiceQuerySet(CatalogQuerySet):
    guarded_fields = frozenset({
        'is_active', 'is_laboratory', 'department', 'department_id', 'code', 'name', 'standard_fee',
    })


class ImmutableHistoryQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError('Service fee history is immutable.')

    def delete(self):
        raise ValidationError('Service fee history is immutable.')

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError('Service fee history is immutable.')


class Department(UUIDModel, TimeStampedModel):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    is_clinical = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True, db_index=True)

    objects = DepartmentQuerySet.as_manager()

    class Meta:
        ordering = ('name',)
        constraints = [
            models.UniqueConstraint(Lower('code'), name='unique_department_code_ci'),
            models.UniqueConstraint(Lower('name'), name='unique_department_name_ci'),
        ]

    @property
    def has_active_services(self):
        return self.services.filter(is_active=True).exists()

    @property
    def can_be_deactivated(self):
        return not self.deactivation_blockers

    @property
    def deactivation_blockers(self):
        """Return current operational dependencies; historical records do not block archival."""
        blockers = []
        if self.services.filter(is_active=True).exists():
            blockers.append('active services')
        if self.users.filter(is_active=True).exists():
            blockers.append('active users')
        if self.employees.exclude(status='terminated').exists():
            blockers.append('current employees')
        if self.employee_assignments.filter(end_date__isnull=True).exists():
            blockers.append('current employment assignments')
        if self.visits.filter(status__in=('registered', 'in_progress')).exists():
            blockers.append('open visits')
        if self.visit_queue.filter(status__in=('waiting', 'called', 'serving')).exists():
            blockers.append('active queue entries')
        return tuple(blockers)

    def clean(self):
        super().clean()
        self.code = (self.code or '').strip().upper()
        self.name = (self.name or '').strip()
        self.description = (self.description or '').strip()
        errors = {}
        if not self.code:
            errors['code'] = 'Department code cannot be blank.'
        if not self.name:
            errors['name'] = 'Department name cannot be blank.'
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values('is_active', 'is_clinical').first()
            if previous and previous['is_active'] and not self.is_active and self.has_active_services:
                errors['is_active'] = 'Deactivate all active services before deactivating this department.'
            blockers = tuple(item for item in self.deactivation_blockers if item != 'active services')
            if previous and previous['is_active'] and not self.is_active and blockers:
                errors['is_active'] = f'Department has operational dependencies: {", ".join(blockers)}.'
            if previous and previous['is_clinical'] and not self.is_clinical and self.services.filter(
                is_active=True, is_laboratory=True
            ).exists():
                errors['is_clinical'] = 'A department with active laboratory services must remain clinical.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    @transaction.atomic
    def deactivate(self, *, deactivate_services=False):
        if not self.pk:
            raise ValidationError('An unsaved department cannot be deactivated.')
        locked = type(self).objects.select_for_update().get(pk=self.pk)
        active_services = locked.services.filter(is_active=True)
        if active_services.exists() and not deactivate_services:
            raise ValidationError({
                'is_active': 'This department has active services; explicitly deactivate them as part of this operation.'
            })
        if deactivate_services:
            for service in active_services.select_for_update():
                service.deactivate()
        blockers = tuple(item for item in locked.deactivation_blockers if item != 'active services')
        if blockers:
            raise ValidationError({
                'is_active': f'Department has operational dependencies: {", ".join(blockers)}.'
            })
        if not locked.is_active:
            self.is_active = False
            return locked
        locked.is_active = False
        locked.save(update_fields=('is_active', 'updated_at'))
        self.is_active = False
        return locked

    @transaction.atomic
    def activate(self):
        if not self.pk:
            raise ValidationError('An unsaved department cannot be activated.')
        locked = type(self).objects.select_for_update().get(pk=self.pk)
        if not locked.is_active:
            locked.is_active = True
            locked.save(update_fields=('is_active', 'updated_at'))
        self.is_active = True
        return locked

    def delete(self, *args, **kwargs):
        raise ValidationError('Departments are archival records; deactivate them instead of deleting them.')

    def __str__(self):
        return self.name


class Service(UUIDModel, TimeStampedModel):
    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name='services')
    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=150)
    standard_fee = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(0)]
    )
    is_laboratory = models.BooleanField(default=False)
    is_discountable = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True, db_index=True)

    objects = ServiceQuerySet.as_manager()

    class Meta:
        ordering = ('department__name', 'name')
        constraints = [
            models.UniqueConstraint(fields=('department', 'name'), name='unique_service_name_per_department'),
            models.UniqueConstraint(Lower('code'), name='unique_service_code_ci'),
            models.UniqueConstraint(
                Lower('name'), 'department', name='unique_service_name_per_department_ci'
            ),
            models.CheckConstraint(condition=models.Q(standard_fee__gte=0), name='service_fee_nonnegative'),
        ]

    @property
    def is_available(self):
        if not self.is_active or not self.department.is_active:
            return False
        if not self.is_laboratory:
            return True
        try:
            return self.lab_test.is_active
        except ObjectDoesNotExist:
            return False

    def clean(self):
        super().clean()
        self.code = (self.code or '').strip().upper()
        self.name = (self.name or '').strip()
        errors = {}
        if not self.code:
            errors['code'] = 'Service code cannot be blank.'
        if not self.name:
            errors['name'] = 'Service name cannot be blank.'
        if self.is_active and self.department_id and not self.department.is_active:
            errors['department'] = 'An active service cannot belong to an inactive department.'
        if self.is_active and self.is_laboratory and self.department_id and not self.department.is_clinical:
            errors['is_laboratory'] = 'An active laboratory service must belong to a clinical department.'

        previous = None
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                'code', 'department_id', 'is_laboratory', 'is_active'
            ).first()
        if previous:
            has_visit_lines = self.visit_lines.exists()
            try:
                lab_test = self.lab_test
            except ObjectDoesNotExist:
                lab_test = None
            is_in_use = has_visit_lines or lab_test is not None
            if is_in_use and previous['code'] != self.code:
                errors['code'] = 'The code of a service already in use cannot be changed.'
            if is_in_use and previous['department_id'] != self.department_id:
                errors['department'] = 'The department of a service already in use cannot be changed.'
            previous_name = type(self).objects.filter(pk=self.pk).values_list('name', flat=True).first()
            if is_in_use and previous_name != self.name:
                errors['name'] = 'The name of a service already in use cannot be changed.'
            if previous['is_laboratory'] and not self.is_laboratory and lab_test is not None:
                errors['is_laboratory'] = 'A service linked to a laboratory test must remain a laboratory service.'
            if previous['is_active'] and not self.is_active and lab_test is not None and lab_test.is_active:
                errors['is_active'] = 'Deactivate the linked laboratory test before deactivating this service.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        previous_fee = None
        if self.pk and not self._state.adding:
            previous_fee = type(self).objects.filter(pk=self.pk).values_list('standard_fee', flat=True).first()
        self.full_clean()
        fee_is_being_saved = 'update_fields' not in kwargs or 'standard_fee' in kwargs['update_fields']
        if (
            previous_fee is not None and fee_is_being_saved and previous_fee != self.standard_fee
            and not (getattr(self, '_fee_change_reason', '') or '').strip()
        ):
            raise ValidationError({
                'standard_fee': 'Use change_standard_fee() and provide a reason when changing a standard fee.'
            })
        with transaction.atomic():
            result = super().save(*args, **kwargs)
            if previous_fee is None:
                ServiceFeeHistory.objects.create(
                    service=self, previous_fee=None, new_fee=self.standard_fee, reason='Initial standard fee',
                    changed_by=getattr(self, '_fee_changed_by', None),
                )
            elif fee_is_being_saved and previous_fee != self.standard_fee:
                ServiceFeeHistory.objects.create(
                    service=self, previous_fee=previous_fee, new_fee=self.standard_fee,
                    reason=self._fee_change_reason,
                    changed_by=getattr(self, '_fee_changed_by', None),
                )
        return result

    @transaction.atomic
    def deactivate(self):
        if not self.pk:
            raise ValidationError('An unsaved service cannot be deactivated.')
        locked = type(self).objects.select_for_update().select_related('department').get(pk=self.pk)
        type(self.department).objects.select_for_update().get(pk=locked.department_id)
        if not locked.is_active:
            self.is_active = False
            return locked
        locked.is_active = False
        locked.save(update_fields=('is_active', 'updated_at'))
        self.is_active = False
        return locked

    @transaction.atomic
    def activate(self):
        if not self.pk:
            raise ValidationError('An unsaved service cannot be activated.')
        locked = type(self).objects.select_for_update().select_related('department').get(pk=self.pk)
        department = type(self.department).objects.select_for_update().get(pk=locked.department_id)
        locked.department = department
        if not locked.is_active:
            locked.is_active = True
            locked.save(update_fields=('is_active', 'updated_at'))
        self.is_active = True
        return locked

    def change_standard_fee(self, *, new_fee, reason, changed_by=None):
        reason = (reason or '').strip()
        if not reason:
            raise ValidationError({'reason': 'A reason is required when changing a standard fee.'})
        self.standard_fee = Decimal(new_fee)
        self._fee_change_reason = reason
        self._fee_changed_by = changed_by
        try:
            self.save(update_fields=('standard_fee', 'updated_at'))
        finally:
            self.__dict__.pop('_fee_change_reason', None)
            self.__dict__.pop('_fee_changed_by', None)
        return self

    def delete(self, *args, **kwargs):
        raise ValidationError('Services are archival records; deactivate them instead of deleting them.')

    def __str__(self):
        return f'{self.department.name} - {self.name}'


class ServiceFeeHistory(UUIDModel):
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name='fee_history')
    previous_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    new_fee = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    reason = models.CharField(max_length=255)
    changed_by = models.ForeignKey(
        'accounts.User', null=True, blank=True, on_delete=models.PROTECT,
        related_name='service_fee_changes',
    )
    changed_at = models.DateTimeField(default=timezone.now, db_index=True, editable=False)

    objects = ImmutableHistoryQuerySet.as_manager()

    class Meta:
        ordering = ('-changed_at',)
        constraints = [
            models.CheckConstraint(condition=models.Q(new_fee__gte=0), name='service_fee_history_nonnegative'),
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError('Service fee history is immutable.')
        self.reason = (self.reason or '').strip()
        if not self.reason:
            raise ValidationError({'reason': 'A fee change reason is required.'})
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Service fee history is immutable.')

    def __str__(self):
        return f'{self.service.code}: {self.previous_fee} -> {self.new_fee}'
