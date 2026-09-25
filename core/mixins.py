from ipaddress import ip_address, ip_network

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from rest_framework import serializers

from .models import AuditLog


def client_ip(request):
    remote = request.META.get('REMOTE_ADDR')
    trusted_networks = getattr(settings, 'AUDIT_TRUSTED_PROXY_NETWORKS', ())

    def is_trusted(value):
        try:
            address = ip_address(value)
            return any(address in ip_network(network) for network in trusted_networks)
        except ValueError:
            return False

    if not remote or not is_trusted(remote):
        return remote
    forwarded = [item.strip() for item in request.META.get('HTTP_X_FORWARDED_FOR', '').split(',') if item.strip()]
    for candidate in reversed(forwarded):
        if not is_trusted(candidate):
            try:
                return str(ip_address(candidate))
            except ValueError:
                return remote
    return remote


class AuditModelViewSetMixin:
    def _audit(self, action, instance, changes=None):
        AuditLog.objects.create(
            actor=self.request.user,
            action=action,
            content_type=ContentType.objects.get_for_model(instance, for_concrete_model=False),
            object_id=str(instance.pk),
            object_repr=str(instance)[:255],
            changes=changes or {},
            ip_address=client_ip(self.request),
            user_agent=self.request.META.get('HTTP_USER_AGENT', '')[:1000],
        )

    def perform_create(self, serializer):
        instance = serializer.save()
        self._audit(AuditLog.Action.CREATE, instance)

    def perform_update(self, serializer):
        before = {field: getattr(serializer.instance, field, None) for field in serializer.validated_data}
        instance = serializer.save()
        changes = {
            field: {'from': str(before[field]), 'to': str(getattr(instance, field, None))}
            for field in before
            if before[field] != getattr(instance, field, None)
        }
        self._audit(AuditLog.Action.UPDATE, instance, changes)

    def perform_destroy(self, instance):
        representation = str(instance)
        object_id = str(instance.pk)
        content_type = ContentType.objects.get_for_model(instance, for_concrete_model=False)
        instance.delete()
        AuditLog.objects.create(
            actor=self.request.user,
            action=AuditLog.Action.DELETE,
            content_type=content_type,
            object_id=object_id,
            object_repr=representation[:255],
            ip_address=client_ip(self.request),
            user_agent=self.request.META.get('HTTP_USER_AGENT', '')[:1000],
        )


class ImmutableTransactionMixin:
    def update(self, instance, validated_data):
        raise serializers.ValidationError('Posted transactions are immutable; create a reversal or correction.')
