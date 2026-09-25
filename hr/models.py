import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from core.models import TimeStampedModel, UUIDModel


def employee_number():
    return f'EMP-{uuid.uuid4().hex[:10].upper()}'


def default_working_days():
    return [0, 1, 2, 3, 4]


class ValidatedModelMixin:
    """Ensure domain rules apply outside DRF too (admin, scripts and jobs)."""

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Employee(ValidatedModelMixin, UUIDModel, TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        LEAVE = 'leave', 'On leave'
        SUSPENDED = 'suspended', 'Suspended'
        TERMINATED = 'terminated', 'Terminated'

    employee_number = models.CharField(max_length=20, unique=True, default=employee_number, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='employee_profile'
    )
    department = models.ForeignKey('departments.Department', on_delete=models.PROTECT, related_name='employees')
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    father_name = models.CharField(max_length=150, blank=True)
    job_title = models.CharField(max_length=120)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    national_id = models.CharField(max_length=50, blank=True, unique=True, null=True)
    hire_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    salary = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'), validators=[MinValueValidator(0)])
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.ACTIVE, db_index=True)

    class Meta:
        ordering = ('first_name', 'last_name')
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_date__isnull=True) | models.Q(end_date__gte=models.F('hire_date')),
                name='employee_end_not_before_hire',
            ),
            models.CheckConstraint(
                condition=~models.Q(status='terminated') | models.Q(end_date__isnull=False),
                name='terminated_employee_has_end_date',
            ),
        ]

    def clean(self):
        errors = {}
        if self.end_date and self.hire_date and self.end_date < self.hire_date:
            errors['end_date'] = 'End date cannot be before hire date.'
        if self.status == self.Status.TERMINATED and not self.end_date:
            errors['end_date'] = 'A terminated employee must have an end date.'
        if self.end_date and self.end_date < timezone.localdate() and self.status != self.Status.TERMINATED:
            errors['status'] = 'An employee whose employment has ended must be terminated.'
        if self.user_id and self.user.department_id and self.user.department_id != self.department_id:
            errors['user'] = 'The linked user must belong to the same department.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        previous_salary = None
        if self.pk:
            previous_salary = type(self).objects.filter(pk=self.pk).values_list('salary', flat=True).first()
        # Retry the very unlikely random-number collision without weakening uniqueness.
        for attempt in range(3):
            try:
                with transaction.atomic():
                    result = super().save(*args, **kwargs)
                break
            except IntegrityError as exc:
                if attempt == 2 or not self._state.adding:
                    raise
                self.employee_number = employee_number()
        if self.status == self.Status.TERMINATED and self.user_id and self.user.is_active:
            type(self.user).objects.filter(pk=self.user_id).update(is_active=False)
            self.user.is_active = False
        if previous_salary != self.salary:
            effective_date = self.hire_date if previous_salary is None else timezone.localdate()
            SalaryHistory.objects.update_or_create(
                employee=self,
                effective_from=effective_date,
                defaults={'amount': self.salary, 'reason': 'Initial salary' if previous_salary is None else 'Salary updated'},
            )
        return result

    def is_employed_on(self, date):
        return self.hire_date <= date and (self.end_date is None or date <= self.end_date)

    @property
    def full_name(self):
        return f'{self.first_name} {self.last_name}'.strip()

    def __str__(self):
        return f'{self.employee_number} - {self.full_name}'


class Attendance(ValidatedModelMixin, UUIDModel, TimeStampedModel):
    class Status(models.TextChoices):
        PRESENT = 'present', 'Present'
        ABSENT = 'absent', 'Absent'
        LEAVE = 'leave', 'Leave'
        HALF_DAY = 'half_day', 'Half day'

    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='attendance_records')
    date = models.DateField(db_index=True)
    check_in = models.TimeField(null=True, blank=True)
    check_out = models.TimeField(null=True, blank=True)
    check_out_date = models.DateField(null=True, blank=True, help_text='Set for overnight shifts; defaults to attendance date.')
    status = models.CharField(max_length=15, choices=Status.choices)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ('-date', 'employee__first_name')
        constraints = [models.UniqueConstraint(fields=('employee', 'date'), name='unique_employee_attendance_date')]

    def clean(self):
        errors = {}
        if self.employee_id and self.date and not self.employee.is_employed_on(self.date):
            errors['date'] = 'Attendance must fall within the employee employment period.'
        if self.date and self.date > timezone.localdate():
            errors['date'] = 'Attendance cannot be recorded in the future.'
        if self.employee_id and self.employee.status == Employee.Status.TERMINATED:
            errors['employee'] = 'Attendance cannot be recorded for a terminated employee.'
        if self.check_out and not self.check_in:
            errors['check_out'] = 'Check-out requires a check-in.'
        if self.check_out_date and not self.check_out:
            errors['check_out_date'] = 'A check-out date requires a check-out time.'
        if self.check_out_date and self.date and self.check_out_date < self.date:
            errors['check_out_date'] = 'Check-out date cannot precede the attendance date.'
        if self.status in (self.Status.ABSENT, self.Status.LEAVE) and (self.check_in or self.check_out):
            errors['status'] = 'Absent or leave records cannot contain check-in/out times.'
        if self.status in (self.Status.PRESENT, self.Status.HALF_DAY) and not self.check_in:
            errors['check_in'] = 'Present and half-day records require a check-in.'
        if self.check_in and self.check_out:
            out_date = self.check_out_date or self.date
            if out_date and datetime.combine(out_date, self.check_out) <= datetime.combine(self.date, self.check_in):
                errors['check_out'] = 'Check-out must be after check-in.'
        if errors:
            raise ValidationError(errors)

    @property
    def worked_duration(self):
        if not self.check_in or not self.check_out:
            return None
        return datetime.combine(self.check_out_date or self.date, self.check_out) - datetime.combine(self.date, self.check_in)

    def __str__(self):
        return f'{self.employee} {self.date}'


class PayrollRecord(ValidatedModelMixin, UUIDModel, TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        APPROVED = 'approved', 'Approved'
        PAID = 'paid', 'Paid'
        CANCELLED = 'cancelled', 'Cancelled'

    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='payroll_records')
    period_start = models.DateField()
    period_end = models.DateField()
    base_salary = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(0)])
    allowances = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'), validators=[MinValueValidator(0)])
    deductions = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'), validators=[MinValueValidator(0)])
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ('-period_end',)
        constraints = [
            models.CheckConstraint(condition=models.Q(period_end__gte=models.F('period_start')), name='payroll_period_valid'),
            models.CheckConstraint(
                condition=models.Q(deductions__lte=models.F('base_salary') + models.F('allowances')),
                name='payroll_deductions_not_above_gross',
            ),
            models.UniqueConstraint(fields=('employee', 'period_start', 'period_end'), name='unique_employee_payroll_period'),
        ]

    def clean(self):
        errors = {}
        if self.period_start and self.period_end and self.period_end < self.period_start:
            errors['period_end'] = 'Period end cannot be before period start.'
        if self.deductions > self.gross_pay:
            errors['deductions'] = 'Deductions cannot exceed gross pay.'
        if self.employee_id and self.period_start and self.period_end:
            if not self.employee.is_employed_on(self.period_start) or not self.employee.is_employed_on(self.period_end):
                errors['period_start'] = 'Payroll period must fall within the employee employment period.'
            overlaps = type(self).objects.filter(
                employee_id=self.employee_id,
                period_start__lte=self.period_end,
                period_end__gte=self.period_start,
            ).exclude(pk=self.pk).exclude(status=self.Status.CANCELLED)
            if overlaps.exists() and self.status != self.Status.CANCELLED:
                errors['period_start'] = 'This payroll period overlaps another payroll record.'
        if self.status == self.Status.PAID and not self.paid_at:
            errors['paid_at'] = 'Paid payroll requires a payment timestamp.'
        if self.paid_at and self.status != self.Status.PAID:
            errors['status'] = 'A payment timestamp is only valid for paid payroll.'
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).first()
            if previous and previous.status == self.Status.PAID:
                protected = ('employee_id', 'period_start', 'period_end', 'base_salary', 'allowances', 'deductions')
                if any(getattr(previous, field) != getattr(self, field) for field in protected):
                    errors['status'] = 'Financial fields of paid payroll cannot be changed.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.status == self.Status.PAID and not self.paid_at:
            self.paid_at = timezone.now()
        if self.status != self.Status.PAID:
            self.paid_at = None
        return super().save(*args, **kwargs)

    @property
    def gross_pay(self):
        return self.base_salary + self.allowances

    @property
    def net_pay(self):
        return self.gross_pay - self.deductions

    def __str__(self):
        return f'{self.employee} {self.period_start} - {self.period_end}'


class LeaveRequest(ValidatedModelMixin, UUIDModel, TimeStampedModel):
    class LeaveType(models.TextChoices):
        ANNUAL = 'annual', 'Annual'
        SICK = 'sick', 'Sick'
        UNPAID = 'unpaid', 'Unpaid'
        MATERNITY = 'maternity', 'Maternity'
        PATERNITY = 'paternity', 'Paternity'
        OTHER = 'other', 'Other'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'
        CANCELLED = 'cancelled', 'Cancelled'

    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='leave_requests')
    leave_type = models.CharField(max_length=15, choices=LeaveType.choices)
    start_date = models.DateField()
    end_date = models.DateField()
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='reviewed_leave_requests')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True)

    class Meta:
        ordering = ('-start_date',)

    def clean(self):
        errors = {}
        if self.start_date and self.end_date and self.end_date < self.start_date:
            errors['end_date'] = 'Leave end date cannot precede its start date.'
        if self.employee_id and self.start_date and self.end_date:
            if not self.employee.is_employed_on(self.start_date) or not self.employee.is_employed_on(self.end_date):
                errors['start_date'] = 'Leave must fall within the employment period.'
            overlaps = type(self).objects.filter(employee_id=self.employee_id, start_date__lte=self.end_date, end_date__gte=self.start_date).exclude(pk=self.pk).exclude(status__in=(self.Status.REJECTED, self.Status.CANCELLED))
            if overlaps.exists() and self.status not in (self.Status.REJECTED, self.Status.CANCELLED):
                errors['start_date'] = 'This leave overlaps an existing request.'
        reviewed = self.status in (self.Status.APPROVED, self.Status.REJECTED)
        if reviewed and (not self.reviewed_by_id or not self.reviewed_at):
            errors['reviewed_by'] = 'Approved or rejected leave requires reviewer details.'
        if errors:
            raise ValidationError(errors)

    @property
    def total_days(self):
        return (self.end_date - self.start_date).days + 1

    def __str__(self):
        return f'{self.employee} {self.start_date} - {self.end_date}'


class WorkShift(ValidatedModelMixin, UUIDModel, TimeStampedModel):
    name = models.CharField(max_length=100, unique=True)
    start_time = models.TimeField()
    end_time = models.TimeField()
    break_minutes = models.PositiveIntegerField(default=0)
    working_days = models.JSONField(
        default=default_working_days,
        help_text='ISO-style weekday numbers: Monday=0 through Sunday=6.',
    )
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ('name',)

    @property
    def crosses_midnight(self):
        return self.end_time <= self.start_time

    @property
    def scheduled_duration(self):
        today = timezone.localdate()
        end_date = today + timedelta(days=1) if self.crosses_midnight else today
        return datetime.combine(end_date, self.end_time) - datetime.combine(today, self.start_time) - timedelta(minutes=self.break_minutes)

    def clean(self):
        errors = {}
        if self.scheduled_duration <= timedelta(0):
            errors['break_minutes'] = 'Break cannot consume the entire shift.'
        if not isinstance(self.working_days, list) or not self.working_days:
            errors['working_days'] = 'At least one working day is required.'
        elif any(type(day) is not int or day < 0 or day > 6 for day in self.working_days):
            errors['working_days'] = 'Working days must be integers from 0 (Monday) through 6 (Sunday).'
        elif len(self.working_days) != len(set(self.working_days)):
            errors['working_days'] = 'Working days cannot contain duplicates.'
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.name


class ShiftAssignment(ValidatedModelMixin, UUIDModel, TimeStampedModel):
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='shift_assignments')
    shift = models.ForeignKey(WorkShift, on_delete=models.PROTECT, related_name='assignments')
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ('-start_date',)

    def clean(self):
        errors = {}
        if self.end_date and self.end_date < self.start_date:
            errors['end_date'] = 'Assignment end date cannot precede its start date.'
        if self.employee_id and not self.employee.is_employed_on(self.start_date):
            errors['start_date'] = 'Assignment must begin during employment.'
        if errors:
            raise ValidationError(errors)


class Holiday(ValidatedModelMixin, UUIDModel, TimeStampedModel):
    name = models.CharField(max_length=150)
    date = models.DateField(db_index=True)
    department = models.ForeignKey('departments.Department', null=True, blank=True, on_delete=models.CASCADE, related_name='holidays')
    is_paid = models.BooleanField(default=True)

    class Meta:
        ordering = ('date',)
        constraints = [models.UniqueConstraint(fields=('date', 'department'), name='unique_holiday_date_department')]

    def __str__(self):
        return f'{self.name} ({self.date})'


class SalaryHistory(ValidatedModelMixin, UUIDModel, TimeStampedModel):
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='salary_history')
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(0)])
    effective_from = models.DateField()
    reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ('-effective_from',)
        constraints = [models.UniqueConstraint(fields=('employee', 'effective_from'), name='unique_salary_effective_date')]

    def clean(self):
        if self.employee_id and not self.employee.is_employed_on(self.effective_from):
            raise ValidationError({'effective_from': 'Salary change must occur during employment.'})

    def save(self, *args, **kwargs):
        result = super().save(*args, **kwargs)
        latest = type(self).objects.filter(employee_id=self.employee_id).order_by('-effective_from', '-created_at').first()
        if latest and latest.pk == self.pk:
            Employee.objects.filter(pk=self.employee_id).update(salary=self.amount)
        return result


class PayrollComponent(ValidatedModelMixin, UUIDModel, TimeStampedModel):
    class Kind(models.TextChoices):
        ALLOWANCE = 'allowance', 'Allowance'
        BONUS = 'bonus', 'Bonus'
        OVERTIME = 'overtime', 'Overtime'
        TAX = 'tax', 'Tax'
        ADVANCE = 'advance', 'Advance repayment'
        DEDUCTION = 'deduction', 'Other deduction'

    payroll = models.ForeignKey(PayrollRecord, on_delete=models.CASCADE, related_name='components')
    kind = models.CharField(max_length=12, choices=Kind.choices)
    description = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])

    class Meta:
        ordering = ('kind', 'description')

    def clean(self):
        if self.payroll_id and self.payroll.status == PayrollRecord.Status.PAID:
            raise ValidationError({'payroll': 'Components of paid payroll cannot be changed.'})


class EmploymentDocument(ValidatedModelMixin, UUIDModel, TimeStampedModel):
    class DocumentType(models.TextChoices):
        CONTRACT = 'contract', 'Contract'
        IDENTITY = 'identity', 'Identity'
        CERTIFICATE = 'certificate', 'Certificate'
        LICENSE = 'license', 'Professional license'
        OTHER = 'other', 'Other'

    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='documents')
    document_type = models.CharField(max_length=15, choices=DocumentType.choices)
    title = models.CharField(max_length=200)
    file = models.FileField(upload_to='hr/employee-documents/%Y/%m/')
    issued_on = models.DateField(null=True, blank=True)
    expires_on = models.DateField(null=True, blank=True)

    def clean(self):
        if self.issued_on and self.expires_on and self.expires_on < self.issued_on:
            raise ValidationError({'expires_on': 'Expiry date cannot precede issue date.'})


class EmploymentAssignment(ValidatedModelMixin, UUIDModel, TimeStampedModel):
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='assignment_history')
    department = models.ForeignKey('departments.Department', on_delete=models.PROTECT, related_name='employee_assignments')
    job_title = models.CharField(max_length=120)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ('-start_date',)

    def clean(self):
        errors = {}
        if self.end_date and self.end_date < self.start_date:
            errors['end_date'] = 'Assignment end date cannot precede its start date.'
        if self.employee_id and not self.employee.is_employed_on(self.start_date):
            errors['start_date'] = 'Assignment must begin during employment.'
        if self.employee_id:
            upper = self.end_date or datetime.max.date()
            overlaps = type(self).objects.filter(employee_id=self.employee_id, start_date__lte=upper).filter(models.Q(end_date__isnull=True) | models.Q(end_date__gte=self.start_date)).exclude(pk=self.pk)
            if overlaps.exists():
                errors['start_date'] = 'This assignment overlaps an existing employment assignment.'
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f'{self.employee} - {self.job_title}'
