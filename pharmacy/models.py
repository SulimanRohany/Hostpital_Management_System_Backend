import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q, Sum
from django.utils import timezone

from core.models import TimeStampedModel, UUIDModel


def document_number(prefix):
    return f'{prefix}-{uuid.uuid4().hex[:12].upper()}'


def purchase_number():
    return document_number('PUR')


def sale_number():
    return document_number('SAL')


class MedicineCategory(UUIDModel, TimeStampedModel):
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ('name',)

    def __str__(self):
        return self.name


class Supplier(UUIDModel, TimeStampedModel):
    name = models.CharField(max_length=160, unique=True)
    contact_person = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    tax_number = models.CharField(max_length=80, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ('name',)

    @property
    def amount_due(self):
        purchases = self.purchases.filter(status=Purchase.Status.POSTED).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
        initial_paid = self.purchases.filter(status=Purchase.Status.POSTED).aggregate(total=Sum('paid_amount'))['total'] or Decimal('0')
        later_paid = self.payments.filter(is_void=False).aggregate(total=Sum('amount'))['total'] or Decimal('0')
        return purchases - initial_paid - later_paid

    def __str__(self):
        return self.name


class Medicine(UUIDModel, TimeStampedModel):
    category = models.ForeignKey(MedicineCategory, on_delete=models.PROTECT, related_name='medicines')
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=180)
    generic_name = models.CharField(max_length=180, blank=True)
    strength = models.CharField(max_length=80, blank=True)
    dosage_form = models.CharField(max_length=80, blank=True)
    unit = models.CharField(max_length=30, default='unit')
    reorder_level = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0'), validators=[MinValueValidator(0)])
    default_sale_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'), validators=[MinValueValidator(0)])
    requires_prescription = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ('name', 'strength')
        constraints = [
            models.UniqueConstraint(fields=('name', 'strength', 'dosage_form'), name='unique_medicine_specification')
        ]

    @property
    def stock_quantity(self):
        return self.usable_stock

    @property
    def physical_stock(self):
        return self.batches.aggregate(total=Sum('quantity_available'))['total'] or Decimal('0')

    @property
    def usable_stock(self):
        return self.batches.filter(
            is_active=True, expiry_date__gt=timezone.localdate(), quantity_available__gt=0,
        ).aggregate(total=Sum('quantity_available'))['total'] or Decimal('0')

    @property
    def expired_stock(self):
        return self.batches.filter(
            expiry_date__lte=timezone.localdate(), quantity_available__gt=0,
        ).aggregate(total=Sum('quantity_available'))['total'] or Decimal('0')

    @property
    def is_low_stock(self):
        return self.usable_stock <= self.reorder_level

    def __str__(self):
        specification = ' '.join(filter(None, (self.name, self.strength, self.dosage_form)))
        return specification


class MedicineBatch(UUIDModel, TimeStampedModel):
    medicine = models.ForeignKey(Medicine, on_delete=models.PROTECT, related_name='batches')
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name='batches')
    batch_number = models.CharField(max_length=80)
    expiry_date = models.DateField(db_index=True)
    purchase_price = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    sale_price = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    quantity_received = models.DecimalField(max_digits=12, decimal_places=3, validators=[MinValueValidator(0)])
    quantity_available = models.DecimalField(max_digits=12, decimal_places=3, validators=[MinValueValidator(0)])
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ('expiry_date', 'created_at')
        constraints = [
            models.UniqueConstraint(fields=('medicine', 'batch_number'), name='unique_batch_per_medicine'),
            models.CheckConstraint(condition=models.Q(quantity_available__lte=models.F('quantity_received')), name='batch_available_not_above_received'),
            models.CheckConstraint(condition=Q(quantity_received__gte=0), name='batch_received_nonnegative'),
            models.CheckConstraint(condition=Q(quantity_available__gte=0), name='batch_available_nonnegative'),
            models.CheckConstraint(condition=Q(sale_price__gte=F('purchase_price')), name='batch_sale_not_below_cost'),
        ]
        indexes = [models.Index(fields=('medicine', 'is_active', 'expiry_date'))]

    def clean(self):
        super().clean()
        if self.quantity_available > self.quantity_received:
            raise ValidationError({'quantity_available': 'Available quantity cannot exceed received quantity.'})
        if self.sale_price < self.purchase_price:
            raise ValidationError({'sale_price': 'Sale price cannot be below purchase cost.'})

    def save(self, *args, **kwargs):
        if self.pk:
            original = MedicineBatch.objects.filter(pk=self.pk).first()
            if original:
                immutable = ('medicine_id', 'supplier_id', 'batch_number', 'purchase_price', 'quantity_received')
                if any(getattr(original, field) != getattr(self, field) for field in immutable):
                    raise ValidationError('A posted batch\'s identity, cost, supplier, and received quantity are immutable.')
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.medicine} / {self.batch_number}'


class Purchase(UUIDModel, TimeStampedModel):
    class Status(models.TextChoices):
        POSTED = 'posted', 'Posted'
        VOID = 'void', 'Void'

    purchase_number = models.CharField(max_length=20, unique=True, default=purchase_number, editable=False)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name='purchases')
    invoice_number = models.CharField(max_length=80)
    purchase_date = models.DateField(db_index=True)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    paid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'), validators=[MinValueValidator(0)])
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.POSTED, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='purchases_created')
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='purchases_voided'
    )
    void_reason = models.TextField(blank=True)

    class Meta:
        ordering = ('-purchase_date', '-created_at')
        constraints = [
            models.UniqueConstraint(fields=('supplier', 'invoice_number'), name='unique_supplier_invoice'),
            models.CheckConstraint(condition=models.Q(paid_amount__lte=models.F('total_amount')), name='purchase_paid_not_above_total'),
            models.CheckConstraint(condition=Q(total_amount__gte=0), name='purchase_total_nonnegative'),
            models.CheckConstraint(condition=Q(paid_amount__gte=0), name='purchase_paid_nonnegative'),
            models.CheckConstraint(
                condition=(Q(status='posted', voided_by__isnull=True, void_reason='') |
                           Q(status='void', voided_by__isnull=False) & ~Q(void_reason='')),
                name='purchase_void_metadata_consistent',
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            original = Purchase.objects.filter(pk=self.pk).first()
            allowed = {'status', 'voided_by_id', 'void_reason'}
            if original:
                changed = {
                    field.attname for field in self._meta.concrete_fields
                    if field.attname not in {'updated_at'} and getattr(original, field.attname) != getattr(self, field.attname)
                }
                if changed - allowed:
                    raise ValidationError('Posted purchases are immutable; void and recreate the purchase.')
        return super().save(*args, **kwargs)

    @property
    def amount_due(self):
        later = self.supplier_payments.filter(is_void=False).aggregate(total=Sum('amount'))['total'] or Decimal('0')
        return self.total_amount - self.paid_amount - later

    def __str__(self):
        return self.purchase_number


class PurchaseLine(UUIDModel):
    purchase = models.ForeignKey(Purchase, on_delete=models.PROTECT, related_name='lines')
    medicine = models.ForeignKey(Medicine, on_delete=models.PROTECT, related_name='purchase_lines')
    batch = models.OneToOneField(MedicineBatch, on_delete=models.PROTECT, related_name='purchase_line')
    quantity = models.DecimalField(max_digits=12, decimal_places=3, validators=[MinValueValidator(Decimal('0.001'))])
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])

    @property
    def line_total(self):
        return self.quantity * self.unit_cost

    def clean(self):
        super().clean()
        if self.batch_id and self.medicine_id and self.batch.medicine_id != self.medicine_id:
            raise ValidationError({'medicine': 'The line medicine must match the batch medicine.'})
        if self.batch_id and self.purchase_id and self.batch.supplier_id != self.purchase.supplier_id:
            raise ValidationError({'batch': 'The batch supplier must match the purchase supplier.'})
        if self.batch_id and self.quantity != self.batch.quantity_received:
            raise ValidationError({'quantity': 'The purchase line quantity must match the batch received quantity.'})
        if self.batch_id and self.purchase_id and self.batch.expiry_date <= self.purchase.purchase_date:
            raise ValidationError({'batch': 'The batch must expire after the purchase date.'})

    def save(self, *args, **kwargs):
        if self.pk and PurchaseLine.objects.filter(pk=self.pk).exists():
            raise ValidationError('Posted purchase lines are immutable.')
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Posted purchase lines cannot be deleted; void the purchase.')

    def __str__(self):
        return f'{self.purchase.purchase_number}: {self.medicine}'


class Sale(UUIDModel, TimeStampedModel):
    class Status(models.TextChoices):
        POSTED = 'posted', 'Posted'
        VOID = 'void', 'Void'

    sale_number = models.CharField(max_length=20, unique=True, default=sale_number, editable=False)
    patient = models.ForeignKey('patients.Patient', null=True, blank=True, on_delete=models.PROTECT, related_name='pharmacy_sales')
    sale_date = models.DateTimeField(db_index=True)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'), validators=[MinValueValidator(0)])
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    paid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'), validators=[MinValueValidator(0)])
    discount_reason = models.CharField(max_length=255, blank=True)
    prescription_reference = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.POSTED, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='sales_created')
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='sales_voided'
    )
    void_reason = models.TextField(blank=True)

    class Meta:
        ordering = ('-sale_date', '-created_at')
        constraints = [
            models.CheckConstraint(condition=models.Q(discount_amount__lte=models.F('subtotal')), name='sale_discount_not_above_subtotal'),
            models.CheckConstraint(condition=models.Q(paid_amount__lte=models.F('total_amount')), name='sale_paid_not_above_total'),
            models.CheckConstraint(condition=Q(subtotal__gte=0), name='sale_subtotal_nonnegative'),
            models.CheckConstraint(condition=Q(total_amount__gte=0), name='sale_total_nonnegative'),
            models.CheckConstraint(condition=Q(paid_amount__gte=0), name='sale_paid_nonnegative'),
            models.CheckConstraint(condition=Q(total_amount=F('subtotal') - F('discount_amount')), name='sale_total_equation'),
            models.CheckConstraint(
                condition=Q(discount_amount=0) | ~Q(discount_reason=''), name='sale_discount_reason_required',
            ),
            models.CheckConstraint(
                condition=(Q(status='posted', voided_by__isnull=True, void_reason='') |
                           Q(status='void', voided_by__isnull=False) & ~Q(void_reason='')),
                name='sale_void_metadata_consistent',
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            original = Sale.objects.filter(pk=self.pk).first()
            allowed = {'status', 'voided_by_id', 'void_reason'}
            if original:
                changed = {
                    field.attname for field in self._meta.concrete_fields
                    if field.attname not in {'updated_at'} and getattr(original, field.attname) != getattr(self, field.attname)
                }
                if changed - allowed:
                    raise ValidationError('Posted sales are immutable; void and recreate the sale.')
        return super().save(*args, **kwargs)

    @property
    def profit(self):
        revenue = sum((line.line_total for line in self.lines.all()), Decimal('0')) - self.discount_amount
        cost = sum((line.quantity * line.unit_cost for line in self.lines.all()), Decimal('0'))
        return revenue - cost

    def __str__(self):
        return self.sale_number


class SaleLine(UUIDModel):
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name='lines')
    batch = models.ForeignKey(MedicineBatch, on_delete=models.PROTECT, related_name='sale_lines')
    quantity = models.DecimalField(max_digits=12, decimal_places=3, validators=[MinValueValidator(Decimal('0.001'))])
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])

    @property
    def medicine(self):
        return self.batch.medicine

    @property
    def line_total(self):
        return self.quantity * self.unit_price

    def clean(self):
        super().clean()
        if self.batch_id:
            if not self.batch.is_active:
                raise ValidationError({'batch': 'Inactive batches cannot be sold.'})
            if self.batch.expiry_date <= timezone.localdate():
                raise ValidationError({'batch': 'Expired batches cannot be sold.'})
            if self.unit_price < self.batch.purchase_price:
                raise ValidationError({'unit_price': 'Sale price cannot be below the batch purchase cost.'})
            if self.unit_cost != self.batch.purchase_price:
                raise ValidationError({'unit_cost': 'Recorded unit cost must match the batch purchase cost.'})

    def save(self, *args, **kwargs):
        if self.pk and SaleLine.objects.filter(pk=self.pk).exists():
            raise ValidationError('Posted sale lines are immutable.')
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Posted sale lines cannot be deleted; void the sale.')

    def __str__(self):
        return f'{self.sale.sale_number}: {self.batch.medicine}'


class StockMovement(UUIDModel):
    class MovementType(models.TextChoices):
        PURCHASE = 'purchase', 'Purchase'
        SALE = 'sale', 'Sale'
        ADJUSTMENT_IN = 'adjustment_in', 'Adjustment in'
        ADJUSTMENT_OUT = 'adjustment_out', 'Adjustment out'
        VOID = 'void', 'Void or reversal'

    batch = models.ForeignKey(MedicineBatch, on_delete=models.PROTECT, related_name='movements')
    movement_type = models.CharField(max_length=20, choices=MovementType.choices, db_index=True)
    quantity_change = models.DecimalField(max_digits=12, decimal_places=3)
    quantity_after = models.DecimalField(max_digits=12, decimal_places=3)
    reference = models.CharField(max_length=120, unique=True)
    reason = models.CharField(max_length=255)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='stock_movements')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ('-created_at',)
        constraints = [
            models.CheckConstraint(condition=Q(quantity_after__gte=0), name='stock_movement_after_nonnegative'),
            models.CheckConstraint(condition=~Q(quantity_change=0), name='stock_movement_change_nonzero'),
        ]

    def save(self, *args, **kwargs):
        if self.pk and StockMovement.objects.filter(pk=self.pk).exists():
            raise ValidationError('Stock movements are immutable.')
        expected_after = self.batch.quantity_available + self.quantity_change
        if self.quantity_change == 0:
            raise ValidationError({'quantity_change': 'Quantity change cannot be zero.'})
        if self.quantity_after != expected_after:
            raise ValidationError({'quantity_after': 'Movement balance does not match the batch balance and change.'})
        if self.quantity_after < 0 or self.quantity_after > self.batch.quantity_received:
            raise ValidationError({'quantity_after': 'Movement balance is outside the valid batch quantity range.'})
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Stock movements cannot be deleted.')

    def __str__(self):
        return f'{self.batch}: {self.quantity_change}'


class SupplierPayment(UUIDModel, TimeStampedModel):
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name='payments')
    purchase = models.ForeignKey(Purchase, null=True, blank=True, on_delete=models.PROTECT, related_name='supplier_payments')
    wallet = models.ForeignKey('finance.Wallet', on_delete=models.PROTECT, related_name='supplier_payments')
    payment_date = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    reference_number = models.CharField(max_length=80, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='supplier_payments_created')
    is_void = models.BooleanField(default=False)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='supplier_payments_voided'
    )
    void_reason = models.TextField(blank=True)

    class Meta:
        ordering = ('-payment_date', '-created_at')
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name='supplier_payment_positive'),
            models.CheckConstraint(
                condition=(Q(is_void=False, voided_by__isnull=True, void_reason='') |
                           Q(is_void=True, voided_by__isnull=False) & ~Q(void_reason='')),
                name='supplier_payment_void_metadata_consistent',
            ),
        ]
        indexes = [models.Index(fields=('supplier', 'is_void', 'payment_date'))]

    def clean(self):
        super().clean()
        if self.purchase_id and self.purchase.supplier_id != self.supplier_id:
            raise ValidationError({'purchase': 'The purchase belongs to another supplier.'})
        if self.purchase_id and self.purchase.status == Purchase.Status.VOID:
            raise ValidationError({'purchase': 'Payments cannot be allocated to a void purchase.'})

    def save(self, *args, **kwargs):
        if self.pk:
            original = SupplierPayment.objects.filter(pk=self.pk).first()
            allowed = {'is_void', 'voided_by_id', 'void_reason'}
            if original:
                changed = {
                    field.attname for field in self._meta.concrete_fields
                    if field.attname not in {'updated_at'} and getattr(original, field.attname) != getattr(self, field.attname)
                }
                if changed - allowed:
                    raise ValidationError('Posted supplier payments are immutable; void and recreate the payment.')
        self.full_clean(exclude=('voided_by',) if self.is_void and not self.voided_by_id else None)
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.supplier}: {self.amount}'
