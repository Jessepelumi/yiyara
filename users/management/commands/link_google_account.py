from getpass import getpass

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from rest_framework.exceptions import APIException

from users.views import verify_google_token


class Command(BaseCommand):
    help = "Link an existing account to a verified Google identity (administrator only)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Existing account to link.")

    def handle(self, *args, **options):
        email = options["email"].strip().lower()
        # Never accept tokens in command arguments or print them to logs.
        try:
            token = getpass("Google ID token (hidden): ")
        except EOFError as exc:
            raise CommandError("Run this command in an interactive terminal.") from exc
        if not token.strip():
            raise CommandError("A Google ID token is required.")
        try:
            claims = verify_google_token(token)
        except APIException as exc:
            raise CommandError(str(exc.detail)) from exc
        if claims["email"].strip().lower() != email:
            raise CommandError("Google identity does not match the specified account email.")

        User = get_user_model()
        try:
            with transaction.atomic():
                user = User.objects.select_for_update().filter(email=email).first()
                if user is None:
                    raise CommandError("Existing account not found; no account was created.")
                if not user.is_active:
                    raise CommandError("Cannot link an inactive account.")
                if user.google_sub and user.google_sub != claims["sub"]:
                    raise CommandError("Account is already linked to a different Google identity.")
                if User.objects.filter(google_sub=claims["sub"]).exclude(pk=user.pk).exists():
                    raise CommandError("Google identity is already linked to another account.")
                user.google_sub = claims["sub"]
                user.is_email_verified = True
                user.save(update_fields=["google_sub", "is_email_verified"])
        except IntegrityError as exc:
            raise CommandError("Identity conflict; no account link was changed.") from exc

        self.stdout.write(self.style.SUCCESS("Google identity linked. Retry Google login in Postman."))
