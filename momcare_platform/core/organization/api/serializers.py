from rest_framework import serializers

from momcare_platform.core.organization.models import Organization


class OrganizationSummarySerializer(serializers.ModelSerializer):
    """The signed-in user's own hospital — identity, review state, and live counts.

    Counts come from the model's properties rather than stored columns, so they
    can never drift from the underlying rows.
    """

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    license_authority_display = serializers.CharField(
        source="get_license_authority_display",
        read_only=True,
    )
    owner_name = serializers.CharField(source="owner.full_name", read_only=True, default="")
    staff_count = serializers.IntegerField(read_only=True)
    patient_count = serializers.IntegerField(read_only=True)
    location_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Organization
        fields = [
            "id",
            "name",
            "status",
            "status_display",
            "reviewed_at",
            "email",
            "phone",
            "license_no",
            "license_authority",
            "license_authority_display",
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
            "owner_name",
            "staff_count",
            "patient_count",
            "location_count",
            "created_at",
        ]
        read_only_fields = fields
