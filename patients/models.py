import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from core.models import TimeStampedModel, UUIDModel


def generate_mrn():
    # 64 random bits fit the field and make collisions practically impossible.
    return f'MRN-{uuid.uuid4().hex[:16].upper()}'


def calculate_age(date_of_birth, today=None):
    if not date_of_birth:
        return None
    today = today or timezone.localdate()
    return today.year - date_of_birth.year - (
        (today.month, today.day) < (date_of_birth.month, date_of_birth.day)
    )


def normalize_phone(value):
    value = (value or '').strip()
    if not value:
        return ''
    prefix = '+' if value.startswith('+') else ''
    return prefix + ''.join(character for character in value if character.isdigit())


class PatientQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True)

    def possible_duplicates(self, *, first_name, father_name='', date_of_birth=None, phone=''):
        """Return likely duplicate records; callers decide whether a match is genuine."""
        candidates = self.filter(first_name__iexact=(first_name or '').strip())
        identity_match = models.Q()
        if father_name:
            identity_match |= models.Q(father_name__iexact=father_name.strip())
        if date_of_birth:
            identity_match |= models.Q(date_of_birth=date_of_birth)
        normalized_phone = normalize_phone(phone)
        if normalized_phone:
            identity_match |= models.Q(phone=normalized_phone)
        return candidates.filter(identity_match) if identity_match else candidates.none()


class Patient(UUIDModel, TimeStampedModel):
    class Gender(models.TextChoices):
        MALE = 'male', 'Male'
        FEMALE = 'female', 'Female'
        OTHER = 'other', 'Other'
        UNKNOWN = 'unknown', 'Unknown'

    class BloodGroup(models.TextChoices):
        A_POSITIVE = 'A+', 'A+'
        A_NEGATIVE = 'A-', 'A-'
        B_POSITIVE = 'B+', 'B+'
        B_NEGATIVE = 'B-', 'B-'
        AB_POSITIVE = 'AB+', 'AB+'
        AB_NEGATIVE = 'AB-', 'AB-'
        O_POSITIVE = 'O+', 'O+'
        O_NEGATIVE = 'O-', 'O-'

    medical_record_number = models.CharField(max_length=20, unique=True, default=generate_mrn, editable=False)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    father_name = models.CharField(max_length=150, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    age_years = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MaxValueValidator(150)])
    gender = models.CharField(max_length=10, choices=Gender.choices, default=Gender.UNKNOWN)
    phone = models.CharField(max_length=30, blank=True, db_index=True)
    national_id = models.CharField(max_length=50, blank=True, db_index=True)
    address = models.TextField(blank=True)
    blood_group = models.CharField(max_length=3, choices=BloodGroup.choices, blank=True)
    allergies = models.TextField(blank=True)
    emergency_contact_name = models.CharField(max_length=150, blank=True)
    emergency_contact_phone = models.CharField(max_length=30, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    deactivated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name='deactivated_patients',
    )
    deactivation_reason = models.TextField(blank=True)

    objects = PatientQuerySet.as_manager()

    class Meta:
        ordering = ('first_name', 'last_name', 'created_at')
        indexes = [models.Index(fields=('first_name', 'last_name', 'father_name'))]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(age_years__isnull=True) | models.Q(age_years__lte=150),
                name='patient_age_at_most_150',
            ),
            models.UniqueConstraint(
                Lower('national_id'),
                condition=~models.Q(national_id=''),
                name='patient_national_id_ci_unique',
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        self.first_name = (self.first_name or '').strip()
        self.last_name = (self.last_name or '').strip()
        self.father_name = (self.father_name or '').strip()
        self.national_id = (self.national_id or '').strip().upper()
        self.phone = normalize_phone(self.phone)
        self.emergency_contact_phone = normalize_phone(self.emergency_contact_phone)
        self.blood_group = (self.blood_group or '').strip().upper()

        if not self.first_name:
            errors['first_name'] = 'First name cannot be blank.'
        if self.date_of_birth and self.date_of_birth > timezone.localdate():
            errors['date_of_birth'] = 'Date of birth cannot be in the future.'
        if self.date_of_birth:
            actual_age = calculate_age(self.date_of_birth)
            if actual_age > 150:
                errors['date_of_birth'] = 'Patient age cannot exceed 150 years.'
            if self.age_years is not None and abs(actual_age - self.age_years) > 1:
                errors['age_years'] = 'Age does not match the date of birth.'
        if self.phone and not (7 <= len(self.phone.lstrip('+')) <= 15):
            errors['phone'] = 'Enter a phone number containing 7 to 15 digits.'
        if self.emergency_contact_phone and not (7 <= len(self.emergency_contact_phone.lstrip('+')) <= 15):
            errors['emergency_contact_phone'] = 'Enter a phone number containing 7 to 15 digits.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.clean()
        if self.date_of_birth:
            self.age_years = calculate_age(self.date_of_birth)
        return super().save(*args, **kwargs)

    @property
    def age(self):
        return calculate_age(self.date_of_birth) if self.date_of_birth else self.age_years

    def deactivate(self, *, by=None, reason=''):
        if not self.is_active:
            return
        self.is_active = False
        self.deactivated_at = timezone.now()
        self.deactivated_by = by
        self.deactivation_reason = (reason or '').strip()
        self.save(update_fields=(
            'is_active', 'deactivated_at', 'deactivated_by', 'deactivation_reason', 'updated_at',
        ))

    def reactivate(self):
        self.is_active = True
        self.deactivated_at = None
        self.deactivated_by = None
        self.deactivation_reason = ''
        self.save(update_fields=(
            'is_active', 'deactivated_at', 'deactivated_by', 'deactivation_reason', 'updated_at',
        ))

    @property
    def full_name(self):
        return f'{self.first_name} {self.last_name}'.strip()

    def __str__(self):
        return f'{self.medical_record_number} - {self.full_name}'


class PatientNote(UUIDModel, TimeStampedModel):
    class NoteType(models.TextChoices):
        GENERAL = 'general', 'General'
        CLINICAL = 'clinical', 'Clinical'
        ALLERGY = 'allergy', 'Allergy'
        ADMINISTRATIVE = 'administrative', 'Administrative'

    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name='notes')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='patient_notes')
    note_type = models.CharField(max_length=20, choices=NoteType.choices, default=NoteType.GENERAL)
    note = models.TextField()
    is_confidential = models.BooleanField(default=True)
    amended_at = models.DateTimeField(null=True, blank=True)
    amended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name='amended_patient_notes',
    )
    is_archived = models.BooleanField(default=False, db_index=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name='archived_patient_notes',
    )
    archive_reason = models.TextField(blank=True)

    class Meta:
        ordering = ('-created_at',)

    def clean(self):
        super().clean()
        self.note = (self.note or '').strip()
        if not self.note:
            raise ValidationError({'note': 'Patient note cannot be blank.'})

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)

    def archive(self, *, by, reason):
        if self.is_archived:
            return
        self.is_archived = True
        self.archived_at = timezone.now()
        self.archived_by = by
        self.archive_reason = (reason or '').strip()
        self.save(update_fields=('is_archived', 'archived_at', 'archived_by', 'archive_reason', 'updated_at'))

    def __str__(self):
        return f'Note for {self.patient.medical_record_number}'
