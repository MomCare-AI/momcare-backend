"""JWT authentication that already knows the tenant when it matters.

Row Level Security needs ``app.current_org_id`` set before the very first
protected-table query of a request — which, for an authenticated API call,
is the query that resolves *who* the token belongs to. Stock SimpleJWT looks
the user up first and would only learn their organization afterwards, which
is exactly backwards for RLS: that lookup itself would come back empty.

The fix: the hospital is already known the moment the token was issued, so
it travels *in* the token as a claim, covered by the same signature check
that already protects the user id. Setting the RLS context from that claim
before ``get_user()`` runs means the identity lookup — and everything after
it in the same request, since ``ATOMIC_REQUESTS`` keeps one request in one
transaction — is already correctly scoped.

A platform admin has no organization (``organization_id`` is null on their
own row), so no claim value could ever satisfy a tenant-scoped policy for
them — their identity lookup is bypassed instead, same reasoning as login.
They are not expected to drive this API day-to-day (see MyOrganizationView);
Django admin is their surface, and it carries its own bypass already
(``AdminRLSBypassMiddleware``).
"""

from __future__ import annotations

from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.tokens import RefreshToken

from momcare_platform.core.common.rls import bypass_rls, set_current_organization

ORG_CLAIM = "org_id"


class TenantAwareJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        org_id = validated_token.get(ORG_CLAIM)
        if org_id:
            set_current_organization(org_id)
            return super().get_user(validated_token)
        with bypass_rls():
            return super().get_user(validated_token)


def issue_tokens_for(user) -> RefreshToken:
    """The one place a refresh/access pair is minted, so the org claim is
    never something a caller remembers to add — it is simply part of issuing
    a token at all."""
    refresh = RefreshToken.for_user(user)
    if user.organization_id:
        refresh[ORG_CLAIM] = str(user.organization_id)
    return refresh
