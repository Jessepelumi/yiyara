from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

User = get_user_model()


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
