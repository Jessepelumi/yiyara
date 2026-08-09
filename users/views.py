import secrets

from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import get_user_model

User = get_user_model()

class InternalAuthView(APIView):
    """
    Exchanges a NextAuth verified email for a Django JWT
    """
    
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        internal_secret = request.headers.get("X-Internal-Secret")
        expected_secret = settings.INTERNAL_AUTH_SECRET
        if not expected_secret:
            return Response(
                {"error": "Internal authentication is not configured"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if not internal_secret or not secrets.compare_digest(internal_secret, expected_secret):
            return Response({"error": "Unauthorized internal request"}, status=status.HTTP_401_UNAUTHORIZED)
        
        email = request.data.get("email")
        if not email:
            return Response({"error": "Email required"}, status=status.HTTP_400_BAD_REQUEST)
        
        # Normalize email
        email = email.lower()

        # Get or create the user
        user, created = User.objects.get_or_create(
            email = email,
            defaults={
                'first_name': request.data.get("first_name", ""),
                'last_name': request.data.get("last_name", ""),
                'is_email_verified': True, # verified by NextAuth
            }
        )

        # Verify user who exists but wasn't verified
        if not created and not user.is_email_verified:
            user.is_email_verified = True
            user.save()

        # Generate tokens
        refresh = RefreshToken.for_user(user)

        return Response({
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": {
                "id": user.id,
                "email": user.email,
                "username": user.username
            }
        })
