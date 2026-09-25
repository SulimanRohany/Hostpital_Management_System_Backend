from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404
from rest_framework import decorators, response, status, viewsets
from rest_framework.exceptions import ValidationError

from accounts.models import User
from core.mixins import AuditModelViewSetMixin
from core.permissions import RolePermission
from .models import LabOrder, LabTest
from .serializers import (
    LabOrderCancellationSerializer, LabOrderCreateSerializer, LabOrderFilterSerializer,
    LabOrderSerializer, LabOrderUpdateSerializer, LabResultSerializer, LabTestSerializer,
)


LAB_READ_ROLES = (
    User.Role.ADMINISTRATOR, User.Role.LABORATORY, User.Role.MANAGER,
    User.Role.CLINICIAN, User.Role.RECEPTION,
)


def as_api_error(exc):
    return ValidationError(
        getattr(exc, 'message_dict', None) or getattr(exc, 'messages', None) or str(exc)
    )


class LabTestViewSet(AuditModelViewSetMixin, viewsets.ModelViewSet):
    queryset = LabTest.objects.select_related('service')
    serializer_class = LabTestSerializer
    permission_classes = (RolePermission,)
    read_roles = LAB_READ_ROLES
    write_roles = (User.Role.ADMINISTRATOR, User.Role.LABORATORY)
    search_fields = ('code', 'name', 'specimen_type')
    ordering_fields = ('code', 'name', 'created_at')

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError as exc:
            raise ValidationError({
                'detail': 'This laboratory test is already in use and cannot be deleted; deactivate it instead.'
            }) from exc
        except DjangoValidationError as exc:
            raise as_api_error(exc) from exc


class LabOrderViewSet(AuditModelViewSetMixin, viewsets.ModelViewSet):
    queryset = LabOrder.objects.select_related(
        'patient', 'visit', 'ordered_by', 'collected_by', 'cancelled_by',
    ).prefetch_related('items__test', 'items__resulted_by')
    serializer_class = LabOrderSerializer
    permission_classes = (RolePermission,)
    read_roles = LAB_READ_ROLES
    write_roles = (User.Role.ADMINISTRATOR, User.Role.LABORATORY)
    action_roles = {
        'create': (
            User.Role.ADMINISTRATOR, User.Role.LABORATORY, User.Role.CLINICIAN,
            User.Role.RECEPTION,
        ),
        'partial_update': (
            User.Role.ADMINISTRATOR, User.Role.LABORATORY, User.Role.CLINICIAN,
        ),
        'result': (User.Role.ADMINISTRATOR, User.Role.LABORATORY),
        'collect': (User.Role.ADMINISTRATOR, User.Role.LABORATORY),
        'cancel': (User.Role.ADMINISTRATOR, User.Role.LABORATORY, User.Role.MANAGER),
    }
    search_fields = ('order_number', 'patient__medical_record_number', 'patient__first_name', 'patient__last_name')
    ordering_fields = ('ordered_at', 'created_at', 'status')
    http_method_names = ('get', 'post', 'patch', 'head', 'options')

    def get_serializer_class(self):
        if self.action == 'create':
            return LabOrderCreateSerializer
        if self.action == 'partial_update':
            return LabOrderUpdateSerializer
        return LabOrderSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        detail = LabOrderSerializer(serializer.instance, context=self.get_serializer_context())
        return response.Response(
            detail.data, status=status.HTTP_201_CREATED,
            headers=self.get_success_headers(detail.data),
        )

    def partial_update(self, request, *args, **kwargs):
        result = super().partial_update(request, *args, **kwargs)
        order = self.get_object()
        result.data = LabOrderSerializer(order, context=self.get_serializer_context()).data
        return result

    def get_queryset(self):
        qs = super().get_queryset()
        if getattr(self, 'action', None) != 'list':
            return qs
        keys = set(LabOrderFilterSerializer().fields)
        raw = {key: value for key, value in self.request.query_params.items() if key in keys}
        filters = LabOrderFilterSerializer(data=raw)
        filters.is_valid(raise_exception=True)
        data = filters.validated_data
        if patient := data.get('patient'):
            qs = qs.filter(patient_id=patient)
        if order_status := data.get('status'):
            qs = qs.filter(status=order_status)
        if exact_date := data.get('date'):
            qs = qs.filter(ordered_at__date=exact_date)
        if date_from := data.get('date_from'):
            qs = qs.filter(ordered_at__date__gte=date_from)
        if date_to := data.get('date_to'):
            qs = qs.filter(ordered_at__date__lte=date_to)
        return qs

    @decorators.action(detail=True, methods=('patch',), url_path=r'results/(?P<item_id>[^/.]+)')
    def result(self, request, pk=None, item_id=None):
        order = self.get_object()
        item = get_object_or_404(order.items.all(), pk=item_id)
        old_result = {
            'result': item.result,
            'result_unit': item.result_unit,
            'reference_range': item.reference_range,
            'is_abnormal': item.is_abnormal,
        }
        serializer = LabResultSerializer(item, data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        correction_reason = serializer.validated_data.get('correction_reason', '')
        item = serializer.save()
        new_result = {
            'result': item.result,
            'result_unit': item.result_unit,
            'reference_range': item.reference_range,
            'is_abnormal': item.is_abnormal,
            'resulted_by': str(item.resulted_by_id),
            'resulted_at': item.resulted_at.isoformat(),
        }
        self._audit('update', item, {
            'result': {'from': old_result, 'to': new_result},
            'correction_reason': correction_reason,
        })
        order.refresh_from_db()
        return response.Response(LabOrderSerializer(order, context=self.get_serializer_context()).data)

    @decorators.action(detail=True, methods=('post',))
    def collect(self, request, pk=None):
        order = self.get_object()
        old_status = order.status
        try:
            order = order.collect(user=request.user)
        except DjangoValidationError as exc:
            raise as_api_error(exc) from exc
        self._audit('update', order, {
            'status': {'from': old_status, 'to': order.status},
            'collected_by': str(request.user.pk), 'collected_at': order.collected_at.isoformat(),
        })
        return response.Response(LabOrderSerializer(order, context=self.get_serializer_context()).data)

    @decorators.action(detail=True, methods=('post',))
    def cancel(self, request, pk=None):
        serializer = LabOrderCancellationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = self.get_object()
        old_status = order.status
        reason = serializer.validated_data['reason']
        try:
            order = order.cancel(user=request.user, reason=reason)
        except DjangoValidationError as exc:
            raise as_api_error(exc) from exc
        self._audit('update', order, {
            'status': {'from': old_status, 'to': order.status},
            'reason': order.cancellation_reason, 'cancelled_by': str(request.user.pk),
            'cancelled_at': order.cancelled_at.isoformat(),
        })
        return response.Response(LabOrderSerializer(order, context=self.get_serializer_context()).data)
