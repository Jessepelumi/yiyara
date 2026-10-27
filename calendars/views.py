from django.db import transaction
from rest_framework import permissions
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import services
from .models import CalendarConnection
from .serializers import AvailabilitySerializer, PreferencesSerializer


class CalendarView(APIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "calendar"

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        response["Referrer-Policy"] = "no-referrer"
        return response


class ConnectView(CalendarView):
    def post(self, request):
        return Response({"authorization_url": services.start(request.user), "expires_in": 600})


class CallbackView(CalendarView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get(self, request):
        for key in ("state", "code", "error"):
            if len(request.query_params.getlist(key)) > 1:
                raise ValidationError("Duplicate authorization parameters.")
        services.finish(request.query_params)
        return Response({"connected": True, "detail": "Calendar connected. Return to Postman."})


class ConnectionView(CalendarView):
    def get(self, request):
        connection = CalendarConnection.objects.filter(user=request.user).first()
        if not connection:
            return Response({"connected": False, "reconnect_required": False})
        return Response({
            "connected": not connection.reconnect_required,
            "reconnect_required": connection.reconnect_required,
            "email": connection.email, "scopes": connection.scopes,
            "source_calendars": connection.source_calendars,
            "destination_calendar": connection.destination_calendar,
            "updated_at": connection.updated_at,
        })

    def delete(self, request):
        services.disconnect(request.user)
        return Response(status=204)


class CalendarsView(CalendarView):
    def get(self, request):
        return Response({"calendars": services.list_calendars(request.user)})


class PreferencesView(CalendarView):
    def put(self, request):
        serializer = PreferencesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        calendars = {item["id"]: item for item in services.list_calendars(request.user)}
        if any(c not in calendars or calendars[c].get("accessRole") not in ("freeBusyReader", "reader", "writer", "owner")
               for c in data["source_calendars"]):
            raise ValidationError("Select accessible source calendars from the calendar list.")
        if calendars.get(data["destination_calendar"], {}).get("accessRole") != "owner":
            raise ValidationError("The destination must be a calendar you own.")
        with transaction.atomic():
            services.lock_user(request.user)
            connection = services.get_connection(request.user)
            connection.source_calendars = data["source_calendars"]
            connection.destination_calendar = data["destination_calendar"]
            connection.save(update_fields=["source_calendars", "destination_calendar", "updated_at"])
        return Response(data)


class AvailabilityView(CalendarView):
    def post(self, request):
        serializer = AvailabilitySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        connection = services.get_connection(request.user)
        if not connection.source_calendars:
            raise ValidationError("Select availability calendars first.")
        data = serializer.validated_data
        payload = services.api(request.user, "POST", "freeBusy", json={
            "timeMin": data["time_min"].isoformat(), "timeMax": data["time_max"].isoformat(),
            "items": [{"id": c} for c in connection.source_calendars],
        })
        calendars = payload.get("calendars", {})
        if not isinstance(calendars, dict):
            calendars = {}
        failures = []
        for calendar in connection.source_calendars:
            result = calendars.get(calendar)
            if isinstance(result, dict) and not result.get("errors") and isinstance(result.get("busy"), list):
                continue
            errors = result.get("errors", []) if isinstance(result, dict) else []
            reasons = {
                error.get("reason") for error in errors if isinstance(error, dict)
                and isinstance(error.get("reason"), str)
            } if isinstance(errors, list) else set()
            selection_error = bool(reasons) and reasons.issubset({"notFound", "forbidden"})
            failures.append({
                "calendar_id": calendar,
                "code": "calendar_unavailable" if selection_error else "lookup_failed",
                "detail": (
                    "This calendar is inaccessible or does not support availability lookup. "
                    "Public holiday calendars may not support it."
                    if selection_error else
                    "Google did not return complete availability for this calendar. Retry later."
                ),
            })
        if failures:
            selection_only = all(error["code"] == "calendar_unavailable" for error in failures)
            return Response({
                "code": "invalid_availability_calendars" if selection_only else "availability_incomplete",
                "detail": (
                    "Update source_calendars using PUT /api/calendar/preferences/. "
                    "Choose your primary calendar or another calendar that supports availability. "
                    "Use GET /api/calendar/calendars/ to find its ID."
                    if selection_only else
                    "Availability could not be confirmed for every selected calendar. "
                    "Retry later and review any unavailable calendars listed below."
                ),
                "calendar_errors": failures,
            }, status=400 if selection_only else 502)
        return Response({
            "time_min": data["time_min"], "time_max": data["time_max"],
            "calendars": {c: {"busy": calendars[c]["busy"]} for c in connection.source_calendars},
        })
