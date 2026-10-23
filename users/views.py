from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from rest_framework import permissions, status
from rest_framework.exceptions import AuthenticationFailed, APIException
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView
from rest_framework_simplejwt.authentication import JWTAuthentication

from .serializers import (
    GoogleAuthSerializer,
    UserSerializer,
    LogoutSerializer,
    ActiveUserTokenRefreshSerializer,
)

User = get_user_model()


class AccountLinkingRequired(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = (
        "An account already exists with this email. "
        "Account linking is required."
    )
    default_code = "account_linking_required"


class IdentityConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = (
        "Google identity does not match the linked account."
    )
    default_code = "identity_conflict"


def verify_google_token(token):
    # Verify a Google ID token before trusting its claims.
    allowed_audiences = settings.GOOGLE_CLIENT_IDS

    if not allowed_audiences:
        raise APIException(
            "Google authentication is not configured."
        )

    try:
        claims = google_id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            audience=None,
        )
    except (ValueError, GoogleAuthError):
        raise AuthenticationFailed(
            "Invalid or expired Google ID token."
        )

    if claims.get("aud") not in allowed_audiences:
        raise AuthenticationFailed(
            "Invalid Google token audience."
        )

    if not claims.get("sub"):
        raise AuthenticationFailed(
            "Google identity is missing."
        )

    if claims.get("email_verified") is not True:
        raise AuthenticationFailed(
            "Google email is not verified."
        )

    email = claims.get("email")

    if not isinstance(email, str) or not email.strip():
        raise AuthenticationFailed(
            "Google email is missing."
        )

    return claims


def resolve_google_user(claims):
    # Resolve an existing linked user or create a new one.
    google_sub = claims["sub"]
    email = claims["email"].strip().lower()

    with transaction.atomic():
        user = User.objects.select_for_update().filter(
            google_sub=google_sub
        ).first()

        if user is not None:
            if not user.is_active:
                raise AuthenticationFailed(
                    "User account is inactive."
                )

            if user.email.lower() != email:
                raise IdentityConflict()

            return user

        existing_user = User.objects.select_for_update().filter(
            email=email
        ).first()

        if existing_user is not None:
            raise AccountLinkingRequired()

        user = User.objects.create_user(
            email=email,
            google_sub=google_sub,
            first_name=claims.get("given_name", ""),
            last_name=claims.get("family_name", ""),
            is_email_verified=True,
        )

        return user


class GoogleAuthView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_google"

    def post(self, request):
        serializer = GoogleAuthSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        claims = verify_google_token(
            serializer.validated_data["id_token"]
        )

        try:
            user = resolve_google_user(claims)
        except IntegrityError:
            # A concurrent request may have created the user.
            # Resolve the committed record and recheck identity.
            google_sub = claims["sub"]
            email = claims["email"].strip().lower()

            user = User.objects.filter(
                google_sub=google_sub,
                email=email,
            ).first()

            if user is None:
                raise IdentityConflict()

            if not user.is_active:
                raise AuthenticationFailed(
                    "User account is inactive."
                )

        refresh = RefreshToken.for_user(user)

        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": UserSerializer(user).data,
            },
            status=status.HTTP_200_OK,
        )


class RefreshView(TokenRefreshView):
    serializer_class = ActiveUserTokenRefreshSerializer
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_refresh"


class LogoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [JWTAuthentication]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_logout"

    def post(self, request):
        serializer = LogoutSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)

        token = serializer.context["refresh_token"]

        try:
            token.blacklist()
        except TokenError:
            raise AuthenticationFailed(
                "Invalid refresh token."
            )

        return Response(
            status=status.HTTP_204_NO_CONTENT
        )


class CurrentUserView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        serializer = UserSerializer(request.user)

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )
    