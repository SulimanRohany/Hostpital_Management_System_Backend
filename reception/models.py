import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import IntegrityError, models, transaction
from django.db.models import F, Q
from django.utils import timezone

from core.models import TimeStampedModel, UUIDModel


ZERO = Decimal('0.00')


def visit_number():
    return f'VIS-{uuid.uuid4().hex[:12].upper()}'


class Visit(UUIDModel, TimeStampedModel):
    class Status(models.TextChoices):
        REGISTERED = 'registered', 'Registered'
        IN_PROGRESS = 'in_progress', 'In progress'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'

    class VisitType(models.TextChoices):
        OUTPATIENT = 'outpatient', 'Outpatient'
        EMERGENCY = 'emergency', 'Emergency'
        FOLLOW_UP = 'follow_up', 'Follow-up'
        INPATIENT = 'inpatient', 'Inpatient'

    ALLOWED_TRANSITIONS = {
        Status.REGISTERED: {Status.IN_PROGRESS, Status.COMPLETED, Status.CANCELLED},
        Status.IN_PROGRESS: {Status.COMPLETED, Status.CANCELLED},
        Status.COMPLETED: {Status.CANCELLED},
        Status.CANCELLED: set(),
    }
    FINANCIAL_FIELDS = ('patient_id', 'department_id', 'visit_date', 'total_amount', 'discount_amount')

    visit_number = models.CharField(max_length=20, unique=True, default=visit_number, editable=False)
    patient = models.ForeignKey('patients.Patient', on_delete=models.PROTECT, related_name='visits')
    department = models.ForeignKey('departments.Department', on_delete=models.PROTECT, related_name='visits')
    provider = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='assigned_visits'
    )
    visit_type = models.CharField(max_length=20, choices=VisitType.choices, default=VisitType.OUTPATIENT, db_index=True)
    room = models.CharField(max_length=60, blank=True)
    referral_source = models.CharField(max_length=150, blank=True)
    visit_date = models.DateTimeField(db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    discount_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(0)]
    )
    paid_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(0)]
    )
    discount_reason = models.CharField(max_length=255, blank=True)
    discount_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name='reception_discounts_approved',
    )
    discount_approved_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.REGISTERED, db_index=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='visits_created')
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='visits_cancelled'
    )
    cancellation_reason = models.TextField(blank=True)

    class Meta:
        ordering = ('-visit_date', '-created_at')
        constraints = [
            models.CheckConstraint(condition=Q(total_amount__gte=0), name='visit_total_nonnegative'),
            models.CheckConstraint(condition=Q(discount_amount__gte=0), name='visit_discount_nonnegative'),
            models.CheckConstraint(condition=Q(paid_amount__gte=0), name='visit_paid_nonnegative'),
            models.CheckConstraint(condition=Q(discount_amount__lte=F('total_amount')), name='visit_discount_not_above_total'),
            models.CheckConstraint(
                condition=Q(paid_amount__lte=F('total_amount') - F('discount_amount')),
                name='visit_paid_not_above_net',
            ),
            models.CheckConstraint(
                condition=Q(discount_amount=0) | ~Q(discount_reason=''), name='visit_discount_reason_required'
            ),
            models.CheckConstraint(
                condition=(
                    Q(status='cancelled', cancelled_by__isnull=False) & ~Q(cancellation_reason='') & Q(cancelled_at__isnull=False)
                ) | (
                    ~Q(status='cancelled') & Q(cancelled_by__isnull=True) & Q(cancellation_reason='') & Q(cancelled_at__isnull=True)
                ),
                name='visit_cancellation_metadata_consistent',
            ),
            models.CheckConstraint(
                condition=(Q(discount_approved_by__isnull=True) & Q(discount_approved_at__isnull=True)) |
                          (Q(discount_approved_by__isnull=False) & Q(discount_approved_at__isnull=False)),
                name='visit_discount_approval_consistent',
            ),
        ]

    @property
    def net_amount(self):
        return self.total_amount - self.discount_amount

    @property
    def balance_due(self):
        return self.net_amount - self.paid_amount

    @property
    def payment_status(self):
        if self.status == self.Status.CANCELLED and self.paid_amount:
            return 'refunded'
        if self.paid_amount <= ZERO:
            return 'unpaid'
        if self.paid_amount < self.net_amount:
            return 'partially_paid'
        return 'paid'

    def clean(self):
        errors = {}
        if self.patient_id and not self.patient.is_active:
            errors['patient'] = 'Inactive patients cannot be registered.'
        if self.department_id and not self.department.is_active:
            errors['department'] = 'Inactive departments cannot receive visits.'
        if self.visit_date and self.visit_date > timezone.now() + timedelta(minutes=5):
            errors['visit_date'] = 'Visit date cannot be in the future.'
        if self.discount_amount > self.total_amount:
            errors['discount_amount'] = 'Discount cannot exceed the service total.'
        if self.discount_amount and not self.discount_reason.strip():
            errors['discount_reason'] = 'A reason is required when a discount is applied.'
        if self.paid_amount > self.net_amount:
            errors['paid_amount'] = 'Paid amount cannot exceed the net amount.'
        if self.status == self.Status.CANCELLED:
            if not self.cancelled_by_id:
                errors['cancelled_by'] = 'The user who cancelled the visit is required.'
            if not self.cancellation_reason.strip():
                errors['cancellation_reason'] = 'A cancellation reason is required.'
            if not self.cancelled_at:
                errors['cancelled_at'] = 'Cancellation time is required.'
        elif self.cancelled_by_id or self.cancellation_reason or self.cancelled_at:
            errors['status'] = 'Cancellation details are only valid for a cancelled visit.'
        if bool(self.discount_approved_by_id) != bool(self.discount_approved_at):
            errors['discount_approved_by'] = 'Discount approver and approval time must be recorded together.'
        if self.status == self.Status.IN_PROGRESS and not self.started_at:
            errors['started_at'] = 'Start time is required for a visit in progress.'
        if self.status == self.Status.COMPLETED and not self.completed_at:
            errors['completed_at'] = 'Completion time is required for a completed visit.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).first()
            if previous:
                changed_financial = [field for field in self.FINANCIAL_FIELDS if getattr(previous, field) != getattr(self, field)]
                if changed_financial and (previous.paid_amount or previous.status != self.Status.REGISTERED):
                    raise ValidationError('Financial details of a paid or processed visit are immutable; cancel and recreate it.')
                if previous.status != self.status and self.status not in self.ALLOWED_TRANSITIONS[previous.status]:
                    raise ValidationError({'status': f'Cannot change visit status from {previous.status} to {self.status}.'})
        self.full_clean()
        if not self._state.adding:
            return super().save(*args, **kwargs)
        for attempt in range(3):
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError:
                if not type(self).objects.filter(visit_number=self.visit_number).exists() or attempt == 2:
                    raise
                self.visit_number = visit_number()

    def recalculate_total(self):
        total = sum((line.line_total for line in self.service_lines.all()), ZERO)
        if self.discount_amount > total or self.paid_amount > total - self.discount_amount:
            raise ValidationError('The service total cannot be reduced below the discount or amount already paid.')
        type(self).objects.filter(pk=self.pk).update(total_amount=total, updated_at=timezone.now())
        self.total_amount = total
        return total

    @transaction.atomic
    def start(self, *, at=None):
        locked = type(self).objects.select_for_update().get(pk=self.pk)
        if locked.department.is_clinical and not locked.provider_id:
            raise ValidationError('A provider must be assigned before completing a clinical visit.')
        locked.status = self.Status.IN_PROGRESS
        locked.started_at = at or timezone.now()
        locked.save(update_fields=('status', 'started_at', 'updated_at'))
        queue_entry = VisitQueueEntry.objects.filter(visit=locked).first()
        if queue_entry and queue_entry.status == VisitQueueEntry.Status.WAITING:
            queue_entry = queue_entry.transition(VisitQueueEntry.Status.CALLED)
        if queue_entry and queue_entry.status == VisitQueueEntry.Status.CALLED:
            queue_entry.transition(VisitQueueEntry.Status.SERVING)
        return locked

    @transaction.atomic
    def complete(self, *, at=None):
        from laboratory.models import LabOrder

        locked = type(self).objects.select_for_update().get(pk=self.pk)
        if locked.lab_orders.exclude(status__in=(LabOrder.Status.COMPLETED, LabOrder.Status.CANCELLED)).exists():
            raise ValidationError('All laboratory orders must be completed or cancelled before completing the visit.')
        if getattr(settings, 'REQUIRE_VISIT_FULL_PAYMENT_ON_COMPLETION', False) and locked.balance_due:
            raise ValidationError('The visit balance must be paid before completion.')
        locked.status = self.Status.COMPLETED
        locked.started_at = locked.started_at or at or timezone.now()
        locked.completed_at = at or timezone.now()
        locked.save(update_fields=('status', 'started_at', 'completed_at', 'updated_at'))
        return locked

    @transaction.atomic
    def collect_payment(self, *, amount, user, method='cash', reference='', paid_at=None, notes=''):
        from finance.models import Wallet, WalletTransaction
        from finance.services import get_system_wallet, post_wallet_entry

        amount = Decimal(amount)
        locked = type(self).objects.select_for_update().get(pk=self.pk)
        if locked.status == self.Status.CANCELLED:
            raise ValidationError('Payments cannot be collected for a cancelled visit.')
        if amount <= ZERO:
            raise ValidationError({'amount': 'Payment amount must be greater than zero.'})
        if amount > locked.balance_due:
            raise ValidationError({'amount': 'Payment cannot exceed the outstanding balance.'})
        payment = VisitPayment.objects.create(
            visit=locked, payment_type=VisitPayment.PaymentType.PAYMENT, amount=amount,
            method=method, transaction_reference=reference, notes=notes, received_by=user,
            paid_at=paid_at or timezone.now(),
        )
        entry = post_wallet_entry(
            wallet=get_system_wallet(Wallet.Kind.RECEPTION), entry_type=WalletTransaction.EntryType.CREDIT,
            amount=amount, category='reception_income', description=f'Reception payment {locked.visit_number}',
            reference=f'visit-payment:{payment.pk}', user=user, source=locked, transaction_date=payment.paid_at,
        )
        payment.wallet_transaction = entry
        payment.save(update_fields=('wallet_transaction',))
        locked.paid_amount += amount
        locked.save(update_fields=('paid_amount', 'updated_at'))
        return payment

    @transaction.atomic
    def cancel(self, *, user, reason, at=None):
        from finance.models import Wallet, WalletTransaction
        from finance.services import get_system_wallet, post_wallet_entry
        from laboratory.models import LabOrder

        locked = type(self).objects.select_for_update().get(pk=self.pk)
        if locked.status == self.Status.CANCELLED:
            raise ValidationError('Visit is already cancelled.')
        reason = reason.strip()
        if not reason:
            raise ValidationError({'reason': 'A cancellation reason is required.'})
        if locked.lab_orders.filter(status=LabOrder.Status.COMPLETED).exists():
            raise ValidationError('A visit with a completed laboratory order cannot be cancelled.')
        cancelled_at = at or timezone.now()
        locked.lab_orders.exclude(status=LabOrder.Status.CANCELLED).update(
            status=LabOrder.Status.CANCELLED, cancelled_by=user, cancelled_at=cancelled_at,
            cancellation_reason=reason, updated_at=cancelled_at,
        )
        locked.status = self.Status.CANCELLED
        locked.cancelled_by = user
        locked.cancellation_reason = reason
        locked.cancelled_at = cancelled_at
        locked.save(update_fields=('status', 'cancelled_by', 'cancellation_reason', 'cancelled_at', 'updated_at'))
        VisitQueueEntry.objects.filter(visit=locked).exclude(status=VisitQueueEntry.Status.CANCELLED).update(
            status=VisitQueueEntry.Status.CANCELLED, finished_at=cancelled_at, updated_at=cancelled_at,
        )
        if locked.paid_amount:
            refund = VisitPayment.objects.create(
                visit=locked, payment_type=VisitPayment.PaymentType.REFUND, amount=locked.paid_amount,
                method=VisitPayment.Method.CASH, notes=reason, received_by=user, paid_at=cancelled_at,
            )
            entry = post_wallet_entry(
                wallet=get_system_wallet(Wallet.Kind.RECEPTION), entry_type=WalletTransaction.EntryType.DEBIT,
                amount=locked.paid_amount, category='reception_refund',
                description=f'Cancelled {locked.visit_number}: {reason[:160]}',
                reference=f'visit-refund:{refund.pk}', user=user, source=locked, transaction_date=cancelled_at,
            )
            refund.wallet_transaction = entry
            refund.save(update_fields=('wallet_transaction',))
        return locked

    def __str__(self):
        return f'{self.visit_number} - {self.patient.full_name}'


class VisitService(UUIDModel):
    visit = models.ForeignKey(Visit, on_delete=models.CASCADE, related_name='service_lines')
    service = models.ForeignKey('departments.Service', on_delete=models.PROTECT, related_name='visit_lines')
    service_code = models.CharField(max_length=30, blank=True, default='', editable=False)
    service_name = models.CharField(max_length=150, blank=True, default='', editable=False)
    standard_fee = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO, editable=False)
    price_override_reason = models.CharField(max_length=255, blank=True)
    price_overridden_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name='reception_prices_overridden',
    )
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('visit', 'service'), name='unique_service_per_visit'),
            models.CheckConstraint(condition=Q(quantity__gt=0), name='visit_service_quantity_positive'),
            models.CheckConstraint(condition=Q(unit_price__gte=0), name='visit_service_price_nonnegative'),
            models.CheckConstraint(
                condition=(Q(unit_price=F('standard_fee'), price_overridden_by__isnull=True, price_override_reason='')) |
                          (~Q(unit_price=F('standard_fee')) & Q(price_overridden_by__isnull=False) & ~Q(price_override_reason='')),
                name='visit_service_override_consistent',
            ),
        ]

    @property
    def line_total(self):
        return self.quantity * self.unit_price

    def clean(self):
        errors = {}
        if self.visit_id and self.service_id:
            if self.service.department_id != self.visit.department_id:
                errors['service'] = 'The service must belong to the visit department.'
            if not self.service.is_active:
                errors['service'] = 'Inactive services cannot be selected.'
            if self.visit.status != Visit.Status.REGISTERED or self.visit.paid_amount:
                errors['visit'] = 'Service lines of a paid or processed visit are immutable.'
        if self.quantity <= 0:
            errors['quantity'] = 'Quantity must be greater than zero.'
        if self.unit_price != self.standard_fee:
            if not self.price_override_reason.strip() or not self.price_overridden_by_id:
                errors['unit_price'] = 'Price overrides require a reason and authorizing user.'
        elif self.price_override_reason or self.price_overridden_by_id:
            errors['unit_price'] = 'Override details are only valid when the price differs from the standard fee.'
        if errors:
            raise ValidationError(errors)

    @transaction.atomic
    def save(self, *args, **kwargs):
        skip_recalculate = kwargs.pop('skip_recalculate', False)
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            previous = type(self).objects.get(pk=self.pk)
            if previous.visit.paid_amount or previous.visit.status != Visit.Status.REGISTERED:
                raise ValidationError('Service lines of a paid or processed visit are immutable.')
        if self.service_id and self._state.adding:
            self.service_code = self.service.code
            self.service_name = self.service.name
            self.standard_fee = self.service.standard_fee
        self.full_clean()
        result = super().save(*args, **kwargs)
        if not skip_recalculate:
            self.visit.recalculate_total()
        return result

    def delete(self, *args, **kwargs):
        visit = self.visit
        if visit.paid_amount or visit.status != Visit.Status.REGISTERED:
            raise ValidationError('Service lines of a paid or processed visit are immutable.')
        result = super().delete(*args, **kwargs)
        visit.recalculate_total()
        return result

    def __str__(self):
        return f'{self.visit.visit_number}: {self.service_name}'


class VisitPayment(UUIDModel):
    class PaymentType(models.TextChoices):
        PAYMENT = 'payment', 'Payment'
        REFUND = 'refund', 'Refund'

    class Method(models.TextChoices):
        CASH = 'cash', 'Cash'
        CARD = 'card', 'Card'
        BANK_TRANSFER = 'bank_transfer', 'Bank transfer'
        MOBILE_MONEY = 'mobile_money', 'Mobile money'
        OTHER = 'other', 'Other'

    visit = models.ForeignKey(Visit, on_delete=models.PROTECT, related_name='payments')
    payment_type = models.CharField(max_length=10, choices=PaymentType.choices, default=PaymentType.PAYMENT)
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    method = models.CharField(max_length=20, choices=Method.choices, default=Method.CASH)
    transaction_reference = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='visit_payments_received')
    paid_at = models.DateTimeField(default=timezone.now, db_index=True)
    wallet_transaction = models.OneToOneField(
        'finance.WalletTransaction', null=True, blank=True, on_delete=models.PROTECT, related_name='visit_payment'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('paid_at', 'created_at')
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name='visit_payment_amount_positive'),
            models.UniqueConstraint(
                fields=('visit', 'transaction_reference'), condition=~Q(transaction_reference=''),
                name='unique_visit_payment_reference',
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            update_fields = set(kwargs.get('update_fields') or ())
            if update_fields != {'wallet_transaction'}:
                raise ValidationError('Visit payments are immutable.')
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Visit payments cannot be deleted; create a refund instead.')

    def __str__(self):
        return f'{self.visit.visit_number} {self.payment_type} {self.amount}'


class VisitQueueEntry(UUIDModel, TimeStampedModel):
    class Status(models.TextChoices):
        WAITING = 'waiting', 'Waiting'
        CALLED = 'called', 'Called'
        SERVING = 'serving', 'Serving'
        FINISHED = 'finished', 'Finished'
        CANCELLED = 'cancelled', 'Cancelled'

    PRIORITY_NORMAL = 0
    PRIORITY_URGENT = 50
    PRIORITY_EMERGENCY = 100

    visit = models.OneToOneField(Visit, on_delete=models.CASCADE, related_name='queue_entry')
    department = models.ForeignKey('departments.Department', on_delete=models.PROTECT, related_name='visit_queue')
    queue_date = models.DateField(default=timezone.localdate, db_index=True)
    token_number = models.PositiveIntegerField()
    priority = models.PositiveSmallIntegerField(default=PRIORITY_NORMAL, db_index=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.WAITING, db_index=True)
    called_at = models.DateTimeField(null=True, blank=True)
    serving_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-priority', 'token_number')
        constraints = [
            models.UniqueConstraint(fields=('department', 'queue_date', 'token_number'), name='unique_daily_department_token'),
            models.CheckConstraint(condition=Q(token_number__gt=0), name='queue_token_positive'),
        ]

    @classmethod
    @transaction.atomic
    def create_for_visit(cls, visit, *, priority=PRIORITY_NORMAL):
        existing = cls.objects.filter(visit=visit).first()
        if existing:
            return existing
        latest = cls.objects.select_for_update().filter(
            department=visit.department, queue_date=timezone.localdate(visit.visit_date)
        ).order_by('-token_number').first()
        return cls.objects.create(
            visit=visit, department=visit.department, queue_date=timezone.localdate(visit.visit_date),
            token_number=(latest.token_number + 1 if latest else 1), priority=priority,
        )

    @transaction.atomic
    def transition(self, status):
        allowed = {
            self.Status.WAITING: {self.Status.CALLED, self.Status.CANCELLED},
            self.Status.CALLED: {self.Status.SERVING, self.Status.CANCELLED},
            self.Status.SERVING: {self.Status.FINISHED, self.Status.CANCELLED},
            self.Status.FINISHED: set(), self.Status.CANCELLED: set(),
        }
        locked = type(self).objects.select_for_update().get(pk=self.pk)
        if status not in allowed[locked.status]:
            raise ValidationError({'status': f'Cannot change queue status from {locked.status} to {status}.'})
        now = timezone.now()
        locked.status = status
        field = {
            self.Status.CALLED: 'called_at', self.Status.SERVING: 'serving_at',
            self.Status.FINISHED: 'finished_at', self.Status.CANCELLED: 'finished_at',
        }.get(status)
        if field:
            setattr(locked, field, now)
        locked.save(update_fields=('status', field, 'updated_at') if field else ('status', 'updated_at'))
        return locked

    def __str__(self):
        return f'{self.department.code}-{self.token_number}'
