from django.contrib.auth.password_validation import (
    CommonPasswordValidator,
    UserAttributeSimilarityValidator,
    get_default_password_validators,
    validate_password,
)
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken, TokenError

from core.mixins import client_ip
from core.models import AuditLog
from .models import User


class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False)
    department_name = serializers.CharField(source='department.name', read_only=True)

    class Meta:
        model = User
        fields = (
            'id', 'username', 'password', 'first_name', 'last_name', 'email', 'phone',
            'role', 'roles', 'department', 'department_name', 'is_active', 'must_change_password',
            'last_login', 'date_joined',
        )
        read_only_fields = ('id', 'is_active', 'must_change_password', 'last_login', 'date_joined')

    def validate_roles(self, value):
        if not value:
            raise serializers.ValidationError('Assign at least one role.')
        valid = {choice for choice, _label in User.Role.choices}
        unique = list(dict.fromkeys(value))
        invalid = [role for role in unique if role not in valid]
        if invalid:
            raise serializers.ValidationError(f"Unknown roles: {', '.join(invalid)}.")
        return unique

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if 'roles' not in attrs and 'role' in attrs:
            attrs['roles'] = [attrs['role']]
        if 'roles' in attrs:
            if self.instance and self.instance.is_administrator:
                submitted = set(attrs['roles'])
                current = set(self.instance.assigned_roles)
                if submitted != current:
                    raise serializers.ValidationError({
                        'roles': 'System administrator section access is fixed and cannot be changed.'
                    })
            attrs['role'] = (
                User.Role.ADMINISTRATOR
                if User.Role.ADMINISTRATOR in attrs['roles']
                else attrs['roles'][0]
            )
        return attrs

    def validate_password(self, value):
        user = self.instance
        if user:
            raise serializers.ValidationError('Use the reset-password action to change a user password.')
        validate_password(value, user=user)
        return value

    def create(self, validated_data):
        password = validated_data.pop('password', None)
        if not password:
            raise serializers.ValidationError({'password': 'A password is required.'})
        try:
            return User.objects.create_user(password=password, **validated_data)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc

    @transaction.atomic
    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        try:
            instance = super().update(instance, validated_data)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        if password:
            try:
                instance.set_temporary_password(password)
            except DjangoValidationError as exc:
                raise serializers.ValidationError(exc.message_dict) from exc
        return instance


class MeSerializer(UserSerializer):
    class Meta(UserSerializer.Meta):
        read_only_fields = UserSerializer.Meta.fields


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)

    def validate_current_password(self, value):
        if not self.context['request'].user.check_password(value):
            raise serializers.ValidationError('Current password is incorrect.')
        return value

    def validate_new_password(self, value):
        user = self.context['request'].user
        if user.check_password(value):
            raise serializers.ValidationError('The new password must be different from the current password.')
        password_validators = (
            validator
            for validator in get_default_password_validators()
            if not isinstance(validator, (CommonPasswordValidator, UserAttributeSimilarityValidator))
        )
        validate_password(value, user=user, password_validators=password_validators)
        return value

    @transaction.atomic
    def save(self):
        user = self.context['request'].user
        try:
            user.change_password(self.validated_data['new_password'])
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        return user


class PasswordResetSerializer(serializers.Serializer):
    new_password = serializers.CharField(write_only=True)

    def validate_new_password(self, value):
        user = self.context['user']
        if user.check_password(value):
            raise serializers.ValidationError('The temporary password must differ from the current password.')
        validate_password(value, user=user)
        return value

    def save(self):
        user = self.context['user']
        try:
            user.set_temporary_password(self.validated_data['new_password'])
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        return user


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField(write_only=True)

    def save(self):
        try:
            RefreshToken(self.validated_data['refresh']).blacklist()
        except TokenError as exc:
            raise serializers.ValidationError({'refresh': 'The refresh token is invalid or expired.'}) from exc


class HospitalTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token['role'] = user.role
        token['roles'] = user.assigned_roles
        token['name'] = user.get_full_name()
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        data['user'] = MeSerializer(self.user).data
        request = self.context.get('request')
        AuditLog.objects.create(
            actor=self.user,
            action=AuditLog.Action.LOGIN,
            object_repr=str(self.user)[:255],
            ip_address=client_ip(request) if request else None,
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:1000] if request else '',
        )
        return data
