from datetime import timedelta

from rest_framework import serializers


class PreferencesSerializer(serializers.Serializer):
    source_calendars = serializers.ListField(
        child=serializers.CharField(max_length=1024), min_length=1, max_length=50
    )
    destination_calendar = serializers.CharField(max_length=1024)

    def validate_source_calendars(self, value):
        if len(set(value)) != len(value):
            raise serializers.ValidationError("Duplicate calendars are not allowed.")
        return value


class AvailabilitySerializer(serializers.Serializer):
    time_min = serializers.DateTimeField()
    time_max = serializers.DateTimeField()

    def to_internal_value(self, data):
        # Explicit offsets are required; never guess the user's timezone.
        for field in ("time_min", "time_max"):
            value = data.get(field, "")
            if not isinstance(value, str) or not (value.endswith("Z") or "+" in value[10:] or "-" in value[10:]):
                raise serializers.ValidationError({field: "Include a timezone offset or Z."})
        return super().to_internal_value(data)

    def validate(self, attrs):
        duration = attrs["time_max"] - attrs["time_min"]
        if duration <= timedelta(0) or duration > timedelta(days=31):
            raise serializers.ValidationError("Choose a positive range of at most 31 days.")
        return attrs
