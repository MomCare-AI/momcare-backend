"""Model-level behavior for NoteTemplate."""

import pytest
from django.db import IntegrityError, transaction

from momcare_platform.core.locations.models import Location
from momcare_platform.core.monitoring.models import NoteTemplate

pytestmark = pytest.mark.django_db


def test_note_template_check_constraint_rejects_neither_scope(make_hospital):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            NoteTemplate.objects.create(title="No Scope", content="x")


def test_note_template_check_constraint_rejects_both_scopes(make_hospital):
    hospital = make_hospital("Reject Both Template Hospital")
    branch = Location.objects.create(organization=hospital.org, name="Branch", location_manager=hospital.admin)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            NoteTemplate.objects.create(title="Both", content="x", organization=hospital.org, location=branch)


def test_duplicate_note_template_title_in_same_org_is_rejected(make_hospital):
    hospital = make_hospital("Duplicate Template Hospital")
    NoteTemplate.objects.create(title="Routine Check-in", content="Patient reports...", organization=hospital.org)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            NoteTemplate.objects.create(
                title="Routine Check-in",
                content="Different content",
                organization=hospital.org,
            )


def test_same_note_template_title_allowed_in_different_orgs(make_hospital):
    alpha = make_hospital("Alpha Template Hospital")
    beta = make_hospital("Beta Template Hospital")
    NoteTemplate.objects.create(title="Routine Check-in", content="x", organization=alpha.org)
    # Must not raise -- different organization, same title.
    NoteTemplate.objects.create(title="Routine Check-in", content="y", organization=beta.org)


def test_same_title_allowed_at_org_level_and_location_level(make_hospital):
    """A location-scoped title doesn't collide with an org-scoped one of the
    same name -- the unique constraints are per-scope-column, not global."""
    hospital = make_hospital("Same Title Different Scope Hospital")
    branch = Location.objects.create(organization=hospital.org, name="Branch", location_manager=hospital.admin)
    NoteTemplate.objects.create(title="Routine Check-in", content="x", organization=hospital.org)
    # Must not raise.
    NoteTemplate.objects.create(title="Routine Check-in", content="y", location=branch)
