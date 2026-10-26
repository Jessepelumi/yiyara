from django.urls import path

from .views import AvailabilityView, CallbackView, CalendarsView, ConnectionView, ConnectView, PreferencesView

urlpatterns = [
    path("google/connect/", ConnectView.as_view(), name="calendar-connect"),
    path("google/callback/", CallbackView.as_view(), name="calendar-callback"),
    path("connection/", ConnectionView.as_view(), name="calendar-connection"),
    path("calendars/", CalendarsView.as_view(), name="calendar-list"),
    path("preferences/", PreferencesView.as_view(), name="calendar-preferences"),
    path("availability/", AvailabilityView.as_view(), name="calendar-availability"),
]
