from django.urls import path

from .views import AvailabilityView, CallbackView, CalendarsView, ConnectionView, ConnectView, PreferencesView
from .views import (
    ProtectedPeriodDetailView, ProtectedPeriodsView,
    SchedulingPreferencesView, SchedulingWindowsView,
)

urlpatterns = [
    path("google/connect/", ConnectView.as_view(), name="calendar-connect"),
    path("google/callback/", CallbackView.as_view(), name="calendar-callback"),
    path("connection/", ConnectionView.as_view(), name="calendar-connection"),
    path("calendars/", CalendarsView.as_view(), name="calendar-list"),
    path("preferences/", PreferencesView.as_view(), name="calendar-preferences"),
    path("availability/", AvailabilityView.as_view(), name="calendar-availability"),
    path("scheduling-preferences/", SchedulingPreferencesView.as_view(), name="scheduling-preferences"),
    path("protected-periods/", ProtectedPeriodsView.as_view(), name="protected-periods"),
    path("protected-periods/<int:period_id>/", ProtectedPeriodDetailView.as_view(), name="protected-period"),
    path("scheduling-windows/", SchedulingWindowsView.as_view(), name="scheduling-windows"),
]
