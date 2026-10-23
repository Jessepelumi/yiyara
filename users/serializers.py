from django.contrib.auth import get_user_model
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()

class GoogleAuthSerializer(serializers.Serializer):
    id_token = serializers.CharField(
        required=True,
        allow_blank=False,
        trim_whitespace=False,
        write_only=True,
    )


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "username",
            "first_name",
            "last_name",
            "is_email_verified",
        ]
        read_only_fields = fields


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField(
        required=True,
        allow_blank=False,
        trim_whitespace=False,
        write_only=True,
    )

    def validate_refresh(self, value):
        try:
            token = RefreshToken(value)
        except TokenError:
            raise AuthenticationFailed(
                "Invalid or expired refresh token."
            )

        request = self.context["request"]

        token_user_id = str(token["user_id"])
        current_user_id = str(request.user.pk)

        if token_user_id != current_user_id:
            raise AuthenticationFailed(
                "Refresh token does not belong to this user."
            )

        self.context["refresh_token"] = token

        return value


class ActiveUserTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        try:
            token = RefreshToken(attrs["refresh"])
            user_id = token["user_id"]
        except (TokenError, KeyError):
            raise AuthenticationFailed(
                "Invalid or expired refresh token."
            )

        user = User.objects.filter(
            pk=user_id,
            is_active=True,
        ).first()

        if user is None:
            raise AuthenticationFailed(
                "Invalid or inactive user."
            )

        return super().validate(attrs)
        