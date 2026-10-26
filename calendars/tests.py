from datetime import timedelta
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from . import services
from .models import CalendarAuthorization, CalendarConnection


@override_settings(
    GOOGLE_CALENDAR_CLIENT_ID="calendar-client",
    GOOGLE_CALENDAR_CLIENT_SECRET="test-secret",
    GOOGLE_CALENDAR_REDIRECT_URI="http://localhost:8000/api/calendar/google/callback/",
    GOOGLE_CALENDAR_ENCRYPTION_KEY=Fernet.generate_key().decode(),
)
class CalendarTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = get_user_model().objects.create_user(email="owner@gmail.com", google_sub="subject")
        self.client.force_authenticate(self.user)

    def connect(self):
        response = self.client.post("/api/calendar/google/connect/")
        self.assertEqual(response.status_code, 200)
        return {k: v[0] for k, v in parse_qs(urlparse(response.data["authorization_url"]).query).items()}

    def connection(self, **kwargs):
        defaults = {
            "google_sub": "subject", "email": self.user.email,
            "credentials": services.encrypt({"access_token": "access-secret", "refresh_token": "refresh-secret"}),
            "expires_at": timezone.now() + timedelta(hours=1), "scopes": services.CALENDAR_SCOPES,
            "source_calendars": ["owned"], "destination_calendar": "owned",
        }
        defaults.update(kwargs)
        return CalendarConnection.objects.create(user=self.user, **defaults)

    def callback(self, query, **changes):
        tokens = {
            "access_token": "access-secret", "refresh_token": "refresh-secret",
            "expires_in": 3600, "id_token": "signed-id", "scope": " ".join(services.SCOPES),
        }
        tokens.update(changes.pop("tokens", {}))
        claims = {"sub": "subject", "email_verified": True, "nonce": query["nonce"]}
        claims.update(changes)
        with patch("calendars.services.token_request", return_value=(200, tokens)), patch(
            "calendars.services.verify_oauth2_token", return_value=claims
        ):
            self.client.force_authenticate(None)
            return self.client.get("/api/calendar/google/callback/", {"state": query["state"], "code": "code"})

    def test_connect_requires_authentication(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post("/api/calendar/google/connect/").status_code, 401)

    def test_connect_pkce_and_encrypted_attempt(self):
        query = self.connect()
        self.assertEqual(query["access_type"], "offline")
        self.assertEqual(query["code_challenge_method"], "S256")
        attempt = CalendarAuthorization.objects.get()
        self.assertNotIn(query["nonce"], attempt.verifier)
        self.assertNotEqual(attempt.state_hash, query["state"])

    def test_callback_persists_encrypted_tokens_and_rejects_replay(self):
        query = self.connect()
        response = self.callback(query)
        self.assertEqual(response.status_code, 200)
        connection = CalendarConnection.objects.get()
        self.assertNotIn("access-secret", connection.credentials)
        self.assertNotIn("refresh-secret", connection.credentials)
        self.assertEqual(services.decrypt(connection.credentials)["refresh_token"], "refresh-secret")
        self.assertEqual(self.callback(query).status_code, 400)

    def test_rejects_wrong_account_and_nonce(self):
        for changes in ({"sub": "other"}, {"nonce": "other"}, {"email_verified": False}):
            self.client.force_authenticate(self.user)
            query = self.connect()
            self.assertEqual(self.callback(query, **changes).status_code, 400)
            self.assertFalse(CalendarConnection.objects.exists())

    def test_missing_scopes_and_refresh_token(self):
        for tokens in ({"scope": "openid email"}, {"refresh_token": None}):
            self.client.force_authenticate(self.user)
            query = self.connect()
            self.assertEqual(self.callback(query, tokens=tokens).status_code, 400)
            self.assertFalse(CalendarConnection.objects.exists())

    def test_reconnect_preserves_refresh_and_preferences(self):
        self.connection()
        query = self.connect()
        self.assertEqual(self.callback(query, tokens={"refresh_token": None}).status_code, 200)
        connection = CalendarConnection.objects.get()
        self.assertEqual(connection.source_calendars, ["owned"])
        self.assertEqual(services.decrypt(connection.credentials)["refresh_token"], "refresh-secret")

    def test_expired_state_and_denial(self):
        query = self.connect()
        CalendarAuthorization.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.callback(query).status_code, 400)
        self.client.force_authenticate(self.user)
        query = self.connect()
        response = self.client.get("/api/calendar/google/callback/", {"state": query["state"], "error": "access_denied"})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(CalendarAuthorization.objects.get().used)

    def test_disconnect_cancels_pending_consent(self):
        self.connection()
        query = self.connect()
        self.assertEqual(self.client.delete("/api/calendar/connection/").status_code, 204)
        self.assertFalse(CalendarConnection.objects.exists())
        self.assertEqual(self.callback(query).status_code, 400)

    def test_status_never_exposes_tokens(self):
        self.connection()
        response = self.client.get("/api/calendar/connection/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("credentials", response.data)
        self.assertNotIn("access-secret", str(response.data))
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_user_isolation(self):
        self.connection()
        other = get_user_model().objects.create_user(email="other@gmail.com", google_sub="other")
        self.client.force_authenticate(other)
        self.assertFalse(self.client.get("/api/calendar/connection/").data["connected"])
        self.client.delete("/api/calendar/connection/")
        self.assertEqual(CalendarConnection.objects.count(), 1)

    def test_refresh_renewal_and_revocation(self):
        connection = self.connection(expires_at=timezone.now())
        with patch("calendars.services.token_request", return_value=(200, {"access_token": "renewed", "expires_in": 3600})):
            self.assertEqual(services.access_token(self.user), "renewed")
        connection.refresh_from_db()
        self.assertEqual(services.decrypt(connection.credentials)["refresh_token"], "refresh-secret")
        connection.expires_at = timezone.now()
        connection.save()
        with patch("calendars.services.token_request", return_value=(400, {"error": "invalid_grant"})):
            with self.assertRaises(services.ReconnectRequired):
                services.access_token(self.user)
        connection.refresh_from_db()
        self.assertTrue(connection.reconnect_required)

    def test_preferences_require_accessible_sources_and_owned_destination(self):
        self.connection()
        with patch("calendars.services.list_calendars", return_value=[
            {"id": "owned", "accessRole": "owner"}, {"id": "shared", "accessRole": "writer"}
        ]):
            for sources, destination, expected in ((["unknown"], "owned", 400), (["shared"], "shared", 400), (["shared"], "owned", 200)):
                response = self.client.put("/api/calendar/preferences/", {
                    "source_calendars": sources, "destination_calendar": destination
                }, format="json")
                self.assertEqual(response.status_code, expected)

    def test_availability_and_partial_failure(self):
        self.connection()
        query = {"time_min": "2026-11-01T00:00:00Z", "time_max": "2026-11-02T00:00:00Z"}
        with patch("calendars.services.api", return_value={"calendars": {"owned": {"busy": []}}}):
            self.assertEqual(self.client.post("/api/calendar/availability/", query, format="json").status_code, 200)
        with patch("calendars.services.api", return_value={"calendars": {"owned": {"errors": [{"reason": "notFound"}]}}}):
            self.assertEqual(self.client.post("/api/calendar/availability/", query, format="json").status_code, 502)

    def test_availability_rejects_unbounded_and_naive_ranges(self):
        self.connection()
        for start, end in (("2026-11-01T00:00:00", "2026-11-02T00:00:00Z"),
                           ("2026-11-02T00:00:00Z", "2026-11-01T00:00:00Z"),
                           ("2026-11-01T00:00:00Z", "2027-01-01T00:00:00Z")):
            response = self.client.post("/api/calendar/availability/", {"time_min": start, "time_max": end}, format="json")
            self.assertEqual(response.status_code, 400)

    def test_list_paginates(self):
        with patch("calendars.services.api", side_effect=[
            {"items": [{"id": "one"}], "nextPageToken": "next"}, {"items": [{"id": "two"}]}
        ]):
            self.assertEqual([c["id"] for c in services.list_calendars(self.user)], ["one", "two"])

    def test_provider_timeout_is_sanitized(self):
        import httpx

        with patch("calendars.services.httpx.request", side_effect=httpx.ReadTimeout("secret-token")):
            with self.assertRaises(services.CalendarError) as caught:
                services.request_json("GET", "https://example.com")
        self.assertNotIn("secret-token", str(caught.exception))

    def test_unauthorized_calendar_marks_reconnect_required(self):
        connection = self.connection()
        with patch("calendars.services.request_json", return_value=(401, {})):
            with self.assertRaises(services.ReconnectRequired):
                services.api(self.user, "GET", "users/me/calendarList")
        connection.refresh_from_db()
        self.assertTrue(connection.reconnect_required)

    def test_callback_access_logs_are_redacted(self):
        import logging
        from .logging import RedactCalendarCallback

        record = logging.LogRecord("django.server", logging.INFO, "", 0,
                                   '"GET %s HTTP/1.1" 200',
                                   ("/api/calendar/google/callback/?code=secret&state=proof",), None)
        RedactCalendarCallback().filter(record)
        self.assertNotIn("secret", record.getMessage())
        self.assertNotIn("proof", record.getMessage())
