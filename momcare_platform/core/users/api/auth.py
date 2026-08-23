from django.conf import settings
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from momcare_platform.core.common.mail import send_application_received
from momcare_platform.core.users.api.serializers import RegisterSerializer, UserMeSerializer

REFRESH_COOKIE = settings.REFRESH_COOKIE_NAME
COOKIE_MAX_AGE = 7 * 24 * 60 * 60  # 7 days, matches JWT REFRESH_TOKEN_LIFETIME


def _cookie_attrs() -> dict:
    """Attributes that must match exactly between setting and deleting the cookie.

    These come from settings rather than being written here, because production
    needs to change them without a code edit. Once the API is served from a
    subdomain of the site — api.example.com against example.com — the cookie
    needs Domain=.example.com or the browser will not send it on the refresh
    call, and the user is silently signed out an hour after logging in.

    A mismatch between set and delete is the other half of the same trap: a
    cookie set with a Domain is not removed by a delete without one, so logout
    would appear to succeed and leave the session alive.
    """
    return {
        "domain": settings.REFRESH_COOKIE_DOMAIN or None,
        "path": settings.REFRESH_COOKIE_PATH,
        "samesite": settings.REFRESH_COOKIE_SAMESITE,
    }


def _tenant_access_error(user) -> dict | None:
    """Block sign-in while the user's hospital is not cleared for access.

    Returns an error payload, or None when the user may proceed. Platform
    admins have no organization and are never gated — they are created by
    ``createsuperuser``, not by self-registration, and they do the reviewing.
    """
    from momcare_platform.core.organization.models import Organization  # noqa: PLC0415

    org = user.organization
    if org is None:
        return None

    messages = {
        Organization.STATUS_PENDING: (
            "Your hospital's application is still under review. "
            "You'll be able to sign in once a platform admin approves it."
        ),
        Organization.STATUS_REJECTED: (
            "Your hospital's application was not approved. "
            "Contact support@momcare.pk if you believe this is a mistake."
        ),
        Organization.STATUS_SUSPENDED: (
            "Your hospital's access has been suspended. Please contact support@momcare.pk."
        ),
    }

    if org.status in messages:
        return {"detail": messages[org.status], "org_status": org.status}
    if not org.is_active:
        return {"detail": "Your hospital's account is inactive.", "org_status": org.status}
    return None


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        # Secure is settable so it can be switched off for plain-HTTP local work,
        # but it defaults to on: a refresh token sent over HTTP is readable in transit.
        secure=settings.REFRESH_COOKIE_SECURE and not settings.DEBUG,
        **_cookie_attrs(),
    )


@method_decorator(csrf_exempt, name="dispatch")
class RegisterView(APIView):
    """Create a hospital-admin user + their organization in one atomic request.

    Deliberately issues no tokens. The organization starts PENDING and a
    platform admin must verify the hospital against its provincial regulator
    before anyone in that tenant can authenticate — registration here is an
    application, not a sign-up.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        # Best-effort: the application is already saved, so a mail failure must
        # not turn a successful registration into an error.
        send_application_received(user, user.organization)

        return Response(
            {
                "detail": "Application received. A platform admin will review your hospital.",
                "status": user.organization.status,
                "organization_name": user.organization.name,
                "email": user.email,
            },
            status=status.HTTP_201_CREATED,
        )


@method_decorator(csrf_exempt, name="dispatch")
class LoginView(APIView):
    """Email + password → JWT access token (refresh in HttpOnly cookie)."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        from django.contrib.auth import authenticate

        email = request.data.get("email", "").lower().strip()
        password = request.data.get("password", "")

        if not email or not password:
            return Response(
                {"detail": "Email and password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = authenticate(request, username=email, password=password)
        if user is None:
            return Response(
                {"detail": "Invalid credentials."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.is_active:
            return Response(
                {"detail": "Account is inactive."},
                status=status.HTTP_403_FORBIDDEN,
            )

        gate = _tenant_access_error(user)
        if gate is not None:
            return Response(gate, status=status.HTTP_403_FORBIDDEN)

        refresh = RefreshToken.for_user(user)
        response = Response(
            {
                "access": str(refresh.access_token),
                "user": UserMeSerializer(user).data,
            }
        )
        _set_refresh_cookie(response, str(refresh))
        return response


class RefreshView(APIView):
    """Read refresh token from HttpOnly cookie and issue a new access token."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        from rest_framework_simplejwt.serializers import TokenRefreshSerializer

        raw = request.COOKIES.get(REFRESH_COOKIE)
        if not raw:
            return Response(
                {"detail": "Refresh token cookie missing."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = TokenRefreshSerializer(data={"refresh": raw})
        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as exc:
            raise InvalidToken(exc.args[0]) from exc

        data = serializer.validated_data
        response = Response({"access": data["access"]})
        if "refresh" in data:
            _set_refresh_cookie(response, data["refresh"])
        return response


class LogoutView(APIView):
    """Blacklist the refresh token and clear the cookie."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        raw = request.COOKIES.get(REFRESH_COOKIE)
        if raw:
            try:
                RefreshToken(raw).blacklist()
            except TokenError:
                pass  # Already invalid — still clear the cookie

        response = Response({"detail": "Logged out."}, status=status.HTTP_200_OK)
        response.delete_cookie(REFRESH_COOKIE, **_cookie_attrs())
        return response


class MeView(APIView):
    """Return the authenticated user's profile."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserMeSerializer(request.user).data)
