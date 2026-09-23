from typing import ClassVar

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.tokens import default_token_generator
from django.utils.decorators import method_decorator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.authentication import BaseAuthentication
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.settings import api_settings as simplejwt_settings
from rest_framework_simplejwt.tokens import RefreshToken

from momcare_platform.core.common.jwt_auth import issue_tokens_for
from momcare_platform.core.common.mail import send_application_received, send_email_otp, send_password_reset
from momcare_platform.core.common.rls import bypass_rls
from momcare_platform.core.users.api.serializers import (
    ForgotPasswordSerializer,
    PasswordChangeSerializer,
    PatientRegisterSerializer,
    RegisterSerializer,
    ResetPasswordSerializer,
    UserMeSerializer,
    VerifyResetTokenSerializer,
)
from momcare_platform.core.users.models import User

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
            f"Contact {settings.SUPPORT_EMAIL} if you believe this is a mistake."
        ),
        Organization.STATUS_SUSPENDED: (
            f"Your hospital's access has been suspended. Please contact {settings.SUPPORT_EMAIL}."
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

    authentication_classes: ClassVar[list[type[BaseAuthentication]]] = []
    permission_classes = [AllowAny]

    def post(self, request):
        # No organization exists yet for this request to belong to — the
        # uniqueness checks in is_valid() and the insert in save() both need
        # to see across every hospital, which is what this request is for.
        with bypass_rls():
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
    """Email + password → JWT access token (refresh in HttpOnly cookie).

    Email is deliberately the *only* accepted identifier. A phone number was
    tried and removed: it has no single global format, so the same number
    reaches us as "+923001234567", "0300 1234567" or "03001234567" depending
    on who typed it, and an exact-match lookup authenticates only the one
    spelling used at signup. Matching them properly means normalising every
    number to E.164, which needs a country to interpret a bare national
    number against — a guess this platform has no good way to make for a
    hospital it has never seen. Email has one worldwide format and no such
    ambiguity.
    """

    authentication_classes: ClassVar[list[type[BaseAuthentication]]] = []
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get("email", "").lower().strip()
        password = request.data.get("password", "")

        if not email or not password:
            return Response(
                {"detail": "Email and password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Nobody's hospital is known until a credential match says who they
        # are — this lookup is inherently cross-tenant, the same way finding
        # anyone by email always is before their identity is established.
        #
        # authenticate() rather than a manual lookup: Django's ModelBackend
        # already hashes the password against a dummy when no user matches,
        # so an unknown address costs the same time as a wrong password and
        # cannot be used to enumerate who has an account here.
        with bypass_rls():
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

        # Patient-only: self-registration is the one signup path where
        # nothing else ever confirmed she owns this address (contrast staff,
        # invited to a specific address by an admin; hospital owners,
        # verified by a human reviewer). Scoped to ROLE_PATIENT deliberately
        # — is_email_verified is never set for any other role, so gating
        # every login on it here would lock out every hospital account that
        # has ever existed.
        if user.role_code == settings.ROLE_PATIENT and not user.is_email_verified:
            return Response(
                {"detail": "Confirm your email before signing in.", "requires_email_verification": True},
                status=status.HTTP_403_FORBIDDEN,
            )

        gate = _tenant_access_error(user)
        if gate is not None:
            return Response(gate, status=status.HTTP_403_FORBIDDEN)

        refresh = issue_tokens_for(user)
        response = Response(
            {
                "access": str(refresh.access_token),
                "user": UserMeSerializer(user).data,
            }
        )
        _set_refresh_cookie(response, str(refresh))
        return response


class RefreshView(APIView):
    """Read refresh token from HttpOnly cookie and issue a new access token.

    Does not use SimpleJWT's stock ``TokenRefreshSerializer`` — that copies
    whatever claims the old refresh token already carried onto the new
    access token, including ``org_id``. That would let a staff member moved
    to a different hospital keep acting as their old one for the refresh
    token's whole 7-day life, not just the access token's 1 hour. Re-reading
    the user's *current* organization here instead means a reassignment
    self-heals at the very next refresh.
    """

    authentication_classes: ClassVar[list[type[BaseAuthentication]]] = []
    permission_classes = [AllowAny]

    def post(self, request):
        raw = request.COOKIES.get(REFRESH_COOKIE)
        if not raw:
            return Response(
                {"detail": "Refresh token cookie missing."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            old_refresh = RefreshToken(raw)
        except TokenError as exc:
            raise InvalidToken(exc.args[0]) from exc

        user_id = old_refresh[simplejwt_settings.USER_ID_CLAIM]
        # Same reasoning as login: which hospital this token's owner belongs
        # to right now is exactly what this lookup exists to find out.
        with bypass_rls():
            user = User.objects.filter(pk=user_id, is_active=True).first()
        if user is None:
            raise InvalidToken("User no longer exists or is inactive.")

        # SIMPLE_JWT above hardcodes rotation on, with no env override — so
        # unlike the rest of this file, there is no reusing-the-same-refresh-
        # token path here. Handling one only speculatively, with no way to
        # actually exercise it, is exactly the kind of untested branch worth
        # not carrying.
        try:
            old_refresh.blacklist()
        except AttributeError:
            pass
        new_refresh = issue_tokens_for(user)
        response = Response({"access": str(new_refresh.access_token)})
        _set_refresh_cookie(response, str(new_refresh))
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


def _revoke_outstanding_refresh_tokens(user) -> None:
    """Sign the account out everywhere.

    A password is changed for two reasons: it was weak, or somebody else has it.
    The second is the one that matters, and leaving existing refresh tokens
    valid would mean the intruder keeps their session for up to a week while the
    owner believes they have just locked the door.

    Best-effort: the blacklist app must be installed for this to do anything,
    and a failure here must not stop the password change itself, which is the
    part the user asked for.
    """
    try:
        from rest_framework_simplejwt.token_blacklist.models import (  # noqa: PLC0415
            OutstandingToken,
        )
    except ImportError:  # pragma: no cover - the app is installed in this project
        return

    for outstanding in OutstandingToken.objects.filter(user=user):
        try:
            # simplejwt's own stubs type this parameter as Token | None, but
            # passing the raw token string is its documented usage.
            RefreshToken(outstanding.token).blacklist()  # type: ignore[arg-type]
        except TokenError:
            # Already blacklisted, or expired. Either way there is nothing to revoke.
            continue


@method_decorator(csrf_exempt, name="dispatch")
class PasswordChangeView(APIView):
    """Change your own password while signed in."""

    permission_classes = [IsAuthenticated]
    throttle_scope = "auth_sensitive"

    def post(self, request):
        serializer = PasswordChangeSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        user = request.user
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])

        _revoke_outstanding_refresh_tokens(user)

        response = Response(
            {"detail": "Your password has been changed. Please sign in again."},
            status=status.HTTP_200_OK,
        )
        # The refresh cookie in this browser is now blacklisted, so clear it
        # rather than leaving a credential behind that can only fail.
        response.delete_cookie(REFRESH_COOKIE, **_cookie_attrs())
        return response


@method_decorator(csrf_exempt, name="dispatch")
class ForgotPasswordView(APIView):
    """Ask for a reset link by email.

    Always answers the same way, whether or not the address belongs to an
    account. Anything else turns this endpoint into a way to discover who works
    at which hospital — and for a clinical system that list is worth having.
    """

    permission_classes = [AllowAny]
    authentication_classes: ClassVar[list[type[BaseAuthentication]]] = []
    throttle_scope = "auth_sensitive"

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        # Whose account this is — and so which hospital — is the very thing
        # this lookup exists to discover, same as login.
        with bypass_rls():
            user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is not None:
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            send_password_reset(
                user,
                f"{settings.FRONTEND_URL.rstrip('/')}/reset-password/{uid}/{token}",
            )

        return Response(
            {
                "detail": (
                    "If that address belongs to a MomCare account, a reset link is "
                    "on its way. Check your spam folder if it does not arrive."
                ),
            },
            status=status.HTTP_200_OK,
        )


@method_decorator(csrf_exempt, name="dispatch")
class ResetPasswordView(APIView):
    """Set a new password using the emailed link."""

    permission_classes = [AllowAny]
    authentication_classes: ClassVar[list[type[BaseAuthentication]]] = []
    throttle_scope = "auth_sensitive"

    def post(self, request):
        # The serializer's own validate() resolves the user from the emailed
        # uid before anyone's hospital is knowable, same as login — and the
        # write below is to that same not-yet-scoped row, so it stays in the
        # same bypass rather than falling out of context between the two.
        with bypass_rls():
            serializer = ResetPasswordSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            user = serializer.validated_data["user"]
            user.set_password(serializer.validated_data["new_password"])
            # Clears the not-yet-activated flag an invited staff account is
            # created with. This endpoint is where a staff invitation is
            # completed as well as where a forgotten password is reset — both
            # end with the person having chosen a password themselves, which
            # is exactly what the flag tracks.
            user.requires_password_reset = False
            user.save(update_fields=["password", "requires_password_reset"])

        # Whoever forced the reset may already hold a session. Changing the
        # password invalidates the token that produced this link, but not the
        # refresh tokens issued before it.
        _revoke_outstanding_refresh_tokens(user)

        return Response(
            {"detail": "Your password has been set. You can now sign in."},
            status=status.HTTP_200_OK,
        )


@method_decorator(csrf_exempt, name="dispatch")
class VerifyResetTokenView(APIView):
    """Lets the frontend check a reset link is still valid before showing the
    new-password form, instead of only finding out after the visitor fills it
    in and submits. Purely read-only — never changes the password, never
    revokes sessions. ResetPasswordView remains the only endpoint that
    actually resets anything, and re-validates the token itself regardless of
    what this one said (the link could still expire in the gap between the
    two calls) — this is a UX improvement layered on top, not a second source
    of truth.
    """

    permission_classes = [AllowAny]
    authentication_classes: ClassVar[list[type[BaseAuthentication]]] = []
    throttle_scope = "auth_sensitive"

    def post(self, request):
        with bypass_rls():
            serializer = VerifyResetTokenSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
        return Response({"valid": True}, status=status.HTTP_200_OK)


@method_decorator(csrf_exempt, name="dispatch")
class PatientRegisterView(APIView):
    """A woman signing herself up from the mobile app.

    Creates an account and nothing else — no tokens yet. She has no hospital,
    so there is no Patient record either; that is created only when a
    hospital approves her join request, through the same onboarding a
    walk-in goes through.

    No tokens are issued here on purpose: nothing yet proves she owns the
    email address she typed in. ``PatientRegisterSerializer.create()`` sends
    a one-time code to it (``EmailVerificationCode``); she must confirm it
    at ``VerifyPatientEmailView`` before signing in at all — see that view
    and ``LoginView``'s own docstring for the gate on the other end.

    Open to anyone, and throttled like the other credential endpoints: this
    one creates accounts.
    """

    authentication_classes: ClassVar[list[type[BaseAuthentication]]] = []
    permission_classes = [AllowAny]
    throttle_scope = "auth_sensitive"

    def post(self, request):
        serializer = PatientRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        return Response(
            {
                "detail": "Almost done — enter the code we emailed you to finish creating your account.",
                "email": user.email,
            },
            status=status.HTTP_201_CREATED,
        )


class VerifyPatientEmailView(APIView):
    """The second half of patient self-registration: confirm the code sent
    to her email, and only now receive real sign-in tokens.

    Identified by email + code, not a session — she has no token yet, this
    is what produces her first one. Deliberately the same generic failure
    message for "wrong code", "expired code", "too many attempts" and
    "no such pending account": which one it was is not information worth
    handing to whoever is submitting codes, the same reasoning ``LoginView``
    already applies to a wrong password vs. an unknown email.
    """

    authentication_classes: ClassVar[list[type[BaseAuthentication]]] = []
    permission_classes = [AllowAny]
    throttle_scope = "auth_sensitive"

    def post(self, request):
        from momcare_platform.core.users.models import EmailVerificationCode

        email = (request.data.get("email") or "").strip().lower()
        code = (request.data.get("code") or "").strip()
        if not email or not code:
            return Response(
                {"detail": "Email and code are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with bypass_rls():
            user = User.objects.filter(
                email__iexact=email,
                is_email_verified=False,
                role__code=settings.ROLE_PATIENT,
            ).first()
            valid = user is not None and EmailVerificationCode.verify(user, code)

            if not valid:
                return Response(
                    {"detail": "That code is invalid, expired, or has been used too many times."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            user.is_email_verified = True
            user.save(update_fields=["is_email_verified", "updated_at"])

        refresh = issue_tokens_for(user)
        response = Response(
            {
                "detail": "Email confirmed. Choose a hospital to request care from.",
                "access": str(refresh.access_token),
                "user": UserMeSerializer(user).data,
            },
        )
        _set_refresh_cookie(response, str(refresh))
        return response


class ResendPatientVerificationView(APIView):
    """A fresh code, if the first one expired or never arrived.

    Always answers the same way regardless of whether the email matches a
    real pending account — the same enumeration concern as ``ForgotPassword
    View``: confirming "yes, someone started signing up with this address"
    is itself information a stranger should not get from this endpoint.
    """

    authentication_classes: ClassVar[list[type[BaseAuthentication]]] = []
    permission_classes = [AllowAny]
    throttle_scope = "auth_sensitive"

    def post(self, request):
        from momcare_platform.core.users.models import EmailVerificationCode

        email = (request.data.get("email") or "").strip().lower()
        if not email:
            return Response({"detail": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)

        with bypass_rls():
            user = User.objects.filter(
                email__iexact=email,
                is_email_verified=False,
                role__code=settings.ROLE_PATIENT,
            ).first()
            if user is not None:
                _, code = EmailVerificationCode.issue(user)
                send_email_otp(user, code)

        return Response({"detail": "If that email has a pending signup, a new code has been sent."})
