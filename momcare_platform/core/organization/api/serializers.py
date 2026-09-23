from rest_framework import serializers
from timezone_field.rest_framework import TimeZoneSerializerField

from momcare_platform.core.organization.models import (
    AuditLog,
    Notification,
    Organization,
    OrganizationDeactivationRequest,
)


class OrganizationSerializer(serializers.ModelSerializer):
    """The signed-in user's own hospital — identity, review state, live counts,
    and (for a hospital_admin) an editable profile.

    Read: any authenticated user in the hospital. Write: hospital_admin only
    (enforced in the view), and only the fields listed as writable below —
    everything the platform's own review decision rested on (``status``,
    ``reviewed_at``) or that the model derives itself (``region``, the
    counts) stays read-only here. ``license_image`` is also read-only for
    now — its upload path is deliberately deferred, not wired into this
    endpoint yet.
    """

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    owner_name = serializers.CharField(source="owner.full_name", read_only=True, default="")
    staff_count = serializers.IntegerField(read_only=True)
    patient_count = serializers.IntegerField(read_only=True)
    location_count = serializers.IntegerField(read_only=True)
    # Validates the zone name and returns a 400 on bad input, never a 500.
    timezone = TimeZoneSerializerField(required=False)
    # The hospital's own override, and the number actually used once the
    # platform default is folded in — see Organization.effective_confidence_threshold.
    # Both travel together so a caller never has to guess which one a null
    # confidence_threshold falls back to. Read-only here: it gets its own
    # narrow endpoint, OrganizationConfidenceThresholdView.
    effective_confidence_threshold = serializers.DecimalField(
        max_digits=4,
        decimal_places=3,
        read_only=True,
    )

    class Meta:
        model = Organization
        fields = [
            "id",
            "name",
            "license_number",
            "license_image",
            "status",
            "status_display",
            "reviewed_at",
            "email",
            "phone",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "country",
            # Derived from country, never stored — the model region this
            # hospital's patients belong to, or null when the model has no
            # training data for that population.
            "region",
            "region_display",
            "timezone",
            "date_format",
            "established_date",
            "owner_name",
            "staff_count",
            "patient_count",
            "location_count",
            "confidence_threshold",
            "effective_confidence_threshold",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "license_image",
            "status",
            "status_display",
            "reviewed_at",
            "region",
            "region_display",
            "owner_name",
            "staff_count",
            "patient_count",
            "location_count",
            "confidence_threshold",
            "effective_confidence_threshold",
            "created_at",
        ]


class OrganizationDeactivationRequestSerializer(serializers.ModelSerializer):
    """Read shape for a hospital's own deactivation request — what it looked
    like when made, and (once reviewed) what a platform admin decided."""

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    requested_by_name = serializers.CharField(
        source="requested_by.get_full_name",
        read_only=True,
        default="",
    )
    reviewed_by_name = serializers.CharField(
        source="reviewed_by.get_full_name",
        read_only=True,
        default="",
    )

    class Meta:
        model = OrganizationDeactivationRequest
        fields = [
            "id",
            "reason",
            "status",
            "status_display",
            "requested_by_name",
            "reviewed_by_name",
            "review_note",
            "reviewed_at",
            "created_at",
        ]
        read_only_fields = fields


class OrganizationDeactivationRequestCreateSerializer(serializers.Serializer):
    """Asking to close the account. ``reason`` is optional free text, the
    same looseness as every other reviewer-facing note in this codebase
    (``Organization.review_note`` etc.) — a platform admin can always follow
    up if none is given."""

    reason = serializers.CharField(required=False, allow_blank=True, default="")


class OrganizationConfidenceThresholdSerializer(serializers.ModelSerializer):
    """A hospital's own override of the model confidence threshold.

    A separate, single-purpose serializer/endpoint rather than folded into
    the general profile update — this is a clinical-safety setting, not a
    profile detail, and deserves its own narrow write path.
    ``confidence_threshold`` already allows null on the model (see its own
    field docstring): sending ``{"confidence_threshold": null}`` clears this
    hospital's override and reverts it to following the platform default live.
    """

    class Meta:
        model = Organization
        fields = ["confidence_threshold"]


class AuditLogSerializer(serializers.ModelSerializer):
    """One PHI-access record — read-only everywhere; nothing about this
    trail is ever written through the API. See AuditLogMiddleware for the
    single place a row is ever created."""

    user_email = serializers.EmailField(source="user.email", read_only=True, default="")
    user_name = serializers.CharField(source="user.get_full_name", read_only=True, default="")
    action_display = serializers.CharField(source="get_action_display", read_only=True)

    class Meta:
        model = AuditLog
        fields = [
            "id",
            "user_email",
            "user_name",
            "action",
            "action_display",
            "resource",
            "resource_id",
            "ip_address",
            "endpoint",
            "timestamp",
        ]
        read_only_fields = fields


class NotificationSerializer(serializers.ModelSerializer):
    """One "something needs your attention" row -- read-only everywhere;
    the only write this API exposes is the dedicated mark-read action, not
    a general PATCH."""

    notification_type_display = serializers.CharField(source="get_notification_type_display", read_only=True)

    class Meta:
        model = Notification
        fields = [
            "id",
            "notification_type",
            "notification_type_display",
            "message",
            "related_object_id",
            "is_read",
            "read_at",
            "created_at",
        ]
        read_only_fields = fields
