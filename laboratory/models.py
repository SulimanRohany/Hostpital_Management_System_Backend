import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.utils import timezone

from core.models import TimeStampedModel, UUIDModel


def order_number():
    return f'LAB-{uuid.uuid4().hex[:12].upper()}'


class LabTest(UUIDModel, TimeStampedModel):
    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=150, unique=True)
    service = models.OneToOneField(
        'departments.Service', null=True, blank=True, on_delete=models.PROTECT, related_name='lab_test'
    )
    specimen_type = models.CharField(max_length=100, blank=True)
    unit = models.CharField(max_length=30, blank=True)
    reference_range = models.CharField(max_length=255, blank=True)
    instructions = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ('name',)

    def clean(self):
        super().clean()
        self.code = (self.code or '').strip().upper()
        self.name = (self.name or '').strip()
        self.specimen_type = (self.specimen_type or '').strip()
        self.unit = (self.unit or '').strip()
        self.reference_range = (self.reference_range or '').strip()
        self.instructions = (self.instructions or '').strip()
        errors = {}
        if not self.code:
            errors['code'] = 'Test code cannot be blank.'
        if not self.name:
            errors['name'] = 'Test name cannot be blank.'
        if self.service_id:
            if not self.service.is_laboratory:
                errors['service'] = 'The linked service must be marked as a laboratory service.'
            elif not self.service.is_active:
                errors['service'] = 'The linked laboratory service must be active.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class LabOrder(UUIDModel, TimeStampedModel):
    class Status(models.TextChoices):
        ORDERED = 'ordered', 'Ordered'
        COLLECTED = 'collected', 'Specimen collected'
        IN_PROGRESS = 'in_progress', 'In progress'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'

    ALLOWED_TRANSITIONS = {
        Status.ORDERED: {Status.COLLECTED, Status.IN_PROGRESS, Status.CANCELLED},
        Status.COLLECTED: {Status.IN_PROGRESS, Status.COMPLETED, Status.CANCELLED},
        Status.IN_PROGRESS: {Status.COMPLETED, Status.CANCELLED},
        Status.COMPLETED: set(),
        Status.CANCELLED: set(),
    }
    IDENTITY_FIELDS = ('patient_id', 'visit_id', 'ordered_by_id', 'ordered_at')

    order_number = models.CharField(max_length=20, unique=True, default=order_number, editable=False)
    patient = models.ForeignKey('patients.Patient', on_delete=models.PROTECT, related_name='lab_orders')
    visit = models.ForeignKey('reception.Visit', null=True, blank=True, on_delete=models.PROTECT, related_name='lab_orders')
    ordered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='lab_orders_created')
    ordered_at = models.DateTimeField(db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ORDERED, db_index=True)
    clinical_notes = models.TextField(blank=True)
    collected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='lab_orders_collected'
    )
    collected_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='lab_orders_cancelled'
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(blank=True)

    class Meta:
        ordering = ('-ordered_at',)
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(status='cancelled', cancelled_by__isnull=False, cancelled_at__isnull=False)
                    & ~Q(cancellation_reason='')
                ) | (
                    ~Q(status='cancelled') & Q(cancelled_by__isnull=True)
                    & Q(cancelled_at__isnull=True) & Q(cancellation_reason='')
                ),
                name='lab_order_cancellation_consistent',
            ),
            models.CheckConstraint(
                condition=(
                    Q(status__in=('collected', 'in_progress', 'completed'), collected_by__isnull=False,
                      collected_at__isnull=False)
                ) | (
                    Q(status__in=('ordered', 'cancelled')) & Q(collected_by__isnull=True, collected_at__isnull=True)
                ) | (
                    Q(status='cancelled', collected_by__isnull=False, collected_at__isnull=False)
                ),
                name='lab_order_collection_consistent',
            ),
        ]

    def clean(self):
        super().clean()
        self.clinical_notes = (self.clinical_notes or '').strip()
        self.cancellation_reason = (self.cancellation_reason or '').strip()
        errors = {}
        if self.patient_id and not self.patient.is_active and self._state.adding:
            errors['patient'] = 'Inactive patients cannot receive new laboratory orders.'
        if self.visit_id:
            if self.patient_id and self.visit.patient_id != self.patient_id:
                errors['visit'] = 'The selected visit belongs to another patient.'
            if self._state.adding and self.visit.status in (self.visit.Status.COMPLETED, self.visit.Status.CANCELLED):
                errors['visit'] = 'Laboratory orders cannot be added to a completed or cancelled visit.'
        if self.ordered_at and self.ordered_at > timezone.now() + timedelta(minutes=5):
            errors['ordered_at'] = 'Order time cannot be in the future.'
        if self.status == self.Status.CANCELLED:
            if not self.cancelled_by_id:
                errors['cancelled_by'] = 'The user who cancelled the order is required.'
            if not self.cancelled_at:
                errors['cancelled_at'] = 'Cancellation time is required.'
            if not self.cancellation_reason:
                errors['cancellation_reason'] = 'A cancellation reason is required.'
        elif self.cancelled_by_id or self.cancelled_at or self.cancellation_reason:
            errors['status'] = 'Cancellation details are only valid for a cancelled order.'
        collection_recorded = bool(self.collected_by_id) and bool(self.collected_at)
        if bool(self.collected_by_id) != bool(self.collected_at):
            errors['collected_by'] = 'Collector and collection time must be recorded together.'
        if self.status in (self.Status.COLLECTED, self.Status.IN_PROGRESS, self.Status.COMPLETED) and not collection_recorded:
            errors['collected_by'] = 'Collection details are required after specimen collection.'
        if self.status == self.Status.ORDERED and collection_recorded:
            errors['status'] = 'An ordered specimen cannot already have collection details.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        previous = None
        if self.pk and not self._state.adding:
            previous = type(self).objects.filter(pk=self.pk).first()
        if previous:
            changed_identity = [field for field in self.IDENTITY_FIELDS if getattr(previous, field) != getattr(self, field)]
            if changed_identity and (previous.status != self.Status.ORDERED or previous.items.filter(resulted_at__isnull=False).exists()):
                raise ValidationError('The patient, visit, ordering user, and order time are immutable after processing starts.')
            if previous.status != self.status and self.status not in self.ALLOWED_TRANSITIONS[previous.status]:
                raise ValidationError({'status': f'Cannot change laboratory order from {previous.status} to {self.status}.'})
        self.full_clean()
        if not self._state.adding:
            return super().save(*args, **kwargs)
        for attempt in range(3):
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError:
                if not type(self).objects.filter(order_number=self.order_number).exists() or attempt == 2:
                    raise
                self.order_number = order_number()

    @classmethod
    @transaction.atomic
    def create_with_items(cls, *, items, **order_data):
        items = list(items)
        if not items:
            raise ValidationError({'items': 'At least one test is required.'})
        test_ids = [item['test'].pk for item in items]
        if len(test_ids) != len(set(test_ids)):
            raise ValidationError({'items': 'The same test cannot be ordered twice.'})
        if any(not item['test'].is_active for item in items):
            raise ValidationError({'items': 'Inactive tests cannot be ordered.'})
        order = cls.objects.create(**order_data)
        for item_data in items:
            LabOrderItem.objects.create(order=order, **item_data)
        return order

    @transaction.atomic
    def collect(self, *, user, at=None):
        locked = type(self).objects.select_for_update().get(pk=self.pk)
        if locked.status != self.Status.ORDERED:
            raise ValidationError({'status': 'Only ordered tests can be marked collected.'})
        locked.status = self.Status.COLLECTED
        locked.collected_by = user
        locked.collected_at = at or timezone.now()
        locked.save(update_fields=('status', 'collected_by', 'collected_at', 'updated_at'))
        return locked

    @transaction.atomic
    def cancel(self, *, user, reason, at=None):
        locked = type(self).objects.select_for_update().get(pk=self.pk)
        if locked.status in (self.Status.COMPLETED, self.Status.CANCELLED):
            raise ValidationError({'status': f'{locked.get_status_display()} orders cannot be cancelled.'})
        reason = (reason or '').strip()
        if not reason:
            raise ValidationError({'reason': 'A cancellation reason is required.'})
        locked.status = self.Status.CANCELLED
        locked.cancelled_by = user
        locked.cancelled_at = at or timezone.now()
        locked.cancellation_reason = reason
        locked.save(update_fields=(
            'status', 'cancelled_by', 'cancelled_at', 'cancellation_reason', 'updated_at',
        ))
        return locked

    @transaction.atomic
    def recalculate_status(self):
        locked = type(self).objects.select_for_update().get(pk=self.pk)
        if locked.status == self.Status.CANCELLED:
            return locked
        items = locked.items.all()
        if not items.exists():
            raise ValidationError({'items': 'A laboratory order must contain at least one test.'})
        if not items.filter(resulted_at__isnull=True).exists():
            new_status = self.Status.COMPLETED
        elif items.filter(resulted_at__isnull=False).exists():
            new_status = self.Status.IN_PROGRESS
        else:
            return locked
        if not locked.collected_at:
            raise ValidationError('The specimen must be collected before results can be recorded.')
        if locked.status != new_status:
            locked.status = new_status
            locked.save(update_fields=('status', 'updated_at'))
        return locked

    def __str__(self):
        return self.order_number


class LabOrderItem(UUIDModel, TimeStampedModel):
    RESULT_FIELDS = ('result', 'result_unit', 'reference_range', 'is_abnormal', 'resulted_by_id', 'resulted_at')

    order = models.ForeignKey(LabOrder, on_delete=models.CASCADE, related_name='items')
    test = models.ForeignKey(LabTest, on_delete=models.PROTECT, related_name='order_items')
    test_code = models.CharField(max_length=30, blank=True, editable=False)
    test_name = models.CharField(max_length=150, blank=True, editable=False)
    specimen_type = models.CharField(max_length=100, blank=True, editable=False)
    result = models.TextField(blank=True)
    result_unit = models.CharField(max_length=30, blank=True)
    reference_range = models.CharField(max_length=255, blank=True)
    is_abnormal = models.BooleanField(default=False)
    resulted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='lab_results_entered'
    )
    resulted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('order', 'test'), name='unique_test_per_lab_order'),
            models.CheckConstraint(
                condition=(
                    ~Q(result='') & Q(resulted_by__isnull=False, resulted_at__isnull=False)
                ) | (
                    Q(result='') & Q(resulted_by__isnull=True, resulted_at__isnull=True, is_abnormal=False)
                ),
                name='lab_result_metadata_consistent',
            ),
        ]

    def clean(self):
        super().clean()
        self.result = (self.result or '').strip()
        self.result_unit = (self.result_unit or '').strip()
        self.reference_range = (self.reference_range or '').strip()
        errors = {}
        has_result = bool(self.result)
        has_metadata = bool(self.resulted_by_id) and bool(self.resulted_at)
        if bool(self.resulted_by_id) != bool(self.resulted_at):
            errors['resulted_by'] = 'Resulting user and time must be recorded together.'
        if has_result != has_metadata:
            errors['result'] = 'A result and its audit metadata must be recorded together.'
        if self.is_abnormal and not has_result:
            errors['is_abnormal'] = 'A result is required before it can be marked abnormal.'
        if self.order_id and self._state.adding:
            if self.order.status != LabOrder.Status.ORDERED:
                errors['order'] = 'Tests can only be added to a newly ordered laboratory order.'
            if self.test_id and not self.test.is_active:
                errors['test'] = 'Inactive tests cannot be ordered.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        allow_result_update = kwargs.pop('_allow_result_update', False)
        previous = None
        if self.pk and not self._state.adding:
            previous = type(self).objects.filter(pk=self.pk).first()
        if previous:
            if previous.order_id != self.order_id or previous.test_id != self.test_id:
                raise ValidationError('The order and test of an existing laboratory item are immutable.')
            if any(getattr(previous, field) != getattr(self, field) for field in self.RESULT_FIELDS) and not allow_result_update:
                raise ValidationError('Results must be changed through record_result().')
        elif self.test_id:
            self.test_code = self.test.code
            self.test_name = self.test.name
            self.specimen_type = self.test.specimen_type
            self.result_unit = self.result_unit or self.test.unit
            self.reference_range = self.reference_range or self.test.reference_range
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.order.status != LabOrder.Status.ORDERED or self.order.items.filter(resulted_at__isnull=False).exists():
            raise ValidationError('Tests cannot be removed after laboratory processing starts.')
        return super().delete(*args, **kwargs)

    @transaction.atomic
    def record_result(self, *, user, result, result_unit=None, reference_range=None, is_abnormal=False, at=None):
        item = type(self).objects.select_for_update().select_related('order').get(pk=self.pk)
        order = LabOrder.objects.select_for_update().get(pk=item.order_id)
        if order.status in (LabOrder.Status.CANCELLED, LabOrder.Status.COMPLETED):
            raise ValidationError({'result': f'Results cannot be changed on a {order.status} order.'})
        if order.status == LabOrder.Status.ORDERED:
            collection_time = at or timezone.now()
            order.status = LabOrder.Status.COLLECTED
            order.collected_by = user
            order.collected_at = collection_time
            order.save(update_fields=('status', 'collected_by', 'collected_at', 'updated_at'))
        result = (result or '').strip()
        if not result:
            raise ValidationError({'result': 'Result cannot be blank.'})
        item.result = result
        if result_unit is not None:
            item.result_unit = result_unit
        if reference_range is not None:
            item.reference_range = reference_range
        item.is_abnormal = is_abnormal
        item.resulted_by = user
        item.resulted_at = at or timezone.now()
        item.save(_allow_result_update=True)
        item.order = order.recalculate_status()
        return item

    def __str__(self):
        return f'{self.order.order_number}: {self.test_name or self.test.name}'
