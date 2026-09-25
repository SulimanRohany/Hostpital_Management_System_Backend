from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from rest_framework import decorators, response, viewsets
from rest_framework.exceptions import ValidationError

from accounts.models import User
from core.mixins import AuditModelViewSetMixin
from core.permissions import RolePermission
from hr.models import Employee, EmploymentAssignment
from reception.models import Visit, VisitQueueEntry
from .models import Department, Service
from .serializers import (
    DepartmentFilterSerializer, DepartmentLifecycleSerializer, DepartmentSerializer,
    DepartmentWriteSerializer, ServiceFeeHistorySerializer, ServiceFilterSerializer, ServiceSerializer,
    ServiceWriteSerializer,
)


DEPARTMENT_READ_ROLES = (
    User.Role.ADMINISTRATOR, User.Role.RECEPTION, User.Role.LABORATORY,
    User.Role.PHARMACY, User.Role.FINANCE, User.Role.MANAGER, User.Role.HR,
    User.Role.CLINICIAN,
)


def as_api_error(exc):
    return ValidationError(
        getattr(exc, 'message_dict', None) or getattr(exc, 'messages', None) or str(exc)
    )


def validated_query_params(request, serializer_class):
    keys = set(serializer_class().fields)
    unknown = set(request.query_params) - keys - {'search', 'ordering', 'page', 'page_size', 'format'}
    if unknown:
        raise ValidationError({key: 'Unknown query parameter.' for key in sorted(unknown)})
    serializer = serializer_class(data={key: value for key, value in request.query_params.items() if key in keys})
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


class DepartmentViewSet(AuditModelViewSetMixin, viewsets.ModelViewSet):
    queryset = Department.objects.all()
    serializer_class = DepartmentSerializer
    permission_classes = (RolePermission,)
    read_roles = DEPARTMENT_READ_ROLES
    write_roles = (User.Role.ADMINISTRATOR,)
    action_roles = {
        'activate': (User.Role.ADMINISTRATOR,),
        'deactivate': (User.Role.ADMINISTRATOR,),
    }
    search_fields = ('code', 'name')
    ordering_fields = ('code', 'name', 'created_at', 'updated_at')
    http_method_names = ('get', 'post', 'patch', 'head', 'options')

    def get_queryset(self):
        active_services = Service.objects.filter(department_id=OuterRef('pk'), is_active=True)
        active_users = User.objects.filter(department_id=OuterRef('pk'), is_active=True)
        queryset = Department.objects.annotate(
            _has_active_services=Exists(active_services),
            _has_active_users=Exists(active_users),
            _has_current_employees=Exists(
                Employee.objects.filter(
                    department_id=OuterRef('pk'), status__in=(
                        Employee.Status.ACTIVE, Employee.Status.LEAVE, Employee.Status.SUSPENDED,
                    )
                )
            ),
            _has_current_assignments=Exists(
                EmploymentAssignment.objects.filter(department_id=OuterRef('pk'), end_date__isnull=True)
            ),
            _has_open_visits=Exists(
                Visit.objects.filter(
                    department_id=OuterRef('pk'), status__in=(Visit.Status.REGISTERED, Visit.Status.IN_PROGRESS)
                )
            ),
            _has_active_queue_entries=Exists(
                VisitQueueEntry.objects.filter(
                    department_id=OuterRef('pk'), status__in=(
                        VisitQueueEntry.Status.WAITING, VisitQueueEntry.Status.CALLED,
                        VisitQueueEntry.Status.SERVING,
                    )
                )
            ),
            service_count=Count('services', distinct=True),
            active_service_count=Count('services', filter=Q(services__is_active=True), distinct=True),
        ).order_by('name')
        if getattr(self, 'action', None) == 'partial_update':
            # ``lab_test`` is an optional reverse one-to-one relation, so its
            # select_related() join is nullable. PostgreSQL cannot combine
            # that outer join with FOR UPDATE. The write serializer only
            # needs the non-null department relation while locking the row.
            queryset = queryset.select_related(None).select_related('department').select_for_update()
        if getattr(self, 'action', None) == 'list':
            filters = validated_query_params(self.request, DepartmentFilterSerializer)
            if 'is_active' in filters:
                queryset = queryset.filter(is_active=filters['is_active'])
            if 'is_clinical' in filters:
                queryset = queryset.filter(is_clinical=filters['is_clinical'])
        return queryset

    def get_serializer_class(self):
        if self.action in ('create', 'partial_update'):
            return DepartmentWriteSerializer
        return DepartmentSerializer

    def create(self, request, *args, **kwargs):
        result = super().create(request, *args, **kwargs)
        result.data = DepartmentSerializer(
            self.get_queryset().get(pk=result.data['id']), context=self.get_serializer_context()
        ).data
        return result

    @transaction.atomic
    def partial_update(self, request, *args, **kwargs):
        result = super().partial_update(request, *args, **kwargs)
        result.data = DepartmentSerializer(self.get_object(), context=self.get_serializer_context()).data
        return result

    @decorators.action(detail=True, methods=('post',))
    @transaction.atomic
    def activate(self, request, pk=None):
        department = self.get_object()
        old_status = department.is_active
        try:
            department.activate()
        except DjangoValidationError as exc:
            raise as_api_error(exc) from exc
        if old_status != department.is_active:
            self._audit('update', department, {'is_active': {'from': old_status, 'to': department.is_active}})
        department = self.get_queryset().get(pk=department.pk)
        return response.Response(DepartmentSerializer(department, context=self.get_serializer_context()).data)

    @decorators.action(detail=True, methods=('post',))
    @transaction.atomic
    def deactivate(self, request, pk=None):
        serializer = DepartmentLifecycleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        department = self.get_object()
        old_status = department.is_active
        active_service_ids = list(department.services.filter(is_active=True).values_list('id', flat=True))
        try:
            department = department.deactivate(
                deactivate_services=serializer.validated_data['deactivate_services']
            )
        except DjangoValidationError as exc:
            raise as_api_error(exc) from exc
        if old_status != department.is_active or active_service_ids:
            self._audit('update', department, {
                'is_active': {'from': old_status, 'to': department.is_active},
                'deactivated_service_ids': [str(value) for value in active_service_ids]
                if serializer.validated_data['deactivate_services'] else [],
            })
        department = self.get_queryset().get(pk=department.pk)
        return response.Response(DepartmentSerializer(department, context=self.get_serializer_context()).data)


class ServiceViewSet(AuditModelViewSetMixin, viewsets.ModelViewSet):
    queryset = Service.objects.select_related('department')
    serializer_class = ServiceSerializer
    permission_classes = (RolePermission,)
    read_roles = DEPARTMENT_READ_ROLES
    write_roles = (User.Role.ADMINISTRATOR,)
    action_roles = {
        'activate': (User.Role.ADMINISTRATOR, User.Role.MANAGER),
        'deactivate': (User.Role.ADMINISTRATOR, User.Role.MANAGER),
        'fee_history': DEPARTMENT_READ_ROLES,
    }
    search_fields = ('code', 'name', 'department__name')
    ordering_fields = ('code', 'name', 'standard_fee', 'created_at', 'updated_at')
    http_method_names = ('get', 'post', 'patch', 'head', 'options')

    def get_queryset(self):
        queryset = super().get_queryset()
        user = getattr(self.request, 'user', None)
        if (
            getattr(self, 'action', None) in ('activate', 'deactivate')
            and user and not user.is_superuser and user.has_role(User.Role.MANAGER)
        ):
            queryset = queryset.filter(department_id=user.department_id) if user.department_id else queryset.none()
        if getattr(self, 'action', None) == 'partial_update':
            # ``lab_test`` is nullable and cannot be part of a ``FOR UPDATE`` query in PostgreSQL.
            queryset = queryset.select_related(None).select_related('department').select_for_update()
        else:
            queryset = queryset.select_related('lab_test')
        if getattr(self, 'action', None) != 'list':
            return queryset
        filters = validated_query_params(self.request, ServiceFilterSerializer)
        if department := filters.get('department'):
            queryset = queryset.filter(department_id=department)
        for field in ('is_active', 'is_laboratory', 'is_discountable'):
            if field in filters:
                queryset = queryset.filter(**{field: filters[field]})
        if 'is_available' in filters:
            available = filters['is_available']
            condition = Q(is_active=True, department__is_active=True) & (
                Q(is_laboratory=False) | Q(is_laboratory=True, lab_test__is_active=True)
            )
            queryset = queryset.filter(condition if available else ~condition)
        return queryset

    def get_serializer_class(self):
        if self.action in ('create', 'partial_update'):
            return ServiceWriteSerializer
        return ServiceSerializer

    def create(self, request, *args, **kwargs):
        result = super().create(request, *args, **kwargs)
        result.data = ServiceSerializer(
            self.get_queryset().get(pk=result.data['id']), context=self.get_serializer_context()
        ).data
        return result

    @transaction.atomic
    def partial_update(self, request, *args, **kwargs):
        result = super().partial_update(request, *args, **kwargs)
        result.data = ServiceSerializer(self.get_object(), context=self.get_serializer_context()).data
        return result

    @decorators.action(detail=True, methods=('post',))
    @transaction.atomic
    def activate(self, request, pk=None):
        service = self.get_object()
        old_status = service.is_active
        try:
            service.activate()
        except DjangoValidationError as exc:
            raise as_api_error(exc) from exc
        if old_status != service.is_active:
            self._audit('update', service, {'is_active': {'from': old_status, 'to': service.is_active}})
        service = self.get_queryset().get(pk=service.pk)
        return response.Response(ServiceSerializer(service, context=self.get_serializer_context()).data)

    @decorators.action(detail=True, methods=('post',))
    @transaction.atomic
    def deactivate(self, request, pk=None):
        service = self.get_object()
        old_status = service.is_active
        try:
            service.deactivate()
        except DjangoValidationError as exc:
            raise as_api_error(exc) from exc
        if old_status != service.is_active:
            self._audit('update', service, {'is_active': {'from': old_status, 'to': service.is_active}})
        service = self.get_queryset().get(pk=service.pk)
        return response.Response(ServiceSerializer(service, context=self.get_serializer_context()).data)

    @decorators.action(detail=True, methods=('get',), url_path='fee-history')
    def fee_history(self, request, pk=None):
        service = self.get_object()
        queryset = service.fee_history.select_related('changed_by').all()
        page = self.paginate_queryset(queryset)
        serializer = ServiceFeeHistorySerializer(
            page if page is not None else queryset, many=True, context=self.get_serializer_context()
        )
        return self.get_paginated_response(serializer.data) if page is not None else response.Response(serializer.data)
