from django.db.models import Prefetch
from django.utils import timezone
from rest_framework import decorators, response, status, viewsets
from rest_framework.exceptions import ValidationError

from accounts.models import User
from core.mixins import AuditModelViewSetMixin
from core.permissions import RolePermission
from .models import Patient, PatientNote
from .serializers import (
    CONFIDENTIAL_NOTE_ROLES, DeactivationSerializer, DuplicateCheckSerializer,
    NoteArchiveSerializer, PatientFilterSerializer, PatientListSerializer,
    PatientNoteSerializer, PatientSerializer,
)


PATIENT_ROLES = (
    User.Role.ADMINISTRATOR, User.Role.RECEPTION, User.Role.LABORATORY,
    User.Role.MANAGER, User.Role.CLINICIAN, User.Role.PHARMACY,
)


class PatientViewSet(AuditModelViewSetMixin, viewsets.ModelViewSet):
    queryset = Patient.objects.prefetch_related(
        Prefetch('notes', queryset=PatientNote.objects.select_related('author', 'amended_by', 'archived_by'))
    )
    serializer_class = PatientSerializer
    permission_classes = (RolePermission,)
    read_roles = PATIENT_ROLES
    write_roles = (User.Role.ADMINISTRATOR, User.Role.RECEPTION, User.Role.LABORATORY)
    action_roles = {
        'destroy': (User.Role.ADMINISTRATOR, User.Role.MANAGER),
        'reactivate': (User.Role.ADMINISTRATOR, User.Role.MANAGER),
        'history': (User.Role.ADMINISTRATOR, User.Role.LABORATORY, User.Role.MANAGER, User.Role.CLINICIAN),
        'possible_duplicates': PATIENT_ROLES,
    }
    search_fields = (
        'medical_record_number', 'first_name', 'last_name', 'father_name', 'phone', 'national_id',
    )
    ordering_fields = ('first_name', 'last_name', 'created_at', 'date_of_birth')
    http_method_names = ('get', 'post', 'patch', 'delete', 'head', 'options')

    def get_serializer_class(self):
        if self.action == 'list':
            return PatientListSerializer
        return PatientSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action != 'list':
            return queryset
        keys = set(PatientFilterSerializer().fields)
        raw = {key: value for key, value in self.request.query_params.items() if key in keys}
        filters = PatientFilterSerializer(data=raw)
        filters.is_valid(raise_exception=True)
        data = filters.validated_data
        if not data['include_inactive']:
            queryset = queryset.filter(is_active=data['is_active'])
        if value := data.get('gender'):
            queryset = queryset.filter(gender=value)
        if value := data.get('blood_group'):
            queryset = queryset.filter(blood_group=value)
        if value := data.get('date_of_birth_from'):
            queryset = queryset.filter(date_of_birth__gte=value)
        if value := data.get('date_of_birth_to'):
            queryset = queryset.filter(date_of_birth__lte=value)
        if value := data.get('created_from'):
            queryset = queryset.filter(created_at__date__gte=value)
        if value := data.get('created_to'):
            queryset = queryset.filter(created_at__date__lte=value)
        return queryset

    def perform_update(self, serializer):
        if not serializer.instance.is_active:
            raise ValidationError('Inactive patients must be reactivated before they can be updated.')
        super().perform_update(serializer)

    def destroy(self, request, *args, **kwargs):
        serializer = DeactivationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        patient = self.get_object()
        if not patient.is_active:
            raise ValidationError('Patient is already inactive.')
        patient.deactivate(by=request.user, reason=serializer.validated_data['reason'])
        self._audit('delete', patient, {
            'is_active': {'from': 'True', 'to': 'False'},
            'reason': patient.deactivation_reason,
        })
        return response.Response(status=status.HTTP_204_NO_CONTENT)

    @decorators.action(detail=False, methods=('get',), url_path='possible-duplicates')
    def possible_duplicates(self, request):
        serializer = DuplicateCheckSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        matches = Patient.objects.possible_duplicates(**serializer.validated_data)
        page = self.paginate_queryset(matches)
        items = page if page is not None else matches
        data = PatientListSerializer(items, many=True, context=self.get_serializer_context()).data
        return self.get_paginated_response(data) if page is not None else response.Response(data)

    @decorators.action(detail=True, methods=('get',))
    def history(self, request, pk=None):
        patient = self.get_object()
        try:
            page_number = max(int(request.query_params.get('page', 1)), 1)
            page_size = min(max(int(request.query_params.get('page_size', 25)), 1), 100)
        except (TypeError, ValueError):
            raise ValidationError({'page': 'Page and page_size must be positive integers.'})
        start, end = (page_number - 1) * page_size, page_number * page_size

        visits = patient.visits.select_related('department', 'provider').prefetch_related(
            'service_lines', 'payments__received_by'
        ).order_by('-visit_date')
        labs = patient.lab_orders.select_related('visit', 'ordered_by').prefetch_related(
            'items__test', 'items__resulted_by'
        ).order_by('-ordered_at')
        visit_count, lab_count = visits.count(), labs.count()
        return response.Response({
            'page': page_number,
            'page_size': page_size,
            'visits': {
                'count': visit_count,
                'results': [
                    {
                        'id': str(v.id), 'visit_number': v.visit_number, 'date': v.visit_date,
                        'department': v.department.name, 'provider': v.provider.get_full_name() if v.provider else '',
                        'visit_type': v.visit_type, 'status': v.status, 'total_amount': v.total_amount,
                        'discount_amount': v.discount_amount, 'net_amount': v.net_amount,
                        'paid_amount': v.paid_amount, 'balance_due': v.balance_due,
                        'payment_status': v.payment_status,
                        'services': [
                            {'code': line.service_code, 'name': line.service_name, 'quantity': line.quantity,
                             'unit_price': line.unit_price, 'line_total': line.line_total}
                            for line in v.service_lines.all()
                        ],
                        'payments': [
                            {'id': str(payment.id), 'amount': payment.amount, 'method': payment.method,
                             'paid_at': payment.paid_at, 'received_by': payment.received_by.get_full_name()}
                            for payment in v.payments.all()
                        ],
                    } for v in visits[start:end]
                ],
            },
            'laboratory_orders': {
                'count': lab_count,
                'results': [
                    {
                        'id': str(order.id), 'order_number': order.order_number, 'status': order.status,
                        'ordered_at': order.ordered_at,
                        'visit_number': order.visit.visit_number if order.visit else None,
                        'items': [
                            {'test': item.test.name, 'result': item.result, 'unit': item.result_unit,
                             'reference_range': item.reference_range, 'is_abnormal': item.is_abnormal,
                             'resulted_at': item.resulted_at}
                            for item in order.items.all()
                        ],
                    } for order in labs[start:end]
                ],
            },
        })

    @decorators.action(detail=True, methods=('post',))
    def reactivate(self, request, pk=None):
        patient = self.get_object()
        if patient.is_active:
            raise ValidationError('Patient is already active.')
        patient.reactivate()
        self._audit('update', patient, {'is_active': {'from': 'False', 'to': 'True'}})
        return response.Response(PatientSerializer(patient, context=self.get_serializer_context()).data)


class PatientNoteViewSet(AuditModelViewSetMixin, viewsets.ModelViewSet):
    queryset = PatientNote.objects.select_related('patient', 'author', 'amended_by', 'archived_by')
    serializer_class = PatientNoteSerializer
    permission_classes = (RolePermission,)
    read_roles = (User.Role.ADMINISTRATOR, User.Role.LABORATORY, User.Role.MANAGER, User.Role.CLINICIAN)
    write_roles = (User.Role.ADMINISTRATOR, User.Role.LABORATORY, User.Role.CLINICIAN)
    action_roles = {'destroy': (User.Role.ADMINISTRATOR, User.Role.MANAGER)}
    search_fields = ('patient__medical_record_number', 'patient__first_name', 'note')
    http_method_names = ('get', 'post', 'patch', 'delete', 'head', 'options')

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if not user.has_role(*CONFIDENTIAL_NOTE_ROLES):
            queryset = queryset.filter(is_confidential=False)
        include_archived = self.request.query_params.get('include_archived', '').lower() == 'true'
        if not include_archived or not user.has_role(User.Role.ADMINISTRATOR, User.Role.MANAGER):
            queryset = queryset.filter(is_archived=False)
        if patient := self.request.query_params.get('patient'):
            queryset = queryset.filter(patient_id=patient)
        return queryset

    def perform_create(self, serializer):
        instance = serializer.save(author=self.request.user)
        self._audit('create', instance)

    def perform_update(self, serializer):
        before = {field: getattr(serializer.instance, field, None) for field in serializer.validated_data}
        instance = serializer.save(amended_at=timezone.now(), amended_by=self.request.user)
        changes = {
            field: {'from': str(before[field]), 'to': str(getattr(instance, field, None))}
            for field in before if before[field] != getattr(instance, field, None)
        }
        changes['amended_by'] = str(self.request.user.pk)
        self._audit('update', instance, changes)

    def destroy(self, request, *args, **kwargs):
        serializer = NoteArchiveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        note = self.get_object()
        note.archive(by=request.user, reason=serializer.validated_data['reason'])
        self._audit('delete', note, {
            'is_archived': {'from': 'False', 'to': 'True'}, 'reason': note.archive_reason,
        })
        return response.Response(status=status.HTTP_204_NO_CONTENT)
