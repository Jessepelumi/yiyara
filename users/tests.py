from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

User = get_user_model()


class InternalAuthViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    @override_settings(INTERNAL_AUTH_SECRET=None)
    def test_bridge_is_closed_when_secret_is_not_configured(self):
        response = self.client.post(
            "/api/users/auth/bridge/",
            {"email": "owner@example.com"},
            format="json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertFalse(User.objects.exists())

    @override_settings(INTERNAL_AUTH_SECRET="expected-secret")
    def test_bridge_rejects_missing_or_wrong_secret(self):
        missing = self.client.post(
            "/api/users/auth/bridge/",
            {"email": "owner@example.com"},
            format="json",
        )
        wrong = self.client.post(
            "/api/users/auth/bridge/",
            {"email": "owner@example.com"},
            format="json",
            HTTP_X_INTERNAL_SECRET="wrong-secret",
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertFalse(User.objects.exists())

    @override_settings(INTERNAL_AUTH_SECRET="expected-secret")
    def test_bridge_creates_verified_user_and_returns_jwt(self):
        response = self.client.post(
            "/api/users/auth/bridge/",
            {"email": "Owner@Example.com", "first_name": "Owner"},
            format="json",
            HTTP_X_INTERNAL_SECRET="expected-secret",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        user = User.objects.get(email="owner@example.com")
        self.assertTrue(user.is_email_verified)
