import base64
import hashlib
import json
import secrets
from datetime import timedelta
from functools import partial
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.id_token import verify_oauth2_token
from rest_framework.exceptions import APIException, ValidationError

from .models import CalendarAuthorization, CalendarConnection

PREFIX = "https://www.googleapis.com/auth/"
CALENDAR_SCOPES = [PREFIX + s for s in (
    "calendar.calendarlist.readonly", "calendar.events.freebusy", "calendar.events.owned"
)]
SCOPES = ["openid", "email", *CALENDAR_SCOPES]
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_URL = "https://www.googleapis.com/calendar/v3/"


class CalendarError(APIException):
    status_code = 502
    default_detail = "Google Calendar is temporarily unavailable. Try again."


class ReconnectRequired(APIException):
    status_code = 409
    default_detail = "Reconnect Google Calendar to continue."


def cipher():
    try:
        return Fernet(settings.GOOGLE_CALENDAR_ENCRYPTION_KEY.encode())
    except (ValueError, TypeError, AttributeError):
        error = APIException("Calendar credential encryption is not configured.")
        error.status_code = 503
        raise error


def encrypt(payload):
    return cipher().encrypt(json.dumps(payload).encode()).decode()


def decrypt(value):
    try:
        return json.loads(cipher().decrypt(value.encode()))
    except (InvalidToken, ValueError):
        raise CalendarError("Stored Calendar credentials cannot be decrypted.") from None


def configuration():
    if not all((settings.GOOGLE_CALENDAR_CLIENT_ID, settings.GOOGLE_CALENDAR_CLIENT_SECRET,
                settings.GOOGLE_CALENDAR_REDIRECT_URI)):
        error = APIException("Google Calendar is not configured.")
        error.status_code = 503
        raise error
    cipher()


def request_json(method, url, **kwargs):
    try:
        response = httpx.request(method, url, timeout=20, **kwargs)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError()
        return response.status_code, payload
    except (httpx.HTTPError, ValueError):
        raise CalendarError() from None


def lock_user(user):
    return get_user_model().objects.select_for_update().get(pk=user.pk)


def start(user):
    configuration()
    if not user.google_sub:
        raise ValidationError("Link a Google identity to your account first.")
    state, verifier, nonce = (secrets.token_urlsafe(48) for _ in range(3))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    with transaction.atomic():
        lock_user(user)
        CalendarAuthorization.objects.filter(user=user).delete()
        CalendarAuthorization.objects.create(
            user=user, state_hash=hashlib.sha256(state.encode()).hexdigest(),
            verifier=encrypt({"verifier": verifier, "nonce": nonce}),
            expires_at=timezone.now() + timedelta(minutes=10),
        )
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": settings.GOOGLE_CALENDAR_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_CALENDAR_REDIRECT_URI,
        "response_type": "code", "scope": " ".join(SCOPES), "state": state,
        "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256",
        "access_type": "offline", "prompt": "consent", "login_hint": user.email,
    })


def token_request(data):
    configuration()
    return request_json("POST", TOKEN_URL, data={
        "client_id": settings.GOOGLE_CALENDAR_CLIENT_ID,
        "client_secret": settings.GOOGLE_CALENDAR_CLIENT_SECRET, **data,
    })


def finish(params):
    state = params.get("state", "")
    if not state or len(state) > 256:
        raise ValidationError("Missing or invalid authorization state.")
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    attempt = CalendarAuthorization.objects.select_related("user").filter(state_hash=state_hash).first()
    if not attempt:
        raise ValidationError("Invalid or expired authorization state.")
    # Claim once, including failed or declined exchanges. A new attempt is needed on failure.
    claimed = CalendarAuthorization.objects.filter(
        pk=attempt.pk, used=False, expires_at__gt=timezone.now()
    ).update(used=True)
    if not claimed:
        raise ValidationError("Invalid, expired or already used authorization state.")
    with transaction.atomic():
        user = lock_user(attempt.user)
        if not user.is_active or not CalendarAuthorization.objects.filter(pk=attempt.pk).exists():
            raise ValidationError("Authorization cancelled. Start again.")
        if params.get("error"):
            raise ValidationError("Calendar authorization was declined or cancelled.")
        if not params.get("code"):
            raise ValidationError("Authorization code is missing.")
        proof = decrypt(attempt.verifier)
        code, tokens = token_request({
            "grant_type": "authorization_code", "code": params["code"],
            "redirect_uri": settings.GOOGLE_CALENDAR_REDIRECT_URI,
            "code_verifier": proof["verifier"],
        })
        if code != 200:
            raise ValidationError("Google authorization failed. Start a new connection attempt.")
        try:
            claims = verify_oauth2_token(tokens.get("id_token", ""), partial(Request(), timeout=20),
                                         audience=settings.GOOGLE_CALENDAR_CLIENT_ID)
        except (ValueError, GoogleAuthError):
            raise ValidationError("Google identity verification failed.") from None
        if (claims.get("sub") != user.google_sub or claims.get("email_verified") is not True
                or claims.get("nonce") != proof["nonce"]):
            raise ValidationError("Authorize Calendar with the Google account used to sign in.")
        scopes = tokens.get("scope", "").split()
        if not set(CALENDAR_SCOPES).issubset(scopes):
            raise ValidationError("Required Calendar permissions were not granted.")
        old = CalendarConnection.objects.filter(user=user).first()
        refresh = tokens.get("refresh_token")
        if not refresh and old and old.google_sub == claims["sub"]:
            refresh = decrypt(old.credentials).get("refresh_token")
        if not refresh:
            raise ValidationError("Offline access was not granted. Reconnect and grant consent.")
        if not tokens.get("access_token"):
            raise CalendarError()
        try:
            expires = int(tokens["expires_in"])
            if not 0 < expires <= 86400:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise CalendarError() from None
        CalendarConnection.objects.update_or_create(user=user, defaults={
            "google_sub": claims["sub"], "email": user.email,
            "credentials": encrypt({"access_token": tokens["access_token"], "refresh_token": refresh}),
            "expires_at": timezone.now() + timedelta(seconds=expires),
            "scopes": scopes, "reconnect_required": False,
        })


def get_connection(user):
    connection = CalendarConnection.objects.filter(user=user).first()
    if not connection or connection.reconnect_required:
        raise ReconnectRequired()
    return connection


def access_token(user):
    # Serialize refreshes and disconnects; commit reconnect status before raising.
    invalid = False
    with transaction.atomic():
        lock_user(user)
        connection = get_connection(user)
        tokens = decrypt(connection.credentials)
        if connection.expires_at <= timezone.now() + timedelta(seconds=60):
            status, payload = token_request({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
            if status == 400 and payload.get("error") == "invalid_grant":
                connection.reconnect_required = True
                connection.save(update_fields=["reconnect_required", "updated_at"])
                invalid = True
            elif status != 200:
                raise CalendarError()
            else:
                try:
                    expires = int(payload["expires_in"])
                    token = payload["access_token"]
                    if not token or not 0 < expires <= 86400:
                        raise ValueError()
                except (KeyError, TypeError, ValueError):
                    raise CalendarError() from None
                if "scope" in payload and not set(CALENDAR_SCOPES).issubset(payload["scope"].split()):
                    connection.reconnect_required = True
                    connection.save(update_fields=["reconnect_required", "updated_at"])
                    invalid = True
                else:
                    tokens["access_token"] = token
                    if payload.get("refresh_token"):
                        tokens["refresh_token"] = payload["refresh_token"]
                    connection.credentials = encrypt(tokens)
                    connection.expires_at = timezone.now() + timedelta(seconds=expires)
                    connection.save(update_fields=["credentials", "expires_at", "updated_at"])
    if invalid:
        raise ReconnectRequired()
    return tokens["access_token"]


def api(user, method, path, **kwargs):
    token = access_token(user)
    status, payload = request_json(method, API_URL + path, headers={"Authorization": "Bearer " + token}, **kwargs)
    if status == 401:
        CalendarConnection.objects.filter(user=user).update(reconnect_required=True)
        raise ReconnectRequired()
    if status != 200:
        raise CalendarError("Calendar request failed. Check access and permissions, then retry.")
    return payload


def list_calendars(user):
    items, page = [], None
    for _ in range(100):
        params = {"maxResults": 250}
        if page:
            params["pageToken"] = page
        result = api(user, "GET", "users/me/calendarList", params=params)
        for item in result.get("items", []):
            items.append({k: item[k] for k in ("id", "summary", "timeZone", "accessRole", "primary") if k in item})
        page = result.get("nextPageToken")
        if not page:
            return items
    raise CalendarError("Too many calendars; calendar listing could not be completed.")


def disconnect(user):
    with transaction.atomic():
        lock_user(user)
        CalendarAuthorization.objects.filter(user=user).delete()
        CalendarConnection.objects.filter(user=user).delete()
