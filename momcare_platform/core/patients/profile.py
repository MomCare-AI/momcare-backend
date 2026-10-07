"""The patient-side profile: one account, one set of details, shared by every join request.

Her identity is split across two places, and this module is the only one that
knows it: name, phone, date of birth and address are ``User`` columns (they were
already there, and collected at sign-up); the rest is ``PatientProfile``. Callers
see one flat dict.

The key names are deliberately the same ones the hospital-side onboarding form
(``PatientCreateSerializer``) uses, so a snapshot can pre-fill that form as is.
"""

from __future__ import annotations

from django.db import transaction

from momcare_platform.core.patients.models import PatientProfile

ADDRESS_FIELDS = ["address_line1", "address_line2", "city", "state", "postal_code", "country"]

# Columns that live on User.
USER_FIELDS = ["first_name", "last_name", "phone", "date_of_birth", *ADDRESS_FIELDS]

# Columns that live on PatientProfile.
PROFILE_FIELDS = [
    "national_id",
    "blood_group",
    "emergency_contact_name",
    "emergency_contact_phone",
    "emergency_contact_relation",
    "emergency_contact_email",
]

# What must be filled in before she may send a join request. national_id is
# left out because not every country issues one; blood_group because many women
# do not know theirs and would be stuck.
REQUIRED_FIELDS = [
    *USER_FIELDS,
    "emergency_contact_name",
    "emergency_contact_phone",
    "emergency_contact_relation",
    "emergency_contact_email",
]


def profile_values(user) -> dict:
    """Everything in her profile as one flat dict, read straight from the rows.

    Never creates a ``PatientProfile``: reading must not write.
    """
    profile = PatientProfile.objects.filter(user=user).first()
    values = {field: getattr(user, field) for field in USER_FIELDS}
    for field in PROFILE_FIELDS:
        values[field] = getattr(profile, field) if profile is not None else ("" if field != "national_id" else None)
    return values


def missing_fields(user) -> list[str]:
    values = profile_values(user)
    return [field for field in REQUIRED_FIELDS if not values[field]]


def is_complete(user) -> bool:
    return not missing_fields(user)


def snapshot(user) -> dict:
    """A JSON-safe copy of her profile, frozen onto a join request when she sends it.

    Later edits to her profile do not change a request already sent: what the
    hospital was shown is what it was shown.
    """
    values = profile_values(user)
    if values["date_of_birth"] is not None:
        values["date_of_birth"] = values["date_of_birth"].isoformat()
    return values


def payload(user) -> dict:
    """What GET/PATCH /my-profile/ return: her details, plus what is still missing."""
    values = snapshot(user)
    missing = [field for field in REQUIRED_FIELDS if not values[field]]
    return {"email": user.email, **values, "is_complete": not missing, "missing_fields": missing}


@transaction.atomic
def save_profile(user, data: dict) -> None:
    """Write the fields she sent: ``User`` columns to ``User``, the rest to her ``PatientProfile``."""
    user_changes = {k: v for k, v in data.items() if k in USER_FIELDS}
    for field, value in user_changes.items():
        setattr(user, field, value)
    if user_changes:
        user.save(update_fields=[*user_changes, "updated_at"])

    profile_changes = {k: v for k, v in data.items() if k in PROFILE_FIELDS}
    if profile_changes:
        profile, _ = PatientProfile.objects.get_or_create(user=user)
        for field, value in profile_changes.items():
            setattr(profile, field, value)
        profile.save(update_fields=[*profile_changes, "updated_at"])
