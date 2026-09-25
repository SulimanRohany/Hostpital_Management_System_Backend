import mimetypes

from django.contrib.contenttypes.models import ContentType
from django.http import FileResponse, Http404
from rest_framework import mixins, parsers, response, status, viewsets
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from .mixins import client_ip
from .models import AuditLog, HospitalSettings
from .permissions import IsAdministrator
from .serializers import AuditLogSerializer, HospitalSettingsSerializer


class HospitalSettingsView(APIView):
    parser_classes = (parsers.MultiPartParser, parsers.FormParser, parsers.JSONParser)

    def get_permissions(self):
        return [AllowAny()] if self.request.method == 'GET' else [IsAdministrator()]

    def get(self, request):
        hospital_settings = HospitalSettings.load()
        return response.Response(HospitalSettingsSerializer(hospital_settings, context={'request': request}).data)

    def patch(self, request):
        hospital_settings = HospitalSettings.load()
        serializer = HospitalSettingsSerializer(
            hospital_settings, data=request.data, partial=True, context={'request': request}
        )
        serializer.is_valid(raise_exception=True)
        before = {'hospital_name': hospital_settings.hospital_name, 'logo': hospital_settings.logo.name if hospital_settings.logo else ''}
        hospital_settings = serializer.save()
        after = {'hospital_name': hospital_settings.hospital_name, 'logo': hospital_settings.logo.name if hospital_settings.logo else ''}
        changes = {
            key: {'from': before[key], 'to': after[key]}
            for key in before if before[key] != after[key]
        }
        AuditLog.objects.create(
            actor=request.user,
            action=AuditLog.Action.UPDATE,
            content_type=ContentType.objects.get_for_model(hospital_settings),
            object_id=str(hospital_settings.pk),
            object_repr=str(hospital_settings)[:255],
            changes=changes,
            ip_address=client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:1000],
        )
        return response.Response(HospitalSettingsSerializer(hospital_settings, context={'request': request}).data)


class HospitalLogoView(APIView):
    permission_classes = (AllowAny,)

    def get(self, request):
        hospital_settings = HospitalSettings.load()
        if not hospital_settings.logo:
            raise Http404
        content_type = mimetypes.guess_type(hospital_settings.logo.name)[0] or 'application/octet-stream'
        return FileResponse(hospital_settings.logo.open('rb'), content_type=content_type)


class AuditLogViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = AuditLog.objects.select_related('actor', 'content_type')
    serializer_class = AuditLogSerializer
    permission_classes = (IsAdministrator,)
    search_fields = ('object_repr', 'object_id', 'actor__username', 'actor__first_name', 'actor__last_name', 'ip_address')
    ordering_fields = ('created_at', 'action')

    def get_queryset(self):
        queryset = super().get_queryset()
        action = self.request.query_params.get('action')
        if action in AuditLog.Action.values:
            queryset = queryset.filter(action=action)
        return queryset
