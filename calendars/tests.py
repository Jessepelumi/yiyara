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
