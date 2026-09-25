from rest_framework import serializers


class DateRangeSerializer(serializers.Serializer):
    start = serializers.DateField(required=False)
    end = serializers.DateField(required=False)

    def validate(self, attrs):
        if attrs.get('start') and attrs.get('end') and attrs['end'] < attrs['start']:
            raise serializers.ValidationError({'end': 'End date cannot be before start date.'})
        return attrs


class StockReportFilterSerializer(serializers.Serializer):
    expiry_days = serializers.IntegerField(required=False, default=90, min_value=0, max_value=3650)
    include_inactive = serializers.BooleanField(required=False, default=False)
