from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db import models
from rest_framework import decorators, response, status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.views import TokenObtainPairView

from core.mixins import AuditModelViewSetMixin
from core.models import AuditLog
from core.permissions import IsAdministrator, RolePermission
from .models import User
from .serializers import (
    HospitalTokenObtainPairSerializer, LogoutSerializer, MeSerializer, PasswordChangeSerializer,
    PasswordResetSerializer, UserSerializer,
)


class HospitalTokenObtainPairView(TokenObtainPairView):
    serializer_class = HospitalTokenObtainPairSerializer
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = 'login'
    allow_forced_password_change = True


class UserViewSet(AuditModelViewSetMixin, viewsets.ModelViewSet):
    queryset = User.objects.select_related('department').all()
    serializer_class = UserSerializer
    permission_classes = (RolePermission,)
    read_roles = (
        User.Role.ADMINISTRATOR,
        User.Role.RECEPTION,
        User.Role.MANAGER,
        User.Role.CLINICIAN,
    )
    write_roles = (User.Role.ADMINISTRATOR,)
    search_fields = ('username', 'first_name', 'last_name', 'email', 'phone')
    ordering_fields = ('username', 'date_joined', 'last_login', 'role')

    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.query_params
        if params.get('role'):
            # `role` remains the indexed primary role; JSON matching includes secondary sections.
            queryset = queryset.filter(
                models.Q(role=params['role']) | models.Q(roles__icontains=f'"{params["role"]}"')
            ).distinct()
        if params.get('department'):
            queryset = queryset.filter(department_id=params['department'])
        for field in ('is_active', 'must_change_password'):
            value = params.get(field)
            if value is not None:
                normalized = value.lower()
                if normalized not in ('true', 'false'):
                    raise ValidationError({field: 'Use true or false.'})
                queryset = queryset.filter(**{field: normalized == 'true'})
        return queryset

    @staticmethod
    def _lock_administrators():
        list(User.objects.select_for_update().filter(is_active=True).filter(
            models.Q(is_superuser=True) | models.Q(role=User.Role.ADMINISTRATOR)
        ).values_list('pk', flat=True))

    @transaction.atomic
    def perform_update(self, serializer):
        self._lock_administrators()
        super().perform_update(serializer)

    @transaction.atomic
    def perform_destroy(self, instance):
        self._lock_administrators()
        try:
            instance.deactivate(by=self.request.user)
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict) from exc
        self._audit('delete', instance, {'is_active': {'from': 'True', 'to': 'False'}})

    @decorators.action(detail=False, methods=('get',), permission_classes=(IsAuthenticated,))
    def me(self, request):
        return response.Response(MeSerializer(request.user).data)

    @decorators.action(detail=False, methods=('post',), url_path='change-password', permission_classes=(IsAuthenticated,))
    def change_password(self, request):
        serializer = PasswordChangeSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        self._audit(AuditLog.Action.UPDATE, request.user, {'password': {'changed': True}})
        return response.Response(status=status.HTTP_204_NO_CONTENT)

    @decorators.action(detail=False, methods=('post',), permission_classes=(IsAuthenticated,))
    def logout(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        self._audit(AuditLog.Action.LOGOUT, request.user)
        return response.Response(status=status.HTTP_204_NO_CONTENT)

    @decorators.action(detail=True, methods=('post',), url_path='reset-password')
    @transaction.atomic
    def reset_password(self, request, pk=None):
        user = self.get_object()
        serializer = PasswordResetSerializer(data=request.data, context={'user': user})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        self._audit(AuditLog.Action.UPDATE, user, {'password': {'reset': True}, 'must_change_password': {'to': 'True'}})
        return response.Response(status=status.HTTP_204_NO_CONTENT)

    @decorators.action(detail=True, methods=('post',))
    @transaction.atomic
    def activate(self, request, pk=None):
        user = self.get_object()
        if not user.is_active:
            try:
                user.activate()
            except DjangoValidationError as exc:
                raise ValidationError(exc.message_dict) from exc
            self._audit(AuditLog.Action.UPDATE, user, {'is_active': {'from': 'False', 'to': 'True'}})
        return response.Response(UserSerializer(user).data)

    @decorators.action(detail=True, methods=('post',))
    @transaction.atomic
    def deactivate(self, request, pk=None):
        self._lock_administrators()
        user = self.get_object()
        if user.is_active:
            try:
                user.deactivate(by=request.user)
            except DjangoValidationError as exc:
                raise ValidationError(exc.message_dict) from exc
            self._audit(AuditLog.Action.UPDATE, user, {'is_active': {'from': 'True', 'to': 'False'}})
        return response.Response(UserSerializer(user).data)
