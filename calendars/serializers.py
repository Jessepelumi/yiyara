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


class LocalMinuteField(serializers.TimeField):
    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if value.tzinfo is not None or value.second or value.microsecond:
            raise serializers.ValidationError("Use a local time with minute precision, e.g. 09:00.")
        return value


class WorkingHoursSerializer(serializers.Serializer):
    weekday = serializers.IntegerField(min_value=0, max_value=6)
    start_time = LocalMinuteField()
    end_time = LocalMinuteField()

    def validate(self, attrs):
        if attrs["start_time"] >= attrs["end_time"]:
            raise serializers.ValidationError("End time must follow start time on the same day.")
        return attrs


class SchedulingPreferencesSerializer(serializers.Serializer):
    timezone = serializers.CharField(max_length=100)
    working_hours = WorkingHoursSerializer(many=True, allow_empty=True, max_length=50)
    preferred_session_minutes = serializers.IntegerField(min_value=5, max_value=480, default=60)

    def validate_timezone(self, value):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise serializers.ValidationError("Use an IANA timezone such as Europe/London.") from None
        return value

    def validate_working_hours(self, value):
        for weekday in range(7):
            windows = sorted((w for w in value if w["weekday"] == weekday), key=lambda w: w["start_time"])
            for before, after in zip(windows, windows[1:]):
                if before["end_time"] > after["start_time"]:
                    raise serializers.ValidationError("Working-hour windows must not overlap.")
        return [{**w, "start_time": w["start_time"].isoformat(timespec="minutes"),
                 "end_time": w["end_time"].isoformat(timespec="minutes")} for w in value]


class ProtectedPeriodSerializer(serializers.ModelSerializer):
    weekdays = serializers.ListField(
        child=serializers.IntegerField(min_value=0, max_value=6), required=False, max_length=7
    )
    start_time = LocalMinuteField(required=False, allow_null=True)
    end_time = LocalMinuteField(required=False, allow_null=True)

    class Meta:
        from .models import ProtectedPeriod

        model = ProtectedPeriod
        fields = ["id", "title", "kind", "starts_at", "ends_at", "weekdays", "all_day",
                  "start_time", "end_time", "start_date", "end_date", "updated_at"]
        read_only_fields = ["id", "updated_at"]
        # Cross-field checks below provide actionable errors and support PATCH.
        validators = []

    def validate(self, attrs):
        fields = [f for f in self.Meta.fields if f not in self.Meta.read_only_fields]
        data = {f: getattr(self.instance, f) for f in fields} if self.instance else {
            "weekdays": [], "all_day": False,
        }
        data.update(attrs)
        kind = data.get("kind")
        if kind == "one_off":
            for field in ("starts_at", "ends_at"):
                if field in self.initial_data:
                    value = self.initial_data[field]
                    if not isinstance(value, str) or not (value.endswith("Z") or "+" in value[10:] or "-" in value[10:]):
                        raise serializers.ValidationError({field: "Include a timezone offset or Z."})
            if not data.get("starts_at") or not data.get("ends_at") or data["ends_at"] <= data["starts_at"]:
                raise serializers.ValidationError("A one-off period requires starts_at before ends_at.")
            if (data.get("weekdays") or data.get("all_day") or any(data.get(f) is not None
                    for f in ("start_time", "end_time", "start_date", "end_date"))):
                raise serializers.ValidationError("One-off periods cannot contain weekly recurrence fields.")
        elif kind == "weekly":
            if data.get("starts_at") or data.get("ends_at"):
                raise serializers.ValidationError("Weekly periods cannot contain starts_at or ends_at.")
            days = data.get("weekdays", [])
            if not days or len(days) != len(set(days)):
                raise serializers.ValidationError("Select unique weekdays (0=Monday through 6=Sunday).")
            if data.get("all_day"):
                if data.get("start_time") or data.get("end_time"):
                    raise serializers.ValidationError("All-day periods must omit start_time and end_time.")
            elif not data.get("start_time") or not data.get("end_time") or data["end_time"] <= data["start_time"]:
                raise serializers.ValidationError("Weekly times must run forward within one day; split overnight periods.")
            if data.get("start_date") and data.get("end_date") and data["end_date"] < data["start_date"]:
                raise serializers.ValidationError("end_date must not precede start_date.")
        return attrs
