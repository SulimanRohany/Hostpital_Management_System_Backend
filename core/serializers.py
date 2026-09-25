from rest_framework import serializers

from .models import AuditLog, HospitalSettings


class HospitalSettingsSerializer(serializers.ModelSerializer):
    logo_url = serializers.SerializerMethodField()
    has_logo = serializers.SerializerMethodField()
    remove_logo = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = HospitalSettings
        fields = ('hospital_name', 'logo', 'logo_url', 'has_logo', 'remove_logo', 'updated_at')
        read_only_fields = ('logo_url', 'has_logo', 'updated_at')
        extra_kwargs = {'logo': {'write_only': True, 'required': False, 'allow_null': True}}

    def get_logo_url(self, obj):
        if not obj.logo:
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(obj.logo.url) if request else obj.logo.url

    def get_has_logo(self, obj):
        return bool(obj.logo)

    def validate_hospital_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Enter the hospital name.')
        return value

    def validate_logo(self, value):
        if value and value.size > 2 * 1024 * 1024:
            raise serializers.ValidationError('The logo must be 2 MB or smaller.')
        return value

    def update(self, instance, validated_data):
        remove_logo = validated_data.pop('remove_logo', False)
        old_logo = instance.logo.name if instance.logo else None
        replacing_logo = 'logo' in validated_data or remove_logo
        if remove_logo:
            validated_data['logo'] = None
        instance = super().update(instance, validated_data)
        if replacing_logo and old_logo and old_logo != (instance.logo.name if instance.logo else None):
            instance.logo.storage.delete(old_logo)
        return instance


class AuditLogSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()
    actor_username = serializers.SerializerMethodField()
    content_type = serializers.CharField(source='content_type.model', read_only=True)

    class Meta:
        model = AuditLog
        fields = '__all__'
        read_only_fields = tuple(field.name for field in AuditLog._meta.fields)

    def get_actor_name(self, obj):
        if not obj.actor:
            return None
        return obj.actor.get_full_name().strip() or obj.actor.username

    def get_actor_username(self, obj):
        return obj.actor.username if obj.actor else None
