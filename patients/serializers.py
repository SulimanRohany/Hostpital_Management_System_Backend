from django.utils import timezone
from rest_framework import serializers

from accounts.models import User
from .models import Patient, PatientNote, calculate_age, normalize_phone


CONFIDENTIAL_NOTE_ROLES = (User.Role.ADMINISTRATOR, User.Role.MANAGER, User.Role.CLINICIAN)
NOTE_READ_ROLES = CONFIDENTIAL_NOTE_ROLES + (User.Role.LABORATORY,)


class PatientNoteSerializer(serializers.ModelSerializer):
    author_name = serializers.CharField(source='author.get_full_name', read_only=True)
    amended_by_name = serializers.CharField(source='amended_by.get_full_name', read_only=True)
    archived_by_name = serializers.CharField(source='archived_by.get_full_name', read_only=True)
    patient_name = serializers.CharField(source='patient.full_name', read_only=True)
    medical_record_number = serializers.CharField(source='patient.medical_record_number', read_only=True)

    class Meta:
        model = PatientNote
        fields = '__all__'
        read_only_fields = (
            'id', 'author', 'amended_at', 'amended_by', 'is_archived', 'archived_at',
            'archived_by', 'archive_reason', 'created_at', 'updated_at',
        )

    def validate(self, attrs):
        patient = attrs.get('patient', getattr(self.instance, 'patient', None))
        if self.instance and 'patient' in attrs and attrs['patient'].pk != self.instance.patient_id:
            raise serializers.ValidationError({'patient': 'A note cannot be moved to another patient.'})
        if self.instance and self.instance.is_archived:
            raise serializers.ValidationError('Archived notes cannot be amended.')
        if patient and not patient.is_active:
            raise serializers.ValidationError({'patient': 'Notes cannot be added to an inactive patient.'})
        note = attrs.get('note', getattr(self.instance, 'note', ''))
        if not note or not note.strip():
            raise serializers.ValidationError({'note': 'Patient note cannot be blank.'})
        attrs['note'] = note.strip()
        return attrs


class PatientListSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    age = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta:
        model = Patient
        fields = (
            'id', 'medical_record_number', 'first_name', 'last_name', 'full_name', 'father_name',
            'age', 'date_of_birth', 'gender', 'phone', 'blood_group', 'is_active', 'created_at',
        )


class PatientSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    age = serializers.IntegerField(read_only=True, allow_null=True)
    notes = serializers.SerializerMethodField()
    confirm_possible_duplicate = serializers.BooleanField(default=False, write_only=True, required=False)

    class Meta:
        model = Patient
        fields = '__all__'
        read_only_fields = (
            'id', 'medical_record_number', 'age_years', 'is_active', 'deactivated_at', 'deactivated_by',
            'deactivation_reason', 'created_at', 'updated_at',
        )

    def get_notes(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return []
        if not request.user.has_role(*NOTE_READ_ROLES):
            return []
        notes = obj.notes.filter(is_archived=False)
        if not request.user.has_role(*CONFIDENTIAL_NOTE_ROLES):
            notes = notes.filter(is_confidential=False)
        return PatientNoteSerializer(notes, many=True, context=self.context).data

    def validate(self, attrs):
        dob = attrs.get('date_of_birth', getattr(self.instance, 'date_of_birth', None))
        if dob and dob > timezone.localdate():
            raise serializers.ValidationError({'date_of_birth': 'Date of birth cannot be in the future.'})
        if dob and calculate_age(dob) > 150:
            raise serializers.ValidationError({'date_of_birth': 'Patient age cannot exceed 150 years.'})

        for field in ('phone', 'emergency_contact_phone'):
            if field in attrs:
                attrs[field] = normalize_phone(attrs[field])
                digits = attrs[field].lstrip('+')
                if attrs[field] and not 7 <= len(digits) <= 15:
                    raise serializers.ValidationError({field: 'Enter a phone number containing 7 to 15 digits.'})

        for field in ('first_name', 'last_name', 'father_name'):
            if field in attrs:
                attrs[field] = attrs[field].strip()
        if not attrs.get('first_name', getattr(self.instance, 'first_name', '')):
            raise serializers.ValidationError({'first_name': 'First name cannot be blank.'})
        if 'national_id' in attrs:
            attrs['national_id'] = attrs['national_id'].strip().upper()
        if 'blood_group' in attrs:
            attrs['blood_group'] = attrs['blood_group'].strip().upper()

        national_id = attrs.get('national_id', getattr(self.instance, 'national_id', ''))
        if national_id:
            duplicate = Patient.objects.filter(national_id__iexact=national_id)
            if self.instance:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise serializers.ValidationError({'national_id': 'A patient with this national ID already exists.'})

        confirmed = attrs.pop('confirm_possible_duplicate', False)
        if not self.instance and not confirmed:
            matches = Patient.objects.possible_duplicates(
                first_name=attrs.get('first_name', ''), father_name=attrs.get('father_name', ''),
                date_of_birth=dob, phone=attrs.get('phone', ''),
            )[:10]
            match_data = [
                {'id': str(item.pk), 'medical_record_number': item.medical_record_number, 'full_name': item.full_name}
                for item in matches
            ]
            if match_data:
                raise serializers.ValidationError({
                    'possible_duplicates': match_data,
                    'confirm_possible_duplicate': 'Set this field to true after confirming this is a different patient.',
                })
        return attrs


class PatientFilterSerializer(serializers.Serializer):
    is_active = serializers.BooleanField(required=False, default=True)
    include_inactive = serializers.BooleanField(required=False, default=False)
    gender = serializers.ChoiceField(choices=Patient.Gender.choices, required=False)
    blood_group = serializers.ChoiceField(choices=Patient.BloodGroup.choices, required=False)
    date_of_birth_from = serializers.DateField(required=False)
    date_of_birth_to = serializers.DateField(required=False)
    created_from = serializers.DateField(required=False)
    created_to = serializers.DateField(required=False)

    def validate(self, attrs):
        for start, end in (('date_of_birth_from', 'date_of_birth_to'), ('created_from', 'created_to')):
            if attrs.get(start) and attrs.get(end) and attrs[start] > attrs[end]:
                raise serializers.ValidationError({end: 'End date must not be before start date.'})
        return attrs


class DuplicateCheckSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=100, trim_whitespace=True)
    father_name = serializers.CharField(max_length=150, required=False, allow_blank=True, trim_whitespace=True)
    date_of_birth = serializers.DateField(required=False)
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True)

    def validate_phone(self, value):
        return normalize_phone(value)


class DeactivationSerializer(serializers.Serializer):
    reason = serializers.CharField(min_length=3, max_length=2000, trim_whitespace=True)


class NoteArchiveSerializer(serializers.Serializer):
    reason = serializers.CharField(min_length=3, max_length=2000, trim_whitespace=True)
