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
            response = self.client.post("/api/calendar/availability/", query, format="json")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.data["code"], "invalid_availability_calendars")
            self.assertEqual(response.data["calendar_errors"][0]["calendar_id"], "owned")
            self.assertIn("/api/calendar/preferences/", response.data["detail"])
            self.assertNotIn("calendars", response.data)
        self.assertEqual(CalendarConnection.objects.get().source_calendars, ["owned"])

    def test_availability_errors_distinguish_selection_from_provider_failures(self):
        self.connection(source_calendars=["owned", "shared"])
        query = {"time_min": "2026-11-01T00:00:00Z", "time_max": "2026-11-02T00:00:00Z"}
        for reason, expected in (("forbidden", 400), ("internalError", 502), ("unknown", 502)):
            with self.subTest(reason=reason), patch("calendars.services.api", return_value={
                "calendars": {"owned": {"busy": []}, "shared": {"errors": [{"reason": reason}]}}
            }):
                response = self.client.post("/api/calendar/availability/", query, format="json")
                self.assertEqual(response.status_code, expected)
                self.assertEqual([e["calendar_id"] for e in response.data["calendar_errors"]], ["shared"])
                self.assertNotIn("calendars", response.data)
        with patch("calendars.services.api", return_value={"calendars": {
            "owned": {"errors": [{"reason": "notFound"}]},
            "shared": {"errors": [{"reason": "internalError"}]},
        }}):
            response = self.client.post("/api/calendar/availability/", query, format="json")
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.data["code"], "availability_incomplete")
            self.assertEqual(len(response.data["calendar_errors"]), 2)

    def test_missing_or_malformed_availability_is_not_free_time(self):
        self.connection()
        query = {"time_min": "2026-11-01T00:00:00Z", "time_max": "2026-11-02T00:00:00Z"}
        for payload in ({}, {"calendars": None}, {"calendars": {"owned": {}}},
                        {"calendars": {"owned": {"errors": "invalid"}}}):
            with self.subTest(payload=payload), patch("calendars.services.api", return_value=payload):
                response = self.client.post("/api/calendar/availability/", query, format="json")
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.data["code"], "availability_incomplete")
                self.assertNotIn("calendars", response.data)

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


class SchedulingRulesTests(APITestCase):
    preferences_url = "/api/calendar/scheduling-preferences/"
    periods_url = "/api/calendar/protected-periods/"
    windows_url = "/api/calendar/scheduling-windows/"

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = get_user_model().objects.create_user(email="scheduler@gmail.com")
        self.client.force_authenticate(self.user)
        self.preferences = {
            "timezone": "Europe/London", "preferred_session_minutes": 60,
            "working_hours": [{"weekday": 0, "start_time": "09:00", "end_time": "17:00"}],
        }

    def configure(self, **changes):
        response = self.client.put(self.preferences_url, {**self.preferences, **changes}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def weekly(self, **changes):
        return self.client.post(self.periods_url, {
            "kind": "weekly", "title": "Lunch", "weekdays": [0],
            "start_time": "12:00", "end_time": "13:00", **changes,
        }, format="json")

    def preview(self, start="2026-11-02T00:00:00Z", end="2026-11-03T00:00:00Z"):
        return self.client.post(self.windows_url, {"time_min": start, "time_max": end}, format="json")

    def test_requires_authentication_and_explicit_setup(self):
        self.assertEqual(self.client.get(self.preferences_url).data, {"configured": False})
        self.assertEqual(self.preview().status_code, 400)
        self.assertEqual(self.weekly().status_code, 400)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.preferences_url).status_code, 401)
        self.assertEqual(self.client.get(self.periods_url).status_code, 401)
        self.assertEqual(self.preview().status_code, 401)

    def test_preferences_roundtrip_and_replace_without_google_connection(self):
        self.configure()
        self.assertEqual(self.client.get(self.preferences_url).data["timezone"], "Europe/London")
        self.configure(working_hours=[])
        self.assertEqual(self.preview().data["windows"], [])

    def test_invalid_preferences_do_not_replace_existing_rules(self):
        self.configure()
        invalid = [
            {"timezone": "Not/AZone"}, {"preferred_session_minutes": 0},
            {"working_hours": [{"weekday": 7, "start_time": "09:00", "end_time": "17:00"}]},
            {"working_hours": [{"weekday": 0, "start_time": "17:00", "end_time": "09:00"}]},
            {"working_hours": [{"weekday": 0, "start_time": "09:00:01", "end_time": "17:00"}]},
            {"working_hours": [self.preferences["working_hours"][0], {"weekday": 0, "start_time": "16:00", "end_time": "18:00"}]},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.assertEqual(self.client.put(self.preferences_url, {**self.preferences, **changes}, format="json").status_code, 400)
        self.assertEqual(self.client.get(self.preferences_url).data["working_hours"][0]["start_time"], "09:00")

    def test_weekly_and_one_off_subtracted_and_clipped_exactly(self):
        self.configure()
        self.assertEqual(self.weekly().status_code, 201)
        response = self.client.post(self.periods_url, {
            "kind": "one_off", "starts_at": "2026-11-02T14:00:15Z", "ends_at": "2026-11-02T15:00:30Z",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        response = self.preview()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["google_busy_time_checked"])
        windows = response.data["windows"]
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0]["starts_at"].hour, 9)
        self.assertEqual(windows[0]["ends_at"].hour, 12)
        self.assertEqual(windows[1]["ends_at"].second, 15)
        self.assertEqual(windows[2]["starts_at"].second, 30)
        self.assertEqual(windows[2]["ends_at"].hour, 17)
        clipped = self.preview("2026-11-02T09:00:30Z", "2026-11-02T10:00:15Z").data["windows"]
        self.assertEqual(clipped[0]["starts_at"].second, 30)
        self.assertEqual(clipped[0]["ends_at"].second, 15)

    def test_grey_period_crud_and_overlap(self):
        self.configure()
        first = self.weekly()
        self.assertEqual(self.weekly(start_time="12:30", end_time="14:00").status_code, 201)
        self.assertEqual(self.preview().data["windows"][1]["starts_at"].hour, 14)
        url = self.periods_url + str(first.data["id"]) + "/"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.patch(url, {"title": "Break"}, format="json").status_code, 200)
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(len(self.client.get(self.periods_url).data["protected_periods"]), 1)

    def test_invalid_grey_periods(self):
        self.configure()
        for changes in ({"weekdays": []}, {"weekdays": [0, 0]}, {"weekdays": [7]},
                        {"start_time": "22:00", "end_time": "08:00"},
                        {"all_day": True}, {"start_date": "2026-11-10", "end_date": "2026-11-01"},
                        {"starts_at": "2026-11-02T00:00:00Z"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.weekly(**changes).status_code, 400)
        for start, end in (("2026-11-02T10:00:00", "2026-11-02T11:00:00Z"),
                           ("2026-11-02T11:00:00Z", "2026-11-02T10:00:00Z")):
            self.assertEqual(self.client.post(self.periods_url, {
                "kind": "one_off", "starts_at": start, "ends_at": end,
            }, format="json").status_code, 400)

    def test_all_day_and_date_bounded_recurrence(self):
        self.configure()
        response = self.weekly(all_day=True, start_time=None, end_time=None,
                               start_date="2026-11-02", end_date="2026-11-02")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.preview().data["windows"], [])
        self.assertEqual(len(self.preview("2026-11-09T00:00:00Z", "2026-11-10T00:00:00Z").data["windows"]), 1)

    def test_partial_update_revalidates_existing_range(self):
        self.configure()
        response = self.weekly()
        url = self.periods_url + str(response.data["id"]) + "/"
        self.assertEqual(self.client.patch(url, {"end_time": "11:00"}, format="json").status_code, 400)
        self.assertEqual(self.client.get(url).data["end_time"], "13:00:00")

    def test_user_isolation(self):
        self.configure()
        response = self.weekly()
        url = self.periods_url + str(response.data["id"]) + "/"
        other = get_user_model().objects.create_user(email="outsider@gmail.com")
        self.client.force_authenticate(other)
        self.assertFalse(self.client.get(self.preferences_url).data["configured"])
        self.assertEqual(self.client.get(self.periods_url).data["protected_periods"], [])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.patch(url, {"title": "Changed"}, format="json").status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)

    def test_rule_limit(self):
        from .models import ProtectedPeriod

        self.configure()
        ProtectedPeriod.objects.bulk_create([
            ProtectedPeriod(user=self.user, kind="weekly", weekdays=[0], all_day=True) for _ in range(100)
        ])
        self.assertEqual(self.weekly().status_code, 400)

    def test_dst_fall_back_protects_both_repeated_hours(self):
        self.configure(working_hours=[{"weekday": 6, "start_time": "00:00", "end_time": "04:00"}])
        self.assertEqual(self.weekly(weekdays=[6], start_time="01:00", end_time="02:00").status_code, 201)
        response = self.preview("2026-10-25T00:00:00Z", "2026-10-25T04:00:00Z")
        self.assertEqual(len(response.data["windows"]), 1)
        self.assertEqual(response.data["windows"][0]["starts_at"].hour, 2)

    def test_dst_spring_forward_and_timezone_change(self):
        self.configure(working_hours=[{"weekday": 6, "start_time": "00:00", "end_time": "04:00"}])
        windows = self.preview("2027-03-28T00:00:00Z", "2027-03-28T04:00:00Z").data["windows"]
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["ends_at"].hour, 3)
        self.configure(timezone="America/New_York")
        windows = self.preview().data["windows"]
        self.assertEqual(windows[0]["starts_at"].hour, 14)

    def test_no_google_requests_for_local_rules(self):
        self.configure()
        with patch("calendars.services.api") as google:
            self.assertEqual(self.preview().status_code, 200)
            google.assert_not_called()


class TaskSessionTests(APITestCase):
    def setUp(self):
        from goals.models import Goal, Plan
        from tasks.models import Task
        from .models import SchedulingPreferences

        cache.clear()
        self.addCleanup(cache.clear)
        self.user = get_user_model().objects.create_user(email='sessions@gmail.com', google_sub='subject')
        self.client.force_authenticate(self.user)
        self.plan = Plan.objects.create(user=self.user, title='Study', raw_input='Study')
        self.goal = Goal.objects.create(user=self.user, plan=self.plan, title='Learn arrays')
        self.task = Task.objects.create(goal=self.goal, title='Solve two array exercises',
                                        estimated_minutes=60, completion_criteria='Two solutions reviewed', readiness='ready')
        self.start = (timezone.now() + timedelta(days=10)).replace(hour=9, minute=0, second=0, microsecond=0)
        self.end = self.start + timedelta(hours=8)
        SchedulingPreferences.objects.create(user=self.user, timezone='UTC', working_hours=[
            {'weekday': day, 'start_time': '09:00', 'end_time': '17:00'} for day in range(7)
        ])
        CalendarConnection.objects.create(
            user=self.user, google_sub='subject', email=self.user.email, credentials='not-used',
            expires_at=timezone.now() + timedelta(hours=1), source_calendars=['source'], destination_calendar='target',
        )
        self.mock_api = patch('calendars.services.api', side_effect=self.freebusy).start()
        self.addCleanup(patch.stopall)
        patch('calendars.services.list_calendars', return_value=[{'id': 'target', 'accessRole': 'owner'}]).start()

    def freebusy(self, user, method, path, **kwargs):
        return {'calendars': {item['id']: {'busy': []} for item in kwargs['json']['items']}}

    def propose(self, task=None):
        task = task or self.task
        return self.client.post(f'/api/tasks/{task.pk}/sessions/propose/', {
            'time_min': self.start.isoformat(), 'time_max': self.end.isoformat(),
        }, format='json')

    def session(self):
        from .models import WorkSession

        response = self.propose()
        self.assertEqual(response.status_code, 201, response.data)
        return WorkSession.objects.get(pk=response.data['id'])

    def approve(self, session):
        return self.client.post(f'/api/sessions/{session.pk}/approve/')

    def test_manual_task_create_update_and_revision(self):
        from goals.services import restore_plan_revision

        response = self.client.post('/api/tasks/', {
            'goal_id': str(self.goal.pk), 'title': 'Manual task', 'estimated_minutes': 30,
            'completion_criteria': 'Written notes', 'readiness': 'ready',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.plan.refresh_from_db()
        original = self.plan.revisions.get(version=self.plan.version)
        task_id = response.data['id']
        response = self.client.patch(f'/api/tasks/{task_id}/', {'estimated_minutes': 45}, format='json')
        self.assertEqual(response.status_code, 200)
        restore_plan_revision(original, self.user)
        self.assertEqual(self.client.get(f'/api/tasks/{task_id}/').data['estimated_minutes'], 30)

    def test_task_validation_and_unready_tasks(self):
        from tasks.models import Task

        for body in ({'readiness': 'ready', 'completion_criteria': ''}, {'estimated_minutes': 0}, {'estimated_minutes': 481}):
            self.assertEqual(self.client.patch(f'/api/tasks/{self.task.pk}/', body, format='json').status_code, 400)
        task = Task.objects.create(goal=self.goal, title='Legacy task')
        self.assertEqual(self.propose(task).status_code, 400)
        self.assertEqual(self.client.post('/api/tasks/', {'goal_id': 'invalid', 'title': 'Task'}, format='json').status_code, 400)

    def test_proposal_has_no_external_writes(self):
        with patch('calendars.sessions.event_request') as event:
            session = self.session()
            self.assertEqual(session.status, 'proposed')
            self.assertEqual(session.starts_at, self.start)
            self.assertEqual(session.ends_at - session.starts_at, timedelta(hours=1))
            self.assertEqual(session.event_payload['visibility'], 'private')
            event.assert_not_called()

    def test_proposal_respects_google_grey_and_other_sessions(self):
        from .models import ProtectedPeriod
        from tasks.models import Task

        ProtectedPeriod.objects.create(user=self.user, kind='one_off', starts_at=self.start, ends_at=self.start + timedelta(hours=1))
        def busy(user, method, path, **kwargs):
            return {'calendars': {i['id']: {'busy': [{'start': (self.start + timedelta(hours=1)).isoformat(),
                     'end': (self.start + timedelta(hours=2)).isoformat()}]} for i in kwargs['json']['items']}}
        self.mock_api.side_effect = busy
        first = self.session()
        self.assertEqual(first.starts_at, self.start + timedelta(hours=2))
        second_task = Task.objects.create(goal=self.goal, title='Other work', estimated_minutes=60,
                                          completion_criteria='Done', readiness='ready')
        response = self.propose(second_task)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['starts_at'], (self.start + timedelta(hours=3)).isoformat().replace('+00:00', 'Z'))

    def test_no_slot_and_missing_availability_do_not_make_proposals(self):
        from .models import WorkSession

        self.task.estimated_minutes = 480
        self.task.save()
        self.end = self.start + timedelta(hours=1)
        self.assertEqual(self.propose().status_code, 409)
        self.mock_api.return_value = {}
        self.mock_api.side_effect = None
        self.assertEqual(self.propose().status_code, 502)
        self.assertFalse(WorkSession.objects.exists())

    def test_deadline_and_completed_tasks(self):
        self.task.due_date = (self.start - timedelta(days=1)).date()
        self.task.save()
        self.assertEqual(self.propose().status_code, 409)
        self.task.is_completed = True
        self.task.save()
        self.assertEqual(self.propose().status_code, 400)

    def test_approval_publishes_once_and_replays_without_write(self):
        session = self.session()
        with patch('calendars.sessions.event_request', return_value=(200, session.event_payload)) as event:
            response = self.approve(session)
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data['status'], 'published')
            self.assertEqual(self.approve(session).status_code, 200)
            self.assertEqual(event.call_count, 1)
        self.assertEqual(self.propose().status_code, 409)

    def test_changed_task_and_rules_block_approval(self):
        from .models import ProtectedPeriod

        session = self.session()
        self.task.title = 'Different work'
        self.task.save()
        with patch('calendars.sessions.event_request') as event:
            self.assertEqual(self.approve(session).status_code, 409)
            event.assert_not_called()
        self.task.title = session.task_snapshot['title']
        self.task.save()
        ProtectedPeriod.objects.create(user=self.user, kind='one_off', starts_at=session.starts_at, ends_at=session.ends_at)
        with patch('calendars.sessions.event_request') as event:
            self.assertEqual(self.approve(session).status_code, 409)
            event.assert_not_called()

    def test_new_busy_event_blocks_approval(self):
        session = self.session()
        self.mock_api.side_effect = lambda user, method, path, **kwargs: {'calendars': {
            i['id']: {'busy': [{'start': session.starts_at.isoformat(), 'end': session.ends_at.isoformat()}]}
            for i in kwargs['json']['items']}}
        with patch('calendars.sessions.event_request') as event:
            self.assertEqual(self.approve(session).status_code, 409)
            event.assert_not_called()

    def test_timeout_reconciles_same_event_without_reinsertion(self):
        session = self.session()
        with patch('calendars.sessions.event_request', side_effect=services.CalendarError()):
            self.assertEqual(self.approve(session).status_code, 502)
        session.refresh_from_db()
        self.assertEqual(session.status, 'publishing')
        with patch('calendars.sessions.event_request', return_value=(200, session.event_payload)) as event:
            self.assertEqual(self.approve(session).status_code, 200)
            self.assertEqual(event.call_count, 1)
            self.assertEqual(event.call_args.args[2], 'GET')

    def test_retry_missing_event_reuses_fixed_id_and_handles_duplicate(self):
        session = self.session()
        session.status = 'publishing'
        session.save()
        with patch('calendars.sessions.event_request', side_effect=[(404, {}), (409, {}), (200, session.event_payload)]) as event:
            self.assertEqual(self.approve(session).status_code, 200)
            self.assertEqual([c.args[2] for c in event.call_args_list], ['GET', 'POST', 'GET'])
            self.assertEqual(event.call_args_list[1].args[3]['id'], session.google_event_id)

    def test_different_existing_event_is_never_overwritten(self):
        session = self.session()
        session.status = 'publishing'
        session.save()
        with patch('calendars.sessions.event_request', return_value=(200, {'id': session.google_event_id})) as event:
            self.assertEqual(self.approve(session).status_code, 409)
            self.assertEqual(event.call_count, 1)

    def test_reject_expiry_and_missing_task(self):
        session = self.session()
        self.assertEqual(self.client.post(f'/api/sessions/{session.pk}/reject/').status_code, 200)
        self.assertEqual(self.approve(session).status_code, 409)
        session = self.session()
        session.expires_at = timezone.now() - timedelta(seconds=1)
        session.save()
        self.assertEqual(self.approve(session).status_code, 409)
        session = self.session()
        self.task.delete()
        self.assertEqual(self.approve(session).status_code, 409)

    def test_user_isolation(self):
        session = self.session()
        other = get_user_model().objects.create_user(email='other-session@gmail.com')
        self.client.force_authenticate(other)
        self.assertEqual(self.propose().status_code, 404)
        self.assertEqual(self.approve(session).status_code, 404)
        self.assertEqual(self.client.get(f'/api/sessions/{session.pk}/').status_code, 404)
        self.assertEqual(self.client.patch(f'/api/tasks/{self.task.pk}/', {'title': 'Changed'}, format='json').status_code, 404)
        self.assertEqual(self.client.post('/api/tasks/', {'goal_id': str(self.goal.pk), 'title': 'Changed'}, format='json').status_code, 404)

    def test_publishing_intent_prevents_rejection_and_second_proposal(self):
        session = self.session()
        session.status = 'publishing'
        session.save()
        self.assertEqual(self.client.post(f'/api/sessions/{session.pk}/reject/').status_code, 409)
        self.assertEqual(self.propose().status_code, 409)
        response = self.client.patch(f'/api/tasks/{self.task.pk}/', {'title': 'Changed'}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_destinations_are_rechecked_and_changes_require_new_proposal(self):
        session = self.session()
        CalendarConnection.objects.filter(user=self.user).update(destination_calendar='different')
        with patch('calendars.sessions.event_request') as event:
            self.assertEqual(self.approve(session).status_code, 409)
            event.assert_not_called()

    def test_approval_lease_blocks_simultaneous_calls_and_recovers_after_crash(self):
        import uuid

        session = self.session()
        session.publish_attempt = uuid.uuid4()
        session.lease_until = timezone.now() + timedelta(minutes=1)
        session.save()
        with patch('calendars.sessions.event_request') as event:
            self.assertEqual(self.approve(session).status_code, 409)
            event.assert_not_called()
        session.lease_until = timezone.now() - timedelta(seconds=1)
        session.save()
        with patch('calendars.sessions.event_request', return_value=(200, session.event_payload)):
            self.assertEqual(self.approve(session).status_code, 200)
        session.refresh_from_db()
        self.assertIsNone(session.publish_attempt)
        self.assertIsNone(session.lease_until)

    def test_deleted_restored_task_cannot_duplicate_published_session(self):
        from tasks.models import Task

        session = self.session()
        with patch('calendars.sessions.event_request', return_value=(200, session.event_payload)):
            self.assertEqual(self.approve(session).status_code, 200)
        pk = self.task.pk
        self.task.delete()
        self.task = Task.objects.create(pk=pk, goal=self.goal, title='Restored', estimated_minutes=60,
                                        readiness='ready', completion_criteria='Done')
        self.assertEqual(self.propose().status_code, 409)

    def test_actual_event_transport_uses_fixed_id_and_marks_revocation(self):
        from . import sessions
        import uuid

        session = self.session()
        session.status = 'publishing'
        session.publish_attempt = uuid.uuid4()
        session.lease_until = timezone.now() + timedelta(minutes=2)
        session.save()
        with patch('calendars.services.access_token', return_value='provider-token'), patch(
            'calendars.services.request_json', return_value=(200, session.event_payload)
        ) as transport:
            status, _ = sessions.event_request(self.user, session, 'POST', session.event_payload)
            self.assertEqual(status, 200)
            self.assertIn('/calendars/target/events', transport.call_args.args[1])
            self.assertEqual(transport.call_args.kwargs['json']['id'], session.google_event_id)
            self.assertNotIn('attendees', transport.call_args.kwargs['json'])
        with patch('calendars.services.access_token', return_value='provider-token'), patch(
            'calendars.services.request_json', return_value=(401, {})
        ), self.assertRaises(services.ReconnectRequired):
            sessions.event_request(self.user, session, 'GET')
        self.assertTrue(CalendarConnection.objects.get(user=self.user).reconnect_required)

    def test_different_timezone_changes_deadline_at_approval(self):
        from .models import SchedulingPreferences

        self.start += timedelta(hours=2)
        self.task.due_date = self.start.date()
        self.task.save()
        session = self.session()
        # Moving the local deadline into an earlier UTC instant must invalidate the slot.
        SchedulingPreferences.objects.filter(user=self.user).update(timezone='Pacific/Kiritimati')
        with patch('calendars.sessions.event_request') as event:
            response = self.approve(session)
            self.assertEqual(response.status_code, 409)
            event.assert_not_called()

    def test_rule_change_just_before_insert_does_not_trap_unsubmitted_session(self):
        from . import sessions
        from .models import ProtectedPeriod

        session = self.session()
        transport = sessions.event_request
        def change_rules(user, current, method, payload=None):
            ProtectedPeriod.objects.create(user=user, kind='one_off',
                                           starts_at=current.starts_at, ends_at=current.ends_at)
            return transport(user, current, method, payload)
        with patch('calendars.sessions.event_request', side_effect=change_rules), patch(
            'calendars.services.access_token', return_value='token'
        ), patch('calendars.services.request_json') as google:
            self.assertEqual(self.approve(session).status_code, 409)
            google.assert_not_called()
        session.refresh_from_db()
        self.assertEqual(session.status, 'proposed')
        self.assertEqual(self.client.post(f'/api/sessions/{session.pk}/reject/').status_code, 200)

    def test_dependency_completion_is_rechecked_before_calendar_write(self):
        from goals.models import Plan
        from tasks.models import Task, TaskDependency

        parent = Task.objects.create(goal=self.goal, title='Read first', is_completed=True)
        TaskDependency.objects.create(task=self.task, prerequisite=parent)
        session = self.session()
        parent.is_completed = False
        parent.save()
        with patch('calendars.sessions.event_request') as event:
            response = self.approve(session)
        self.assertEqual(response.status_code, 400, response.data)
        event.assert_not_called()
        self.assertEqual(self.propose().status_code, 400)
        parent.is_completed = True
        parent.save()
        self.task.planning_context_version = self.plan.context_version
        self.task.save()
        # A context correction must block both a new slot and approval of the old slot.
        Plan.objects.filter(pk=self.plan.pk).update(context_version=self.plan.context_version + 1)
        self.assertEqual(self.propose().status_code, 400)
        with patch('calendars.sessions.event_request') as event:
            self.assertEqual(self.approve(session).status_code, 400)
        event.assert_not_called()

    def test_old_session_snapshot_remains_compatible_without_dependencies(self):
        session = self.session()
        for field in ('dependencies', 'blocker_reason', 'planning_context_version'):
            session.task_snapshot.pop(field)
        session.save()
        with patch('calendars.sessions.event_request', side_effect=lambda user, current, method, payload=None: (201, payload)):
            response = self.approve(session)
        self.assertEqual(response.status_code, 200, response.data)

    def report_outcome(self, **changes):
        import uuid

        self.plan.refresh_from_db()
        return self.client.post(f'/api/tasks/{self.task.pk}/outcomes/', {
            'client_id': str(uuid.uuid4()), 'plan_version': self.plan.version, 'status': 'partial',
            'remaining_minutes': 20, 'notes': 'First part completed.', **changes,
        }, format='json')

    def test_progress_releases_tentative_reservation_and_uses_remaining_effort(self):
        session = self.session()
        response = self.report_outcome()
        self.assertEqual(response.status_code, 201, response.data)
        session.refresh_from_db()
        self.assertEqual(session.status, 'rejected')
        with patch('calendars.sessions.event_request') as event:
            self.assertEqual(self.approve(session).status_code, 409)
        event.assert_not_called()
        replacement = self.propose()
        self.assertEqual(replacement.status_code, 201, replacement.data)
        from datetime import datetime
        starts = datetime.fromisoformat(replacement.data['starts_at'].replace('Z', '+00:00'))
        ends = datetime.fromisoformat(replacement.data['ends_at'].replace('Z', '+00:00'))
        self.assertEqual(ends - starts, timedelta(minutes=20))
        self.assertEqual(replacement.data['task_snapshot']['estimated_minutes'], 60)
        self.assertEqual(replacement.data['task_snapshot']['remaining_minutes'], 20)

    def test_published_session_outcome_keeps_google_event_and_payload_unchanged(self):
        session = self.session()
        with patch('calendars.sessions.event_request', side_effect=lambda user, current, method, payload=None: (201, payload)):
            self.assertEqual(self.approve(session).status_code, 200)
        session.refresh_from_db()
        original_payload = session.event_payload.copy()
        with patch('calendars.sessions.event_request') as event:
            response = self.report_outcome(status='completed', remaining_minutes=0, session_id=str(session.pk))
        self.assertEqual(response.status_code, 201, response.data)
        event.assert_not_called()
        session.refresh_from_db()
        self.assertEqual(session.status, 'published')
        self.assertEqual(session.event_payload, original_payload)
        self.assertEqual(response.data['outcome']['session_id'], str(session.pk))
        self.assertEqual(response.data['task']['progress_status'], 'completed')

    def test_unconfirmed_publication_blocks_outcomes_and_leaves_progress_unchanged(self):
        from tasks.models import TaskOutcome

        session = self.session()
        session.status = 'publishing'
        session.save()
        response = self.report_outcome()
        self.assertEqual(response.status_code, 409, response.data)
        self.assertFalse(TaskOutcome.objects.exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.progress_version, 0)

    def test_outcome_cannot_claim_an_unpublished_or_another_tasks_session(self):
        from tasks.models import Task

        session = self.session()
        self.assertEqual(self.report_outcome(session_id=str(session.pk)).status_code, 400)
        other = Task.objects.create(goal=self.goal, title='Other')
        session.task = other
        session.task_snapshot['task_id'] = str(other.pk)
        session.status = 'published'
        session.save()
        self.assertEqual(self.report_outcome(session_id=str(session.pk)).status_code, 400)

    def test_legacy_snapshot_without_progress_fields_remains_publishable(self):
        session = self.session()
        for field in ('remaining_minutes', 'progress_version'):
            session.task_snapshot.pop(field)
        session.save()
        with patch('calendars.sessions.event_request', side_effect=lambda user, current, method, payload=None: (201, payload)):
            response = self.approve(session)
        self.assertEqual(response.status_code, 200, response.data)


class SessionManagementTests(APITestCase):
    def setUp(self):
        import copy
        import uuid
        from goals.models import Goal, Plan
        from tasks.models import Task
        from .models import WorkSession, SchedulingPreferences
        from .sessions import snapshot

        cache.clear()
        self.addCleanup(cache.clear)
        self.user = get_user_model().objects.create_user(email='management@example.com', google_sub='subject')
        self.client.force_authenticate(self.user)
        self.plan = Plan.objects.create(user=self.user, title='Study', raw_input='Study')
        self.goal = Goal.objects.create(user=self.user, plan=self.plan, title='Arrays')
        self.task = Task.objects.create(goal=self.goal, title='Write traversal', description='Write a loop.',
            estimated_minutes=60, completion_criteria='Print five values.', readiness='ready')
        self.start = (timezone.now() + timedelta(days=10)).replace(hour=9, minute=0, second=0, microsecond=0)
        SchedulingPreferences.objects.create(user=self.user, timezone='UTC', working_hours=[
            {'weekday': day, 'start_time': '09:00', 'end_time': '17:00'} for day in range(7)])
        CalendarConnection.objects.create(user=self.user, google_sub='subject', email=self.user.email,
            credentials='unused', expires_at=timezone.now() + timedelta(hours=1), source_calendars=['target', 'source'], destination_calendar='target')
        session_id = uuid.uuid4()
        payload = {'id': session_id.hex, 'summary': self.task.title, 'description': self.task.description,
            'start': {'dateTime': self.start.isoformat()}, 'end': {'dateTime': (self.start + timedelta(hours=1)).isoformat()},
            'extendedProperties': {'private': {'yiyara_session_id': str(session_id), 'other_key': 'preserve'}}}
        self.session = WorkSession.objects.create(id=session_id, user=self.user, task=self.task, status='published',
            starts_at=self.start, ends_at=self.start + timedelta(hours=1), expires_at=self.start,
            destination_calendar='target', google_sub='subject', google_event_id=session_id.hex,
            task_snapshot=snapshot(self.task), event_payload=payload, published_at=timezone.now())
        self.event = {**copy.deepcopy(payload), 'etag': '"version-1"', 'status': 'confirmed'}
        self.events = []
        self.other_busy = []
        self.writes = []
        self.timeout_after_write = False
        self.timeout_before_write = False
        self.force412 = False
        self.counter = 1
        patch('calendars.services.access_token', return_value='test-token').start()
        patch('calendars.services.list_calendars', return_value=[{'id': 'target', 'accessRole': 'owner'}]).start()
        self.api = patch('calendars.services.api', side_effect=self.calendar_api).start()
        self.real_request_json = services.request_json
        self.transport = patch('calendars.services.request_json', side_effect=self.provider).start()
        self.addCleanup(patch.stopall)

    def calendar_api(self, user, method, path, **kwargs):
        import copy

        if path == 'freeBusy':
            return {'calendars': {item['id']: {'busy': self.other_busy} for item in kwargs['json']['items']}}
        if method == 'GET' and path == 'calendars/target/events':
            return {'timeZone': 'UTC', 'items': copy.deepcopy(([self.event] if self.event else []) + self.events)}
        raise AssertionError((method, path))

    def provider(self, method, url, **kwargs):
        import copy

        self.assertTrue(url.endswith('/calendars/target/events/' + self.session.google_event_id))
        if method == 'GET':
            return (200, copy.deepcopy(self.event)) if self.event else (404, {})
        self.assertIn(method, ('PATCH', 'DELETE'))
        if self.timeout_before_write:
            self.timeout_before_write = False
            raise services.CalendarError('Timeout')
        if self.force412 or (self.event and kwargs['headers'].get('If-Match') != self.event['etag']):
            return 412, {}
        self.writes.append((method, copy.deepcopy(kwargs)))
        if method == 'DELETE':
            self.event = None
            result = (204, {})
        else:
            self.event.update(copy.deepcopy(kwargs['json']))
            self.counter += 1
            self.event['etag'] = f'"version-{self.counter}"'
            result = (200, copy.deepcopy(self.event))
        if self.timeout_after_write:
            self.timeout_after_write = False
            raise services.CalendarError('Timeout after provider write')
        return result

    def move(self, **changes):
        import uuid

        return self.client.post(f'/api/sessions/{self.session.pk}/reschedule/', {
            'client_id': str(uuid.uuid4()), 'time_min': (self.start + timedelta(hours=2)).isoformat(),
            'time_max': (self.start + timedelta(hours=6)).isoformat(), 'reason': 'Prefer afternoon.', **changes}, format='json')

    def cancel(self, **changes):
        import uuid

        return self.client.post(f'/api/sessions/{self.session.pk}/cancel/', {
            'client_id': str(uuid.uuid4()), 'reason': 'No longer want this calendar session.', **changes}, format='json')

    def approve_change(self, response, **fields):
        return self.client.post(f"/api/sessions/changes/{response.data['change']['id']}/approve/", fields, format='json')

    def test_move_updates_same_event_after_approval_and_preserves_task_and_extra_properties(self):
        response = self.move()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(self.writes)
        self.session.refresh_from_db()
        self.assertEqual(self.session.starts_at, self.start)
        result = self.approve_change(response)
        self.assertEqual(result.status_code, 200, result.data)
        self.session.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.session.starts_at, self.start + timedelta(hours=2))
        self.assertEqual(self.session.ends_at - self.session.starts_at, timedelta(hours=1))
        self.assertEqual(self.session.management_version, 1)
        self.assertEqual(self.session.status, 'published')
        self.assertEqual(self.event['id'], self.session.google_event_id)
        self.assertEqual(self.event['extendedProperties']['private']['other_key'], 'preserve')
        self.assertEqual(self.event['summary'], self.task.title)
        self.assertFalse(self.task.is_completed)
        self.assertEqual(self.task.progress_version, 0)
        self.assertEqual([method for method, _ in self.writes], ['PATCH'])
        self.assertEqual(self.writes[0][1]['headers']['If-Match'], '"version-1"')
        self.assertEqual(self.approve_change(response).status_code, 200)
        self.assertEqual(len(self.writes), 1)

    def test_cancel_preserves_progress_releases_task_and_cannot_be_republished(self):
        response = self.cancel()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(self.writes)
        result = self.approve_change(response)
        self.assertEqual(result.status_code, 200, result.data)
        self.session.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.session.status, 'cancelled')
        self.assertIsNotNone(self.session.cancelled_at)
        self.assertFalse(self.task.is_completed)
        self.assertEqual(self.task.progress_status, 'not_started')
        self.assertEqual(self.writes[0][0], 'DELETE')
        self.assertEqual(self.approve_change(response).status_code, 200)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.client.post(f'/api/sessions/{self.session.pk}/approve/').status_code, 409)
        new = self.client.post(f'/api/tasks/{self.task.pk}/sessions/propose/', {
            'time_min': self.start.isoformat(), 'time_max': (self.start + timedelta(hours=8)).isoformat()}, format='json')
        self.assertEqual(new.status_code, 201, new.data)

    def test_overlap_with_original_event_still_checks_other_overlapping_events(self):
        # The original event is 09:00–10:00. Another event inside it must not be subtracted away.
        self.events = [{'id': 'other', 'start': {'dateTime': (self.start + timedelta(minutes=45)).isoformat()},
                        'end': {'dateTime': (self.start + timedelta(hours=1)).isoformat()}}]
        response = self.move(time_min=(self.start + timedelta(minutes=30)).isoformat(),
                             time_max=(self.start + timedelta(hours=3)).isoformat())
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['change']['starts_at'], (self.start + timedelta(hours=1)).isoformat().replace('+00:00', 'Z'))
        # Without the unrelated busy event an overlapping move to 09:30 is allowed.
        self.events = []
        replacement = self.move(time_min=(self.start + timedelta(minutes=30)).isoformat(),
                                time_max=(self.start + timedelta(hours=3)).isoformat())
        self.assertEqual(replacement.status_code, 201, replacement.data)
        self.assertEqual(replacement.data['change']['starts_at'], (self.start + timedelta(minutes=30)).isoformat().replace('+00:00', 'Z'))

    def test_other_sources_grey_periods_and_other_reservations_block_move(self):
        from .models import ProtectedPeriod, WorkSession
        from tasks.models import Task
        import uuid

        left = self.start + timedelta(hours=2)
        self.other_busy = [{'start': left.isoformat(), 'end': (left + timedelta(hours=1)).isoformat()}]
        ProtectedPeriod.objects.create(user=self.user, kind='one_off', starts_at=left + timedelta(hours=1), ends_at=left + timedelta(hours=2))
        other = Task.objects.create(goal=self.goal, title='Other')
        WorkSession.objects.create(user=self.user, task=other, starts_at=left + timedelta(hours=2), ends_at=left + timedelta(hours=3),
            expires_at=left, google_event_id=uuid.uuid4().hex, destination_calendar='target', google_sub='subject')
        response = self.move()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['change']['starts_at'], (left + timedelta(hours=3)).isoformat().replace('+00:00', 'Z'))

    def test_new_conflict_after_proposal_prevents_patch(self):
        response = self.move()
        self.assertEqual(response.status_code, 201)
        left = self.start + timedelta(hours=2)
        self.events = [{'id': 'new', 'start': {'dateTime': left.isoformat()}, 'end': {'dateTime': (left + timedelta(hours=1)).isoformat()}}]
        result = self.approve_change(response)
        self.assertEqual(result.status_code, 409, result.data)
        self.assertFalse(self.writes)
        self.session.refresh_from_db()
        self.assertEqual(self.session.starts_at, self.start)

    def test_provider_changes_after_proposal_and_at_conditional_write_are_not_overwritten(self):
        response = self.move()
        self.event['etag'] = '"external-edit"'
        self.event['summary'] = 'User edit'
        self.assertEqual(self.approve_change(response).status_code, 409)
        self.assertFalse(self.writes)
        replacement = self.move()
        self.assertEqual(replacement.status_code, 201, replacement.data)
        self.force412 = True
        result = self.approve_change(replacement)
        self.assertEqual(result.status_code, 409, result.data)
        from .models import SessionChange
        self.assertEqual(SessionChange.objects.get(pk=replacement.data['change']['id']).status, 'proposed')
        self.assertEqual(self.event['summary'], 'User edit')

    def test_patch_timeout_reconciles_same_change_without_second_write(self):
        from .models import SessionChange

        response = self.move()
        self.timeout_after_write = True
        self.assertEqual(self.approve_change(response).status_code, 502)
        change = SessionChange.objects.get(pk=response.data['change']['id'])
        self.assertEqual(change.status, 'applying')
        self.assertEqual(self.move().status_code, 409)
        self.assertEqual(self.client.post(f'/api/sessions/changes/{change.pk}/reject/').status_code, 409)
        self.assertEqual(self.approve_change(response).status_code, 200)
        self.assertEqual(len(self.writes), 1)
        self.session.refresh_from_db()
        self.assertEqual(self.session.starts_at, self.start + timedelta(hours=2))

    def test_delete_timeout_reconciles_absence_without_second_delete(self):
        response = self.cancel()
        self.timeout_after_write = True
        self.assertEqual(self.approve_change(response).status_code, 502)
        self.assertIsNone(self.event)
        self.assertEqual(self.approve_change(response).status_code, 200)
        self.assertEqual(len(self.writes), 1)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'cancelled')

    def test_timeout_before_provider_write_retries_same_etag(self):
        response = self.move()
        self.timeout_before_write = True
        self.assertEqual(self.approve_change(response).status_code, 502)
        self.assertFalse(self.writes)
        self.assertEqual(self.approve_change(response).status_code, 200)
        self.assertEqual(len(self.writes), 1)

    def test_deleted_event_cancellation_is_local_only_and_move_does_not_recreate_it(self):
        self.event = None
        self.assertEqual(self.move().status_code, 409)
        response = self.cancel()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.approve_change(response).status_code, 200)
        self.assertFalse(self.writes)

    def test_identity_mismatch_guest_events_and_recurring_events_are_protected(self):
        import copy

        original = copy.deepcopy(self.event)
        for mutation in ['identity', 'guest', 'recurrence']:
            self.event = copy.deepcopy(original)
            if mutation == 'identity':
                self.event['extendedProperties']['private']['yiyara_session_id'] = 'unrelated'
            elif mutation == 'guest':
                self.event['attendees'] = [{'email': 'guest@example.com'}]
            else:
                self.event['recurrence'] = ['RRULE:FREQ=WEEKLY']
            with self.subTest(mutation=mutation):
                self.assertEqual(self.cancel().status_code, 409)
                self.assertEqual(self.move().status_code, 409)
        self.assertFalse(self.writes)

    def test_idempotent_request_replay_and_new_proposal_replaces_only_after_success(self):
        import uuid
        from .models import SessionChange

        client_id = str(uuid.uuid4())
        response = self.move(client_id=client_id)
        replay = self.move(client_id=client_id)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data['change']['id'], response.data['change']['id'])
        self.assertEqual(self.cancel(client_id=client_id).status_code, 409)
        # An impossible replacement must not discard the useful old proposal.
        impossible = self.move(time_min=self.start.replace(hour=18).isoformat(), time_max=self.start.replace(hour=19).isoformat())
        self.assertEqual(impossible.status_code, 409)
        self.assertEqual(SessionChange.objects.get(pk=response.data['change']['id']).status, 'proposed')
        replacement = self.cancel()
        self.assertEqual(replacement.status_code, 201)
        self.assertEqual(SessionChange.objects.get(pk=response.data['change']['id']).status, 'rejected')
        self.assertEqual(self.client.post(f"/api/sessions/changes/{replacement.data['change']['id']}/reject/").status_code, 200)
        self.assertFalse(self.writes)

    def test_outcome_invalidates_pending_move_and_is_blocked_during_uncertain_write(self):
        import uuid
        from .models import SessionChange

        response = self.move()
        body = {'client_id': str(uuid.uuid4()), 'plan_version': self.plan.version, 'status': 'partial', 'remaining_minutes': 20}
        reported = self.client.post(f'/api/tasks/{self.task.pk}/outcomes/', body, format='json')
        self.assertEqual(reported.status_code, 201, reported.data)
        self.assertEqual(SessionChange.objects.get(pk=response.data['change']['id']).status, 'rejected')
        # Cancellation remains possible after progress; uncertain cancellation blocks new outcomes.
        cancellation = self.cancel()
        self.timeout_before_write = True
        self.assertEqual(self.approve_change(cancellation).status_code, 502)
        body.update(client_id=str(uuid.uuid4()), plan_version=reported.data['plan_version'])
        self.assertEqual(self.client.post(f'/api/tasks/{self.task.pk}/outcomes/', body, format='json').status_code, 409)

    def test_past_or_completed_session_cannot_move_but_can_be_cancelled(self):
        self.task.is_completed = True
        self.task.save()
        self.assertEqual(self.move().status_code, 400)
        response = self.cancel()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.approve_change(response).status_code, 200)
        self.task.refresh_from_db()
        self.assertTrue(self.task.is_completed)

    def test_foreign_account_ownership_and_unauthenticated_requests(self):
        response = self.move()
        other = get_user_model().objects.create_user(email='foreign-calendar@example.com')
        self.client.force_authenticate(other)
        self.assertEqual(self.move().status_code, 404)
        self.assertEqual(self.approve_change(response).status_code, 404)
        self.assertEqual(self.client.get(f"/api/sessions/changes/{response.data['change']['id']}/").status_code, 404)
        self.client.force_authenticate(None)
        self.assertEqual(self.move().status_code, 401)
        self.client.force_authenticate(self.user)
        CalendarConnection.objects.filter(user=self.user).update(google_sub='different')
        self.assertEqual(self.cancel().status_code, 409)
        self.assertFalse(self.writes)

    def test_all_day_events_and_transparent_events_in_destination(self):
        self.events = [{'id': 'all-day', 'start': {'date': self.start.date().isoformat()},
                        'end': {'date': (self.start + timedelta(days=1)).date().isoformat()}}]
        occupied = self.move()
        self.assertEqual(occupied.status_code, 201, occupied.data)
        self.assertTrue(occupied.data['change']['requires_conflict_approval'])
        self.events[0]['transparency'] = 'transparent'
        free = self.move()
        self.assertEqual(free.status_code, 201)
        self.assertFalse(free.data['change']['requires_conflict_approval'])

    def test_pending_move_reserves_target_for_other_task_proposals(self):
        from tasks.models import Task

        response = self.move()
        self.assertEqual(response.status_code, 201)
        other = Task.objects.create(goal=self.goal, title='Another exercise', estimated_minutes=60,
                                    completion_criteria='Done', readiness='ready')
        proposed = self.client.post(f'/api/tasks/{other.pk}/sessions/propose/', {
            'time_min': (self.start + timedelta(hours=2)).isoformat(),
            'time_max': (self.start + timedelta(hours=5)).isoformat()}, format='json')
        self.assertEqual(proposed.status_code, 201, proposed.data)
        self.assertEqual(proposed.data['starts_at'], (self.start + timedelta(hours=3)).isoformat().replace('+00:00', 'Z'))

    def test_request_validation_expiry_and_live_lease(self):
        from .models import SessionChange

        self.assertEqual(self.client.post(f'/api/sessions/{self.session.pk}/reschedule/', [], format='json').status_code, 400)
        response = self.move()
        change = SessionChange.objects.get(pk=response.data['change']['id'])
        change.expires_at = timezone.now() - timedelta(seconds=1)
        change.save()
        self.assertEqual(self.approve_change(response).status_code, 409)
        change.lease_until = timezone.now() + timedelta(minutes=1)
        change.save()
        self.assertEqual(self.move().status_code, 409)
        self.assertEqual(self.approve_change(response).status_code, 409)
        self.assertFalse(self.writes)

    def test_delete_transport_accepts_empty_204_body(self):
        import httpx

        with patch('calendars.services.httpx.request', return_value=httpx.Response(204)):
            self.assertEqual(self.real_request_json('DELETE', 'https://example.invalid/event'), (204, {}))

    def test_external_edit_after_uncertain_move_is_not_overwritten_and_allows_explicit_cancel(self):
        response = self.move()
        self.timeout_after_write = True
        self.assertEqual(self.approve_change(response).status_code, 502)
        self.event['etag'] = '"external-later"'
        self.event['start'] = {'dateTime': (self.start + timedelta(hours=4)).isoformat()}
        self.event['end'] = {'dateTime': (self.start + timedelta(hours=5)).isoformat()}
        result = self.approve_change(response)
        self.assertEqual(result.status_code, 409, result.data)
        self.assertEqual(result.data['change']['status'], 'conflicted')
        self.assertEqual(len(self.writes), 1)
        cancellation = self.cancel()
        self.assertEqual(cancellation.status_code, 201, cancellation.data)
        self.assertEqual(self.approve_change(cancellation).status_code, 200)
        self.assertEqual([method for method, _ in self.writes], ['PATCH', 'DELETE'])

    def test_destination_listing_paginates_and_handles_empty_collections(self):
        calls = []
        original = self.calendar_api
        def pages(user, method, path, **kwargs):
            if method == 'GET' and path == 'calendars/target/events':
                calls.append(kwargs['params'].get('pageToken'))
                if not kwargs['params'].get('pageToken'):
                    return {'items': [], 'nextPageToken': 'second', 'timeZone': 'UTC'}
                return {'timeZone': 'UTC'}
            return original(user, method, path, **kwargs)
        self.api.side_effect = pages
        response = self.move()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(calls, [None, 'second'])

    def test_session_account_original_calendar_is_used_when_default_destination_changes(self):
        CalendarConnection.objects.filter(user=self.user).update(destination_calendar='a-new-default')
        response = self.cancel()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.approve_change(response).status_code, 200)
        self.assertEqual(len(self.writes), 1)

    def test_expired_intent_reconciles_already_completed_remote_move(self):
        from .models import SessionChange

        response = self.move()
        self.timeout_after_write = True
        self.assertEqual(self.approve_change(response).status_code, 502)
        SessionChange.objects.filter(pk=response.data['change']['id']).update(expires_at=timezone.now() - timedelta(days=1))
        self.assertEqual(self.approve_change(response).status_code, 200)
        self.assertEqual(len(self.writes), 1)

    def test_scheduling_change_just_before_patch_keeps_proposal_rejectable(self):
        from . import management
        from .models import ProtectedPeriod, SessionChange

        response = self.move()
        original = management.check_slot
        calls = []
        def protect(user, session, change):
            calls.append(True)
            if len(calls) == 2:
                ProtectedPeriod.objects.create(user=user, kind='one_off', starts_at=change.starts_at, ends_at=change.ends_at)
            return original(user, session, change)
        with patch('calendars.management.check_slot', side_effect=protect):
            result = self.approve_change(response)
        self.assertEqual(result.status_code, 409, result.data)
        self.assertFalse(self.writes)
        self.assertEqual(SessionChange.objects.get(pk=response.data['change']['id']).status, 'proposed')
        self.assertEqual(self.client.post(f"/api/sessions/changes/{response.data['change']['id']}/reject/").status_code, 200)

    def test_local_save_failure_after_remote_move_is_recoverable(self):
        from .models import SessionChange

        response = self.move()
        with patch('calendars.management.finalize', side_effect=RuntimeError('database failure')):
            with self.assertRaises(RuntimeError):
                self.approve_change(response)
        self.assertEqual(SessionChange.objects.get(pk=response.data['change']['id']).status, 'applying')
        self.session.refresh_from_db()
        self.assertEqual(self.session.starts_at, self.start)
        self.assertEqual(self.approve_change(response).status_code, 200)
        self.assertEqual(len(self.writes), 1)


    def test_reschedule_overlap_requires_explicit_consent_and_preserves_the_conflicting_event(self):
        left = self.start + timedelta(hours=2)
        other = {'id': 'unrelated', 'summary': 'Existing appointment', 'start': {'dateTime': left.isoformat()},
                 'end': {'dateTime': (left + timedelta(hours=1)).isoformat()}}
        self.events = [other]
        response = self.move(preferred_start=left.isoformat())
        self.assertEqual(response.status_code, 201, response.data)
        change = response.data['change']
        self.assertTrue(change['requires_conflict_approval'])
        self.assertEqual(self.approve_change(response).status_code, 409)
        result = self.approve_change(response, allow_overlap=True, conflict_token=change['conflict_token'])
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(self.events, [other])
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.writes[0][0], 'PATCH')
        self.assertTrue(result.data['session']['overlap_approved'])
        self.assertEqual(len(result.data['session']['overlap_history']), 1)

    def test_find_another_reschedule_avoids_the_reviewed_occupancy(self):
        left = self.start + timedelta(hours=2)
        self.events = [{'id': 'unrelated', 'start': {'dateTime': left.isoformat()},
                        'end': {'dateTime': (left + timedelta(hours=1)).isoformat()}}]
        occupied = self.move(preferred_start=left.isoformat())
        self.assertTrue(occupied.data['change']['requires_conflict_approval'])
        alternative = self.move(conflict_policy='avoid')
        self.assertEqual(alternative.status_code, 201, alternative.data)
        self.assertFalse(alternative.data['change']['requires_conflict_approval'])
        self.assertEqual(alternative.data['change']['starts_at'], (left + timedelta(hours=1)).isoformat().replace('+00:00', 'Z'))

    def test_uncertain_overlapping_reschedule_reconciles_saved_approval(self):
        left = self.start + timedelta(hours=2)
        self.other_busy = [{'start': left.isoformat(), 'end': (left + timedelta(hours=1)).isoformat()}]
        response = self.move(preferred_start=left.isoformat())
        self.timeout_after_write = True
        failed = self.approve_change(response, allow_overlap=True, conflict_token=response.data['change']['conflict_token'])
        self.assertEqual(failed.status_code, 502, failed.data)
        reconciled = self.approve_change(response)
        self.assertEqual(reconciled.status_code, 200, reconciled.data)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(len(reconciled.data['session']['overlap_history']), 1)


class OverlapSchedulingTests(APITestCase):
    def setUp(self):
        from goals.models import Plan, Goal
        from tasks.models import Task
        from .models import SchedulingPreferences

        cache.clear()
        self.addCleanup(cache.clear)
        self.user = get_user_model().objects.create_user(email='overlap@example.com', google_sub='subject')
        self.client.force_authenticate(self.user)
        self.plan = Plan.objects.create(user=self.user, title='Study', raw_input='Study')
        self.goal = Goal.objects.create(user=self.user, plan=self.plan, title='Arrays')
        self.task = Task.objects.create(goal=self.goal, title='Array exercise', readiness='ready', estimated_minutes=60,
                                        completion_criteria='Two solutions written.')
        self.start = (timezone.now() + timedelta(days=10)).replace(hour=9, minute=0, second=0, microsecond=0)
        self.end = self.start + timedelta(hours=4)
        SchedulingPreferences.objects.create(user=self.user, timezone='UTC', working_hours=[
            {'weekday': day, 'start_time': '09:00', 'end_time': '17:00'} for day in range(7)])
        CalendarConnection.objects.create(user=self.user, google_sub='subject', email=self.user.email, credentials='unused',
            expires_at=self.end, source_calendars=['source'], destination_calendar='target')
        self.busy = {'source': [], 'target': []}
        self.events = {}
        self.writes = []
        patch('calendars.services.access_token', return_value='token').start()
        patch('calendars.services.list_calendars', return_value=[{'id': 'target', 'accessRole': 'owner'}]).start()
        self.api = patch('calendars.services.api', side_effect=self.freebusy).start()
        patch('calendars.services.request_json', side_effect=self.provider).start()
        self.addCleanup(patch.stopall)

    def freebusy(self, user, method, path, **kwargs):
        return {'calendars': {item['id']: {'busy': self.busy[item['id']]} for item in kwargs['json']['items']}}

    def provider(self, method, url, **kwargs):
        import copy

        if method == 'GET':
            event = self.events.get(url.rsplit('/', 1)[-1])
            return (200, copy.deepcopy(event)) if event else (404, {})
        self.assertEqual(method, 'POST')
        event = copy.deepcopy(kwargs['json'])
        if event['id'] in self.events:
            return 409, {}
        self.events[event['id']] = event
        self.writes.append(event)
        return 201, event

    def occupy(self, left=None, right=None, calendar='source'):
        self.busy[calendar] = [{'start': (left or self.start).isoformat(), 'end': (right or self.end).isoformat()}]

    def propose(self, **fields):
        return self.client.post(f'/api/tasks/{self.task.pk}/sessions/propose/', {
            'time_min': self.start.isoformat(), 'time_max': self.end.isoformat(), **fields}, format='json')

    def approve(self, response, **fields):
        return self.client.post(f"/api/sessions/{response.data['id']}/approve/", fields, format='json')

    def test_busy_only_range_returns_review_then_explicit_overlap_preserves_other_events(self):
        self.occupy()
        response = self.propose()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data['requires_conflict_approval'])
        self.assertEqual(response.data['conflicts'][0]['calendar_id'], 'source')
        self.assertTrue(response.data['conflict_token'])
        self.assertFalse(self.writes)
        self.assertEqual(self.approve(response).status_code, 409)
        self.assertEqual(self.approve(response, allow_overlap=True, conflict_token='wrong').status_code, 409)
        approved = self.approve(response, allow_overlap=True, conflict_token=response.data['conflict_token'])
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertTrue(approved.data['overlap_approved'])
        self.assertEqual(len(approved.data['overlap_history']), 1)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(len(self.busy['source']), 1)
        self.assertEqual(self.approve(response).status_code, 200)
        self.assertEqual(len(self.writes), 1)

    def test_free_slots_are_preferred_but_explicit_preferred_start_can_be_occupied(self):
        self.occupy(right=self.start + timedelta(hours=1))
        free = self.propose()
        self.assertEqual(free.status_code, 201)
        self.assertFalse(free.data['conflicts'])
        self.assertEqual(free.data['starts_at'], (self.start + timedelta(hours=1)).isoformat().replace('+00:00', 'Z'))
        occupied = self.propose(preferred_start=self.start.isoformat())
        self.assertEqual(occupied.status_code, 201, occupied.data)
        self.assertTrue(occupied.data['requires_conflict_approval'])
        self.assertEqual(occupied.data['starts_at'], self.start.isoformat().replace('+00:00', 'Z'))

    def test_find_another_slot_avoids_conflicts_and_keeps_prior_proposal_if_no_free_time(self):
        from .models import WorkSession

        self.occupy()
        occupied = self.propose()
        alternative = self.propose(conflict_policy='avoid')
        self.assertEqual(alternative.status_code, 409)
        self.assertEqual(WorkSession.objects.get(pk=occupied.data['id']).status, 'proposed')
        self.busy['source'][0]['end'] = (self.start + timedelta(hours=1)).isoformat()
        alternative = self.propose(conflict_policy='avoid')
        self.assertEqual(alternative.status_code, 201)
        self.assertFalse(alternative.data['requires_conflict_approval'])
        self.assertEqual(WorkSession.objects.get(pk=occupied.data['id']).status, 'rejected')

    def test_grey_period_cannot_be_overridden_at_proposal_or_approval(self):
        from .models import ProtectedPeriod

        ProtectedPeriod.objects.create(user=self.user, kind='one_off', starts_at=self.start, ends_at=self.start + timedelta(hours=1))
        self.occupy()
        response = self.propose(preferred_start=self.start.isoformat())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['starts_at'], (self.start + timedelta(hours=1)).isoformat().replace('+00:00', 'Z'))
        ProtectedPeriod.objects.create(user=self.user, kind='one_off', starts_at=self.start + timedelta(hours=1), ends_at=self.end)
        result = self.approve(response, allow_overlap=True, conflict_token=response.data['conflict_token'])
        self.assertEqual(result.status_code, 409, result.data)
        self.assertFalse(self.writes)
        self.assertEqual(self.propose().status_code, 409)

    def test_changed_or_new_conflict_requires_fresh_review(self):
        self.occupy()
        response = self.propose()
        self.occupy(calendar='target')
        result = self.approve(response, allow_overlap=True, conflict_token=response.data['conflict_token'])
        self.assertEqual(result.status_code, 409)
        self.assertFalse(self.writes)
        replacement = self.propose()
        self.assertNotEqual(replacement.data['conflict_token'], response.data['conflict_token'])
        self.assertEqual(self.approve(replacement, allow_overlap=True, conflict_token=replacement.data['conflict_token']).status_code, 200)

    def test_generic_approval_cannot_override_a_new_conflict_on_originally_free_proposal(self):
        response = self.propose()
        self.occupy()
        self.assertEqual(self.approve(response, allow_overlap=True).status_code, 409)
        self.assertFalse(self.writes)

    def test_conflicts_are_rechecked_immediately_before_insert(self):
        from . import sessions

        self.occupy()
        response = self.propose()
        actual = sessions.event_request
        def change_busy(user, session, method, payload=None):
            self.occupy(calendar='target')
            return actual(user, session, method, payload)
        with patch('calendars.sessions.event_request', side_effect=change_busy):
            result = self.approve(response, allow_overlap=True, conflict_token=response.data['conflict_token'])
        self.assertEqual(result.status_code, 409, result.data)
        self.assertFalse(self.writes)
        from .models import WorkSession
        saved = WorkSession.objects.get(pk=response.data['id'])
        self.assertEqual(saved.status, 'proposed')
        self.assertFalse(saved.overlap_approved)

    def test_uncertain_overlap_publish_uses_saved_consent_on_retry(self):
        from . import sessions

        self.occupy()
        response = self.propose()
        actual = sessions.event_request
        def timeout(user, session, method, payload=None):
            result = actual(user, session, method, payload)
            if method == 'POST':
                raise services.CalendarError('Timeout after insert')
            return result
        with patch('calendars.sessions.event_request', side_effect=timeout):
            result = self.approve(response, allow_overlap=True, conflict_token=response.data['conflict_token'])
        self.assertEqual(result.status_code, 502)
        self.assertEqual(self.approve(response).status_code, 200)
        self.assertEqual(len(self.writes), 1)

    def test_published_yiyara_event_is_overridable_but_tentative_hold_is_not(self):
        import uuid
        from .models import WorkSession
        from tasks.models import Task

        other = Task.objects.create(goal=self.goal, title='Existing task')
        session = WorkSession.objects.create(user=self.user, task=other, status='published', starts_at=self.start, ends_at=self.end,
            expires_at=self.end, google_sub='subject', google_event_id=uuid.uuid4().hex, destination_calendar='old-calendar')
        response = self.propose()
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data['requires_conflict_approval'])
        self.assertEqual(response.data['conflicts'][0]['calendar_id'], 'old-calendar')
        session.status = 'publishing'
        session.save()
        self.assertEqual(self.propose().status_code, 409)
        self.assertFalse(self.writes)

    def test_conflict_data_is_merged_and_clipped_to_the_reviewed_slot(self):
        self.busy['source'] = [
            {'start': self.start.isoformat(), 'end': (self.start + timedelta(minutes=45)).isoformat()},
            {'start': (self.start + timedelta(minutes=30)).isoformat(), 'end': self.end.isoformat()}]
        response = self.propose()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data['conflicts']), 1)
        self.assertEqual(response.data['conflicts'][0]['end'], (self.start + timedelta(hours=1)).isoformat())

    def test_preferred_start_requires_explicit_offset_minute_alignment_and_in_range(self):
        for value in ['2026-11-01T09:00:00', (self.start - timedelta(hours=1)).isoformat(),
                      (self.start + timedelta(seconds=10)).isoformat(), (self.end - timedelta(minutes=1)).isoformat()]:
            with self.subTest(value=value):
                self.assertEqual(self.propose(preferred_start=value).status_code, 400)


class ContinuationSessionTests(APITestCase):
    setUp = TaskSessionTests.setUp
    freebusy = TaskSessionTests.freebusy
    propose = TaskSessionTests.propose
    session = TaskSessionTests.session
    approve = TaskSessionTests.approve

    def published(self):
        session = self.session()
        with patch('calendars.sessions.event_request', return_value=(201, session.event_payload)):
            self.assertEqual(self.approve(session).status_code, 200)
        session.refresh_from_db()
        return session

    def body(self, **changes):
        import uuid
        self.plan.refresh_from_db()
        return {'client_id': str(uuid.uuid4()), 'plan_version': self.plan.version,
                'status': 'partial', 'remaining_minutes': 20, 'spent_minutes': 40, **changes}

    def finish(self, session, body=None):
        return self.client.post(f'/api/sessions/{session.pk}/finish/', body or self.body(), format='json')

    def test_partial_finish_continuation_approval_and_history(self):
        from .models import WorkSession
        first = self.published()
        payload, event_id = first.event_payload.copy(), first.google_event_id
        from goals.replanning import protected_task_ids
        self.assertIn(str(self.task.pk), protected_task_ids(self.plan))
        with patch('calendars.services.request_json') as remote:
            result = self.finish(first)
        self.assertEqual(result.status_code, 201, result.data)
        remote.assert_not_called()
        first.refresh_from_db()
        self.assertIsNotNone(first.finished_at)
        self.assertNotIn(str(self.task.pk), protected_task_ids(self.plan))
        self.assertEqual(first.status, 'published')
        self.assertEqual(first.event_payload, payload)
        self.assertEqual(first.google_event_id, event_id)
        self.assertFalse(result.data['task']['is_completed'])
        response = self.propose()
        self.assertEqual(response.status_code, 201, response.data)
        second = WorkSession.objects.get(pk=response.data['id'])
        self.assertEqual(second.continuation_of, first)
        self.assertEqual(str(second.source_outcome_id), result.data['outcome']['id'])
        self.assertEqual(second.ends_at - second.starts_at, timedelta(minutes=20))
        self.assertNotEqual(second.google_event_id, first.google_event_id)
        self.assertGreaterEqual(second.starts_at, first.ends_at)
        with patch('calendars.sessions.event_request', return_value=(201, second.event_payload)) as remote:
            self.assertEqual(self.approve(second).status_code, 200)
            self.assertEqual(self.approve(second).status_code, 200)
            self.assertEqual(remote.call_count, 1)
        self.assertEqual(self.propose().status_code, 409)
        history = self.client.get(f'/api/tasks/{self.task.pk}/sessions/')
        self.assertEqual(len(history.data), 2)
        self.assertEqual(self.finish(second, self.body(status='completed', remaining_minutes=0)).status_code, 201)
        self.assertEqual(self.propose().status_code, 400)

    def test_exact_retry_does_not_rewind_newer_progress_or_finish_twice(self):
        from tasks.models import TaskOutcome
        session = self.published()
        body = self.body()
        self.assertEqual(self.finish(session, body).status_code, 201)
        self.assertEqual(self.finish(session, body).status_code, 200)
        self.assertEqual(TaskOutcome.objects.count(), 1)
        changed = {**body, 'remaining_minutes': 15}
        self.assertEqual(self.finish(session, changed).status_code, 409)
        self.assertEqual(self.finish(session).status_code, 409)
        newer = self.body(remaining_minutes=10)
        self.assertEqual(self.client.post(f'/api/tasks/{self.task.pk}/outcomes/', newer, format='json').status_code, 201)
        replay = self.finish(session, body)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.data['task']['remaining_minutes'], 10)
        self.assertEqual(TaskOutcome.objects.count(), 2)

    def test_progress_report_alone_does_not_finish_session(self):
        session = self.published()
        response = self.client.post(f'/api/tasks/{self.task.pk}/outcomes/', self.body(session_id=str(session.pk)), format='json')
        self.assertEqual(response.status_code, 201)
        session.refresh_from_db()
        self.assertIsNone(session.finished_at)
        self.assertEqual(self.propose().status_code, 409)

    def test_invalid_unconfirmed_and_foreign_sessions_do_not_record_progress(self):
        from tasks.models import TaskOutcome
        session = self.session()
        self.assertEqual(self.finish(session).status_code, 409)
        session.status = 'publishing'
        session.save()
        self.assertEqual(self.finish(session).status_code, 409)
        session.status = 'published'
        session.save()
        self.assertEqual(self.finish(session, self.body(plan_version=999)).status_code, 409)
        self.assertEqual(self.finish(session, self.body(remaining_minutes=0)).status_code, 400)
        self.assertEqual(self.finish(session, self.body(session_id=str(session.pk))).status_code, 400)
        self.assertEqual(self.finish(session, self.body(status='reopened')).status_code, 400)
        other = get_user_model().objects.create_user(email='foreign@example.com')
        self.client.force_authenticate(other)
        self.assertEqual(self.finish(session).status_code, 404)
        self.assertEqual(self.client.get(f'/api/tasks/{self.task.pk}/sessions/').status_code, 404)
        self.assertFalse(TaskOutcome.objects.exists())

    def test_finished_session_cannot_move_but_task_can_be_edited(self):
        import uuid
        session = self.published()
        self.assertEqual(self.finish(session).status_code, 201)
        with patch('calendars.management.remote') as remote:
            result = self.client.post(f'/api/sessions/{session.pk}/reschedule/', {
                'client_id': str(uuid.uuid4()), 'time_min': self.start.isoformat(), 'time_max': self.end.isoformat(),
            }, format='json')
        self.assertEqual(result.status_code, 409)
        remote.assert_not_called()
        edited = self.client.patch(f'/api/tasks/{self.task.pk}/', {'title': 'Finish remaining exercise'}, format='json')
        self.assertEqual(edited.status_code, 200, edited.data)
        session.refresh_from_db()
        self.assertNotEqual(session.task_snapshot['title'], edited.data['title'])

    def test_blocked_or_skipped_finish_needs_readiness_before_continuing(self):
        session = self.published()
        result = self.finish(session, self.body(status='blocked', blocker_reason='Waiting for feedback'))
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(self.propose().status_code, 400)
        response = self.client.post(f'/api/tasks/{self.task.pk}/outcomes/', self.body(status='reopened'), format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.propose().status_code, 201)

    def test_finish_rejects_pending_changes_and_blocks_uncertain_changes(self):
        import uuid
        from .models import SessionChange
        from tasks.models import TaskOutcome
        session = self.published()
        change = SessionChange.objects.create(session=session, user=self.user, client_id=uuid.uuid4(),
            kind='cancel', status='applying', base_version=0, expires_at=self.start)
        self.assertEqual(self.finish(session).status_code, 409)
        self.assertFalse(TaskOutcome.objects.exists())
        session.refresh_from_db()
        self.assertIsNone(session.finished_at)
        change.status = 'proposed'
        change.save()
        self.assertEqual(self.finish(session).status_code, 201)
        change.refresh_from_db()
        self.assertEqual(change.status, 'rejected')

    def test_continuation_stale_progress_and_restored_active_identity_are_guarded(self):
        from .models import WorkSession
        first = self.published()
        self.assertEqual(self.finish(first).status_code, 201)
        response = self.propose()
        second = WorkSession.objects.get(pk=response.data['id'])
        changed = self.client.post(f'/api/tasks/{self.task.pk}/outcomes/', self.body(remaining_minutes=10), format='json')
        self.assertEqual(changed.status_code, 201)
        with patch('calendars.sessions.event_request') as remote:
            self.assertEqual(self.approve(second).status_code, 409)
        remote.assert_not_called()
        response = self.propose()
        second = WorkSession.objects.get(pk=response.data['id'])
        with patch('calendars.sessions.event_request', return_value=(201, second.event_payload)):
            self.assertEqual(self.approve(second).status_code, 200)
        # Revision restore can sever the FK; stable snapshot identity must still block duplicates.
        second.refresh_from_db()
        second.task = None
        second.save()
        self.assertEqual(self.propose().status_code, 409)
        history = self.client.get(f'/api/tasks/{self.task.pk}/sessions/')
        self.assertEqual(len(history.data), 3)

    def test_database_allows_history_but_only_one_unfinished_commitment(self):
        import uuid
        from django.db import IntegrityError, transaction
        from .models import WorkSession
        first = self.published()
        fields = {field: getattr(first, field) for field in (
            'user', 'task', 'status', 'starts_at', 'ends_at', 'expires_at', 'destination_calendar',
            'google_sub', 'task_snapshot', 'event_payload')}
        with self.assertRaises(IntegrityError), transaction.atomic():
            WorkSession.objects.create(**fields, google_event_id=uuid.uuid4().hex)
        self.assertEqual(self.finish(first).status_code, 201)
        WorkSession.objects.create(**fields, google_event_id=uuid.uuid4().hex)
        self.assertEqual(WorkSession.objects.filter(status='published').count(), 2)

    def test_finished_history_survives_task_deletion_and_is_user_scoped(self):
        from .models import WorkSession
        session = self.published()
        self.assertEqual(self.finish(session).status_code, 201)
        task_id = self.task.pk
        self.task.delete()
        history = self.client.get(f'/api/tasks/{task_id}/sessions/')
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.data[0]['id'], str(session.pk))
        self.assertIsNone(history.data[0]['task_id'])
        self.assertTrue(WorkSession.objects.get(pk=session.pk).finished_at)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(f'/api/tasks/{task_id}/sessions/').status_code, 401)


    def test_finish_rolls_back_report_if_session_save_fails(self):
        from tasks.models import TaskOutcome
        session = self.published()
        self.plan.refresh_from_db()
        original_version = self.plan.version
        with patch('calendars.models.WorkSession.save', side_effect=RuntimeError('Simulated local save failure')):
            with self.assertRaises(RuntimeError):
                self.finish(session)
        session.refresh_from_db()
        self.task.refresh_from_db()
        self.plan.refresh_from_db()
        self.assertIsNone(session.finished_at)
        self.assertEqual(self.task.progress_version, 0)
        self.assertEqual(self.plan.version, original_version)
        self.assertFalse(TaskOutcome.objects.exists())


class FinishedSessionManagementTests(APITestCase):
    setUp = SessionManagementTests.setUp
    calendar_api = SessionManagementTests.calendar_api
    provider = SessionManagementTests.provider
    cancel = SessionManagementTests.cancel
    approve_change = SessionManagementTests.approve_change

    def test_explicit_cancellation_preserves_finished_outcome_and_retry(self):
        import uuid
        body = {'client_id': str(uuid.uuid4()), 'plan_version': self.plan.version,
                'status': 'partial', 'remaining_minutes': 20}
        url = f'/api/sessions/{self.session.pk}/finish/'
        result = self.client.post(url, body, format='json')
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(self.writes, [])
        cancellation = self.cancel()
        self.assertEqual(cancellation.status_code, 201, cancellation.data)
        self.assertEqual(self.approve_change(cancellation).status_code, 200)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'cancelled')
        self.assertIsNotNone(self.session.finished_at)
        replay = self.client.post(url, body, format='json')
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data['task']['remaining_minutes'], 20)
        self.assertEqual(len(self.writes), 1)
