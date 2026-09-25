from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import F
from rest_framework import decorators, response, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError

from accounts.models import User
from core.mixins import AuditModelViewSetMixin
from core.permissions import RolePermission
from .models import Visit, VisitQueueEntry
from .serializers import (
    CancellationSerializer, CollectPaymentSerializer, QueueFilterSerializer, QueueTransitionSerializer,
    VisitCreateSerializer, VisitDetailSerializer, VisitFilterSerializer, VisitPaymentSerializer,
    VisitQueueSerializer, VisitUpdateSerializer,
)


def as_api_error(exc):
    return ValidationError(getattr(exc, 'message_dict', None) or getattr(exc, 'messages', None) or str(exc))


class VisitViewSet(AuditModelViewSetMixin, viewsets.ModelViewSet):
    queryset = Visit.objects.select_related(
        'patient', 'department', 'provider', 'created_by', 'cancelled_by', 'discount_approved_by',
        'queue_entry',
    ).prefetch_related('service_lines__service', 'payments__received_by', 'lab_orders')
    serializer_class = VisitDetailSerializer
    permission_classes = (RolePermission,)
    read_roles = (
        User.Role.ADMINISTRATOR, User.Role.RECEPTION, User.Role.LABORATORY, User.Role.FINANCE,
        User.Role.MANAGER, User.Role.CLINICIAN,
    )
    write_roles = (User.Role.ADMINISTRATOR, User.Role.RECEPTION)
    search_fields = (
        'visit_number', 'patient__medical_record_number', 'patient__first_name',
        'patient__last_name', 'patient__father_name', 'department__name',
    )
    ordering_fields = ('visit_date', 'total_amount', 'paid_amount', 'created_at')
    http_method_names = ('get', 'post', 'patch', 'head', 'options')

    ACTION_WRITE_ROLES = {
        'create': (User.Role.ADMINISTRATOR, User.Role.RECEPTION, User.Role.MANAGER),
        'partial_update': (User.Role.ADMINISTRATOR, User.Role.RECEPTION, User.Role.MANAGER),
        'cancel': (User.Role.ADMINISTRATOR, User.Role.RECEPTION, User.Role.MANAGER),
        'collect_payment': (User.Role.ADMINISTRATOR, User.Role.RECEPTION, User.Role.FINANCE),
        'start_visit': (User.Role.ADMINISTRATOR, User.Role.CLINICIAN, User.Role.MANAGER),
        'complete_visit': (User.Role.ADMINISTRATOR, User.Role.CLINICIAN, User.Role.MANAGER),
        'queue_transition': (User.Role.ADMINISTRATOR, User.Role.RECEPTION, User.Role.CLINICIAN),
    }

    def get_permissions(self):
        self.write_roles = self.ACTION_WRITE_ROLES.get(getattr(self, 'action', None), self.write_roles)
        return super().get_permissions()

    def get_serializer_class(self):
        if self.action == 'create':
            return VisitCreateSerializer
        if self.action in ('update', 'partial_update'):
            return VisitUpdateSerializer
        return VisitDetailSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        detail = VisitDetailSerializer(serializer.instance, context=self.get_serializer_context())
        return response.Response(detail.data, status=status.HTTP_201_CREATED, headers=self.get_success_headers(detail.data))

    def perform_create(self, serializer):
        visit = serializer.save()
        overrides = [
            {'service': line.service_code, 'standard_fee': str(line.standard_fee), 'charged_fee': str(line.unit_price),
             'reason': line.price_override_reason, 'authorized_by': str(line.price_overridden_by_id)}
            for line in visit.service_lines.all() if line.unit_price != line.standard_fee
        ]
        self._audit('create', visit, {
            'discount': {'amount': str(visit.discount_amount), 'reason': visit.discount_reason,
                         'approved_by': str(visit.discount_approved_by_id or '')},
            'price_overrides': overrides, 'provider': str(visit.provider_id or ''),
            'queue_token': visit.queue_entry.token_number,
        })

    def get_queryset(self):
        qs = super().get_queryset()
        # A clinician-only user gets a limited worklist of their own visits. An
        # administrator or multi-role user should keep full reception access.
        clinician_only = set(self.request.user.assigned_roles) == {User.Role.CLINICIAN}
        if clinician_only:
            qs = qs.filter(provider=self.request.user)
        if getattr(self, 'action', None) not in ('list',):
            return qs
        keys = set(VisitFilterSerializer().fields)
        raw = {key: value for key, value in self.request.query_params.items() if key in keys}
        filters = VisitFilterSerializer(data=raw)
        filters.is_valid(raise_exception=True)
        data = filters.validated_data
        if exact_date := data.get('date'):
            qs = qs.filter(visit_date__date=exact_date)
        if date_from := data.get('visit_date_from'):
            qs = qs.filter(visit_date__date__gte=date_from)
        if date_to := data.get('visit_date_to'):
            qs = qs.filter(visit_date__date__lte=date_to)
        for field in ('department', 'patient', 'provider', 'status', 'visit_type'):
            if value := data.get(field):
                qs = qs.filter(**{f'{field}_id' if field in ('department', 'patient', 'provider') else field: value})
        payment_status = data.get('payment_status')
        if payment_status == 'unpaid':
            qs = qs.exclude(status=Visit.Status.CANCELLED).filter(paid_amount=0)
        elif payment_status == 'partially_paid':
            qs = qs.exclude(status=Visit.Status.CANCELLED).filter(paid_amount__gt=0, paid_amount__lt=F('total_amount') - F('discount_amount'))
        elif payment_status == 'paid':
            qs = qs.exclude(status=Visit.Status.CANCELLED).filter(paid_amount=F('total_amount') - F('discount_amount'))
        elif payment_status == 'refunded':
            qs = qs.filter(status=Visit.Status.CANCELLED, paid_amount__gt=0)
        return qs

    @decorators.action(detail=True, methods=('post',))
    def cancel(self, request, pk=None):
        serializer = CancellationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        visit = self.get_object()
        privileged = request.user.has_role(User.Role.ADMINISTRATOR, User.Role.MANAGER)
        if visit.status != Visit.Status.REGISTERED and not privileged:
            raise PermissionDenied('Only a manager may cancel an in-progress or completed visit.')
        if visit.paid_amount > Decimal(settings.RECEPTION_MAX_REFUND_AMOUNT) and not privileged:
            raise PermissionDenied('This refund amount requires manager approval.')
        old_status = visit.status
        try:
            visit = visit.cancel(user=request.user, reason=serializer.validated_data['reason'])
        except DjangoValidationError as exc:
            raise as_api_error(exc)
        self._audit('update', visit, {
            'status': {'from': old_status, 'to': visit.status}, 'reason': visit.cancellation_reason,
            'refund_amount': str(visit.paid_amount), 'cancelled_by': str(request.user.pk),
        })
        return response.Response(VisitDetailSerializer(visit, context=self.get_serializer_context()).data)

    @decorators.action(detail=True, methods=('post',), url_path='start')
    def start_visit(self, request, pk=None):
        visit = self.get_object()
        old_status = visit.status
        try:
            visit = visit.start()
        except DjangoValidationError as exc:
            raise as_api_error(exc)
        self._audit('update', visit, {'status': {'from': old_status, 'to': visit.status}, 'started_at': str(visit.started_at)})
        return response.Response(VisitDetailSerializer(visit, context=self.get_serializer_context()).data)

    @decorators.action(detail=True, methods=('post',), url_path='complete')
    def complete_visit(self, request, pk=None):
        visit = self.get_object()
        old_status = visit.status
        try:
            visit = visit.complete()
        except DjangoValidationError as exc:
            raise as_api_error(exc)
        if hasattr(visit, 'queue_entry') and visit.queue_entry.status == VisitQueueEntry.Status.SERVING:
            visit.queue_entry.transition(VisitQueueEntry.Status.FINISHED)
        self._audit('update', visit, {'status': {'from': old_status, 'to': visit.status}, 'completed_at': str(visit.completed_at)})
        return response.Response(VisitDetailSerializer(visit, context=self.get_serializer_context()).data)

    @decorators.action(detail=True, methods=('get', 'post'), url_path='payments')
    def collect_payment(self, request, pk=None):
        visit = self.get_object()
        if request.method == 'GET':
            page = self.paginate_queryset(visit.payments.all())
            serialized = VisitPaymentSerializer(page if page is not None else visit.payments.all(), many=True).data
            return self.get_paginated_response(serialized) if page is not None else response.Response(serialized)
        serializer = CollectPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            payment = visit.collect_payment(
                amount=data['amount'], user=request.user, method=data['method'],
                reference=data.get('transaction_reference', ''), notes=data.get('notes', ''),
            )
        except DjangoValidationError as exc:
            raise as_api_error(exc)
        self._audit('update', payment.visit, {
            'payment': {'amount': str(payment.amount), 'method': payment.method,
                        'reference': payment.transaction_reference, 'received_by': str(request.user.pk)}
        })
        return response.Response(VisitPaymentSerializer(payment).data, status=status.HTTP_201_CREATED)

    @decorators.action(detail=True, methods=('get',), url_path='receipt')
    def receipt(self, request, pk=None):
        visit = self.get_object()
        data = VisitDetailSerializer(visit, context=self.get_serializer_context()).data
        return response.Response({
            'receipt_number': visit.visit_number, 'issued_at': visit.created_at,
            'patient': {'id': str(visit.patient_id), 'name': visit.patient.full_name,
                        'medical_record_number': visit.patient.medical_record_number},
            'department': visit.department.name, 'provider': visit.provider.get_full_name() if visit.provider else '',
            'visit_date': visit.visit_date, 'services': data['service_lines'],
            'total_amount': visit.total_amount, 'discount_amount': visit.discount_amount,
            'discount_reason': visit.discount_reason, 'net_amount': visit.net_amount,
            'paid_amount': visit.paid_amount, 'balance_due': visit.balance_due,
            'payment_status': visit.payment_status, 'payments': data['payments'],
            'status': visit.status, 'cancellation_reason': visit.cancellation_reason,
            'created_by': visit.created_by.get_full_name() or visit.created_by.username,
        })

    @decorators.action(detail=False, methods=('get',), url_path='queue')
    def queue(self, request):
        keys = set(QueueFilterSerializer().fields)
        raw = {key: value for key, value in request.query_params.items() if key in keys}
        serializer = QueueFilterSerializer(data=raw)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        qs = VisitQueueEntry.objects.select_related('visit__patient', 'department').filter(queue_date=data['date'])
        clinician_only = set(request.user.assigned_roles) == {User.Role.CLINICIAN}
        if clinician_only:
            qs = qs.filter(visit__provider=request.user)
        if department := data.get('department'):
            qs = qs.filter(department_id=department)
        if queue_status := data.get('status'):
            qs = qs.filter(status=queue_status)
        page = self.paginate_queryset(qs)
        serialized = VisitQueueSerializer(page if page is not None else qs, many=True).data
        return self.get_paginated_response(serialized) if page is not None else response.Response(serialized)

    @decorators.action(detail=True, methods=('post',), url_path='queue-status')
    def queue_transition(self, request, pk=None):
        serializer = QueueTransitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        visit = self.get_object()
        old_status = visit.queue_entry.status
        try:
            entry = visit.queue_entry.transition(serializer.validated_data['status'])
        except DjangoValidationError as exc:
            raise as_api_error(exc)
        self._audit('update', visit, {
            'queue_status': {'from': old_status, 'to': entry.status}, 'token_number': entry.token_number,
        })
        return response.Response(VisitQueueSerializer(entry).data)
