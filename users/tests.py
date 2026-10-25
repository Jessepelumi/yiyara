from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from rest_framework import status
from rest_framework.test import APITestCase

User = get_user_model()


@override_settings(
    GOOGLE_CLIENT_IDS=(
        "test-client.apps.googleusercontent.com",
    )
)
class AuthenticationTests(APITestCase):

    google_url = "/api/users/auth/google/"
    refresh_url = "/api/users/auth/refresh/"
    logout_url = "/api/users/auth/logout/"
    me_url = "/api/users/me/"

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.claims = {
            "sub": "google-user-123",
            "email": "test@gmail.com",
            "email_verified": True,
            "aud": "test-client.apps.googleusercontent.com",
            "given_name": "Test",
            "family_name": "User",
        }

    def google_login(self, claims=None):
        if claims is None:
            claims = self.claims

        with patch(
            "users.views.google_id_token.verify_oauth2_token",
            return_value=claims,
        ):
            return self.client.post(
                self.google_url,
                {"id_token": "mock-google-token"},
                format="json",
            )

    def authenticate(self, access):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {access}"
        )

    def test_new_google_user_is_created(self):
        response = self.google_login()

        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

        user = User.objects.get(email="test@gmail.com")

        self.assertEqual(
            user.google_sub,
            "google-user-123",
        )
        self.assertTrue(user.is_email_verified)
        self.assertFalse(user.has_usable_password())

    def test_returning_google_user(self):
        self.google_login()
        response = self.google_login()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.count(), 1)

    def test_missing_token(self):
        response = self.client.post(
            self.google_url,
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_invalid_google_token(self):
        with patch(
            "users.views.google_id_token.verify_oauth2_token",
            side_effect=ValueError("Invalid token"),
        ):
            response = self.client.post(
                self.google_url,
                {"id_token": "invalid"},
                format="json",
            )

        self.assertEqual(response.status_code, 401)

    def test_expired_google_token(self):
        with patch(
            "users.views.google_id_token.verify_oauth2_token",
            side_effect=ValueError("Token expired"),
        ):
            response = self.client.post(
                self.google_url,
                {"id_token": "expired"},
                format="json",
            )

        self.assertEqual(response.status_code, 401)

    def test_wrong_audience(self):
        claims = {
            **self.claims,
            "aud": "untrusted-client",
        }

        response = self.google_login(claims)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(User.objects.count(), 0)

    def test_unverified_email(self):
        claims = {
            **self.claims,
            "email_verified": False,
        }

        response = self.google_login(claims)

        self.assertEqual(response.status_code, 401)

    def test_missing_google_subject(self):
        claims = {
            **self.claims,
            "sub": "",
        }

        response = self.google_login(claims)

        self.assertEqual(response.status_code, 401)

    def test_inactive_user_cannot_login(self):
        User.objects.create_user(
            email="test@gmail.com",
            google_sub="google-user-123",
            is_active=False,
        )

        response = self.google_login()

        self.assertEqual(response.status_code, 401)

    def test_existing_email_requires_linking(self):
        User.objects.create_user(
            email="test@gmail.com"
        )

        response = self.google_login()

        self.assertEqual(response.status_code, 409)

    def test_google_identity_email_conflict(self):
        User.objects.create_user(
            email="different@gmail.com",
            google_sub="google-user-123",
        )

        response = self.google_login()

        self.assertEqual(response.status_code, 409)

    def test_me_requires_authentication(self):
        response = self.client.get(self.me_url)

        self.assertEqual(response.status_code, 401)

    def test_me_returns_current_user(self):
        login = self.google_login()

        self.authenticate(login.data["access"])

        response = self.client.get(self.me_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["email"],
            "test@gmail.com",
        )
        self.assertNotIn("google_sub", response.data)

    def test_refresh_rotation(self):
        login = self.google_login()
        original_refresh = login.data["refresh"]

        response = self.client.post(
            self.refresh_url,
            {"refresh": original_refresh},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertNotEqual(
            original_refresh,
            response.data["refresh"],
        )

        replay = self.client.post(
            self.refresh_url,
            {"refresh": original_refresh},
            format="json",
        )

        self.assertEqual(replay.status_code, 401)

    def test_inactive_user_cannot_refresh(self):
        login = self.google_login()

        user = User.objects.get(email="test@gmail.com")
        user.is_active = False
        user.save(update_fields=["is_active"])

        response = self.client.post(
            self.refresh_url,
            {"refresh": login.data["refresh"]},
            format="json",
        )

        self.assertEqual(response.status_code, 401)

    def test_logout_revokes_refresh(self):
        login = self.google_login()

        self.authenticate(login.data["access"])

        logout = self.client.post(
            self.logout_url,
            {"refresh": login.data["refresh"]},
            format="json",
        )

        self.assertEqual(logout.status_code, 204)

        refresh = self.client.post(
            self.refresh_url,
            {"refresh": login.data["refresh"]},
            format="json",
        )

        self.assertEqual(refresh.status_code, 401)

    def test_logout_requires_authentication(self):
        login = self.google_login()

        response = self.client.post(
            self.logout_url,
            {"refresh": login.data["refresh"]},
            format="json",
        )

        self.assertEqual(response.status_code, 401)

    def test_cannot_logout_another_users_token(self):
        first_login = self.google_login()

        second_claims = {
            **self.claims,
            "sub": "google-user-456",
            "email": "second@gmail.com",
        }

        second_login = self.google_login(second_claims)

        self.authenticate(first_login.data["access"])

        response = self.client.post(
            self.logout_url,
            {"refresh": second_login.data["refresh"]},
            format="json",
        )

        self.assertEqual(response.status_code, 401)

        # Second user's refresh token must still work.
        refresh = self.client.post(
            self.refresh_url,
            {"refresh": second_login.data["refresh"]},
            format="json",
        )

        self.assertEqual(refresh.status_code, 200)

    def test_old_auth_bridge_is_removed(self):
        response = self.client.post(
            "/api/users/auth/bridge/",
            {
                "email": "test@gmail.com",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 404)


@override_settings(GOOGLE_CLIENT_IDS=("test-client",))
class AccountLinkingCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="owner@gmail.com")
        self.claims = {
            "sub": "verified-subject",
            "email": "owner@gmail.com",
            "email_verified": True,
            "aud": "test-client",
        }

    def link(self, claims=None):
        with patch("users.management.commands.link_google_account.getpass", return_value="secret-token"), patch(
            "users.views.google_id_token.verify_oauth2_token",
            return_value=claims or self.claims,
        ):
            output = StringIO()
            call_command("link_google_account", email=self.user.email, stdout=output)
            self.assertNotIn("secret-token", output.getvalue())

    def test_links_in_place_and_is_repeatable(self):
        from goals.models import Plan

        plan = Plan.objects.create(user=self.user, title="Existing ambition", raw_input="Learn DSA")
        self.link()
        self.link()
        self.user.refresh_from_db()
        self.assertEqual(self.user.google_sub, self.claims["sub"])
        self.assertTrue(self.user.is_email_verified)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(plan.user_id, self.user.pk)
        self.assertTrue(Plan.objects.filter(pk=plan.pk, user=self.user).exists())

    def test_rejects_wrong_email_unverified_and_wrong_audience(self):
        for changes in ({"email": "other@gmail.com"}, {"email_verified": False}, {"aud": "other-client"}):
            with self.subTest(changes=changes), self.assertRaises(CommandError):
                self.link({**self.claims, **changes})
        self.user.refresh_from_db()
        self.assertIsNone(self.user.google_sub)

    def test_rejects_inactive_account(self):
        self.user.is_active = False
        self.user.save()
        with self.assertRaises(CommandError):
            self.link()

    def test_does_not_replace_existing_identity(self):
        self.user.google_sub = "original-subject"
        self.user.save()
        with self.assertRaises(CommandError):
            self.link()
        self.user.refresh_from_db()
        self.assertEqual(self.user.google_sub, "original-subject")

    def test_rejects_identity_owned_by_another_account(self):
        User.objects.create_user(email="other@gmail.com", google_sub=self.claims["sub"])
        with self.assertRaises(CommandError):
            self.link()
        self.user.refresh_from_db()
        self.assertIsNone(self.user.google_sub)

    def test_rejects_missing_account(self):
        self.user.delete()
        with self.assertRaises(CommandError):
            self.link()
        self.assertEqual(User.objects.count(), 0)

    def test_rejects_invalid_token(self):
        with patch("users.management.commands.link_google_account.getpass", return_value="invalid"), patch(
            "users.views.google_id_token.verify_oauth2_token", side_effect=ValueError("invalid")
        ), self.assertRaises(CommandError):
            call_command("link_google_account", email="owner@gmail.com", stdout=StringIO())
