from django.core.validators import RegexValidator
from django.utils import timezone
from rest_framework import serializers

from momcare_platform.core.monitoring.models import (
    MAX_SESSION_DURATION_SECONDS,
    ClinicalTag,
    MonitoringNote,
    MonitoringSession,
    NoteTemplate,
    PatientStatus,
    StatusLabel,
)
from momcare_platform.core.monitoring.services import get_or_create_tags

HEX_COLOR_VALIDATOR = RegexValidator(
    regex=r"^#[0-9A-Fa-f]{6}$",
    message="color must be a hex code like #RRGGBB.",
)


class ClinicalTagSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClinicalTag
        fields = ["id", "name", "color", "organization", "location", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_color(self, value):
        if not value:
            return None
        HEX_COLOR_VALIDATOR(value)
        return value

    def validate(self, attrs):
        """Exactly one of organization/location, and it must be the
        caller's own hospital -- the model's CheckConstraint enforces the
        "exactly one" half at the DB level; this is what stops a hospital
        admin planting a tag under another tenant's organization/location
        by id before it ever reaches that constraint.
        """
        request = self.context.get("request")
        caller_org = getattr(getattr(request, "user", None), "organization", None) if request else None

        organization = attrs.get("organization", getattr(self.instance, "organization", None))
        location = attrs.get("location", getattr(self.instance, "location", None))

        if bool(organization) == bool(location):
            raise serializers.ValidationError("Provide exactly one of 'organization' or 'location'.")
        if organization is not None and organization != caller_org:
            raise serializers.ValidationError({"organization": "Must be your own hospital."})
        if location is not None and (caller_org is None or location.organization_id != caller_org.id):
            raise serializers.ValidationError({"location": "Must belong to your own hospital."})
        return attrs


class NoteTemplateSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True, default="")
    updated_by_name = serializers.CharField(source="updated_by.get_full_name", read_only=True, default="")

    class Meta:
        model = NoteTemplate
        fields = [
            "id",
            "title",
            "content",
            "organization",
            "location",
            "created_by",
            "created_by_name",
            "updated_by",
            "updated_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_by", "updated_by", "created_at", "updated_at"]

    def validate_title(self, value):
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError("title cannot be blank.")
        return cleaned

    def validate(self, attrs):
        """Exactly one of organization/location, and it must be the
        caller's own hospital -- same reasoning as ClinicalTagSerializer.validate above.
        """
        request = self.context.get("request")
        caller_org = getattr(getattr(request, "user", None), "organization", None) if request else None

        organization = attrs.get("organization", getattr(self.instance, "organization", None))
        location = attrs.get("location", getattr(self.instance, "location", None))

        if bool(organization) == bool(location):
            raise serializers.ValidationError("Provide exactly one of 'organization' or 'location'.")
        if organization is not None and organization != caller_org:
            raise serializers.ValidationError({"organization": "Must be your own hospital."})
        if location is not None and (caller_org is None or location.organization_id != caller_org.id):
            raise serializers.ValidationError({"location": "Must belong to your own hospital."})
        return attrs


class TagSpecSerializer(serializers.Serializer):
    """A single entry in a ``tags`` list on note creation/update.

    Two mutually exclusive modes:
      * **Attach existing** -- pass ``id`` (UUID) of an already-created
        ``ClinicalTag`` visible to the caller. ``name``/``color`` are
        ignored in this mode -- an existing, shared tag is never silently
        renamed or repainted by attaching it.
      * **Create new** -- pass ``name`` (and optionally ``color``). If a
        tag with that name already exists in scope (case-insensitive),
        it's reused and the given ``color`` is ignored (see
        ``services.get_or_create_tags``).
    """

    id = serializers.UUIDField(required=False, allow_null=True, default=None)
    name = serializers.CharField(max_length=100, required=False, allow_blank=True, default="", trim_whitespace=True)
    color = serializers.CharField(max_length=7, required=False, allow_null=True, default=None)

    def validate_color(self, value):
        if value:
            HEX_COLOR_VALIDATOR(value)
        return value

    def validate(self, attrs):
        has_id = attrs.get("id") is not None
        has_name = bool(attrs.get("name", "").strip())
        if has_id and has_name:
            raise serializers.ValidationError("Provide either 'id' (existing tag) or 'name' (new tag), not both.")
        if not has_id and not has_name:
            raise serializers.ValidationError("Either 'id' (existing tag) or 'name' (new tag) is required.")
        return attrs


class MonitoringSessionSerializer(serializers.ModelSerializer):
    patient_name = serializers.CharField(source="patient.full_name", read_only=True)
    mrn = serializers.CharField(source="patient.mrn", read_only=True, default="")
    pregnancy_id = serializers.UUIDField(source="pregnancy.id", read_only=True, default=None)
    gestational_age = serializers.CharField(source="pregnancy.gestational_age_display", read_only=True, default="")
    added_by_name = serializers.CharField(source="added_by.get_full_name", read_only=True, default="")

    class Meta:
        model = MonitoringSession
        fields = [
            "id",
            "patient",
            "patient_name",
            "mrn",
            "pregnancy_id",
            "gestational_age",
            "duration_seconds",
            "recorded_at",
            "added_by",
            "added_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "patient", "added_by", "created_at", "updated_at"]

    def validate_duration_seconds(self, value):
        if value <= 0:
            raise serializers.ValidationError("duration_seconds must be greater than zero.")
        if value > MAX_SESSION_DURATION_SECONDS:
            raise serializers.ValidationError(
                f"duration_seconds cannot exceed {MAX_SESSION_DURATION_SECONDS} seconds (24 hours).",
            )
        return value

    def validate_recorded_at(self, value):
        if value > timezone.now():
            raise serializers.ValidationError("recorded_at cannot be in the future.")
        return value


class MonitoringNoteSerializer(serializers.ModelSerializer):
    patient_name = serializers.CharField(source="patient.full_name", read_only=True)
    mrn = serializers.CharField(source="patient.mrn", read_only=True, default="")
    pregnancy_id = serializers.UUIDField(source="pregnancy.id", read_only=True, default=None)
    session_id = serializers.PrimaryKeyRelatedField(source="session", read_only=True, pk_field=serializers.UUIDField())
    added_by_name = serializers.CharField(source="added_by.get_full_name", read_only=True, default="")
    tags = ClinicalTagSerializer(many=True, read_only=True)
    tags_input = TagSpecSerializer(many=True, write_only=True, required=False)

    class Meta:
        model = MonitoringNote
        fields = [
            "id",
            "patient",
            "patient_name",
            "mrn",
            "pregnancy_id",
            "session_id",
            "note",
            "recorded_at",
            "added_by",
            "added_by_name",
            "tags",
            "tags_input",
            "left_voicemail",
            "two_way_communication",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "patient",
            "session_id",
            "recorded_at",
            "added_by",
            "created_at",
            "updated_at",
        ]

    def validate_note(self, value):
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError("note cannot be blank.")
        return cleaned

    def validate(self, attrs):
        note_text = attrs.get("note", getattr(self.instance, "note", "")).strip()
        left_voicemail = attrs.get("left_voicemail", getattr(self.instance, "left_voicemail", False))
        two_way = attrs.get("two_way_communication", getattr(self.instance, "two_way_communication", False))
        if (left_voicemail or two_way) and not note_text:
            raise serializers.ValidationError(
                {"left_voicemail": "left_voicemail/two_way_communication require a non-empty note."},
            )
        if left_voicemail and two_way:
            raise serializers.ValidationError(
                {"left_voicemail": "left_voicemail and two_way_communication cannot both be true."},
            )
        return attrs

    def update(self, instance, validated_data):
        tag_specs = validated_data.pop("tags_input", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if tag_specs is not None:
            instance.tags.set(get_or_create_tags(tag_specs, location=instance.patient.location))
        return instance


class CombinedMonitoringSerializer(serializers.Serializer):
    """Input for ``POST /patients/<patient_id>/monitoring/`` -- creates a
    session, a note, or both, atomically. ``patient`` comes from the URL,
    not the body.
    """

    duration_seconds = serializers.IntegerField(required=False, allow_null=True, default=None, min_value=1)
    recorded_at = serializers.DateTimeField(required=False)
    note = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="")
    tags = TagSpecSerializer(many=True, required=False, allow_null=True, default=list)
    left_voicemail = serializers.BooleanField(required=False, default=False)
    two_way_communication = serializers.BooleanField(required=False, default=False)

    def validate_duration_seconds(self, value):
        if value is None:
            return value
        if value > MAX_SESSION_DURATION_SECONDS:
            raise serializers.ValidationError(
                f"duration_seconds cannot exceed {MAX_SESSION_DURATION_SECONDS} seconds (24 hours).",
            )
        return value

    def validate_recorded_at(self, value):
        if value > timezone.now():
            raise serializers.ValidationError("recorded_at cannot be in the future.")
        return value

    def validate(self, attrs):
        duration = attrs.get("duration_seconds")
        note_text = (attrs.get("note") or "").strip()
        tags = attrs.get("tags") or []
        left_voicemail = attrs.get("left_voicemail", False)
        two_way = attrs.get("two_way_communication", False)

        if tags and not note_text:
            raise serializers.ValidationError({"tags": "tags require a non-empty note."})
        if (left_voicemail or two_way) and not note_text:
            raise serializers.ValidationError(
                {"left_voicemail": "left_voicemail/two_way_communication require a non-empty note."},
            )
        if left_voicemail and two_way:
            raise serializers.ValidationError(
                {"left_voicemail": "left_voicemail and two_way_communication cannot both be true."},
            )
        if not duration and not note_text:
            raise serializers.ValidationError("Provide at least a duration or a note.")
        return attrs


class StatusLabelSerializer(serializers.ModelSerializer):
    class Meta:
        model = StatusLabel
        fields = ["id", "name", "description", "color", "organization", "location", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_color(self, value):
        if not value:
            return None
        HEX_COLOR_VALIDATOR(value)
        return value

    def validate(self, attrs):
        """Exactly one of organization/location, and it must be the caller's
        own hospital -- same reasoning as ClinicalTagSerializer.validate above.
        """
        request = self.context.get("request")
        caller_org = getattr(getattr(request, "user", None), "organization", None) if request else None

        organization = attrs.get("organization", getattr(self.instance, "organization", None))
        location = attrs.get("location", getattr(self.instance, "location", None))

        if bool(organization) == bool(location):
            raise serializers.ValidationError("Provide exactly one of 'organization' or 'location'.")
        if organization is not None and organization != caller_org:
            raise serializers.ValidationError({"organization": "Must be your own hospital."})
        if location is not None and (caller_org is None or location.organization_id != caller_org.id):
            raise serializers.ValidationError({"location": "Must belong to your own hospital."})
        return attrs


class PatientStatusSerializer(serializers.ModelSerializer):
    patient_name = serializers.CharField(source="patient.full_name", read_only=True)
    added_by_name = serializers.CharField(source="added_by.get_full_name", read_only=True, default="")

    class Meta:
        model = PatientStatus
        fields = [
            "id",
            "patient",
            "patient_name",
            "pregnancy",
            "name",
            "description",
            "color",
            "added_by",
            "added_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "patient", "pregnancy", "added_by", "created_at", "updated_at"]

    def validate_name(self, value):
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError("name cannot be blank.")
        return cleaned

    def validate_color(self, value):
        HEX_COLOR_VALIDATOR(value)
        return value
