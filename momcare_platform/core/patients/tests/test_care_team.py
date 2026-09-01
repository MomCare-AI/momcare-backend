"""CareTeamMembership — additive to Pregnancy.assigned_staff, never a
replacement for it.

Model-level only: there is no API layer yet (Phase 2 of the dashboard master
plan). What matters at this layer is the one property the whole design leans
on — a deactivated staff member's membership rows must stop counting as
"currently assigned" without losing the historical fact that they once were,
using the exact same is_active-checking pattern already proven in
alerts/services.py:recipients_for_tier.
"""

from datetime import timedelta

import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from momcare_platform.core.patients.models import CareTeamMembership, Consent
from momcare_platform.core.patients.services import enrol_patient

pytestmark = pytest.mark.django_db


@pytest.fixture
def pregnancy_for(db):
    def _make(hospital, *, first_name="Ayesha", weeks_pregnant=20):
        lmp = timezone.now().date() - timedelta(weeks=weeks_pregnant)
        patient = enrol_patient(
            organization=hospital.org,
            recorded_by=hospital.admin,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
            pregnancy_data={"lmp": lmp},
            consent={"status": Consent.STATUS_GRANTED},
        )
        return patient.current_pregnancy

    return _make


def test_a_membership_starts_active_with_no_end_date(make_hospital, make_staff, pregnancy_for):
    hospital = make_hospital("Care Team Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@careteam.test")
    pregnancy = pregnancy_for(hospital)

    membership = CareTeamMembership.objects.create(
        pregnancy=pregnancy,
        staff=nurse.staff,
        role=CareTeamMembership.ROLE_NURSE,
    )

    assert membership.is_active is True
    assert membership.ended_at is None
    assert membership.started_at is not None


def test_ending_a_membership_preserves_the_row(make_hospital, make_staff, pregnancy_for):
    """A handoff is recorded by ending one row and starting another — never
    by deleting or mutating who held it, the same convention this codebase
    already uses for Consent and AlertEvent."""
    hospital = make_hospital("Handoff Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@handoff.test")
    pregnancy = pregnancy_for(hospital)
    membership = CareTeamMembership.objects.create(
        pregnancy=pregnancy,
        staff=nurse.staff,
        role=CareTeamMembership.ROLE_NURSE,
    )

    membership.end()
    membership.refresh_from_db()

    assert membership.is_active is False
    assert membership.ended_at is not None
    # The row itself still exists and is still queryable — this is the
    # historical fact "who was on the team, and until when."
    assert CareTeamMembership.objects.filter(id=membership.id).exists()


def test_a_deactivated_staff_members_membership_must_not_read_as_currently_assigned(
    make_hospital, make_staff, pregnancy_for
):
    """The property this whole design leans on. A departed nurse's row still
    resolves (PROTECT, soft-deleted Staff) — a query for "who's on this
    pregnancy's team right now" must filter on staff.is_active too, not just
    membership.is_active, or a deactivated clinician's patients would keep
    silently appearing in their own "my patients" view forever."""
    hospital = make_hospital("Deactivation Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@deactivation.test")
    pregnancy = pregnancy_for(hospital)
    membership = CareTeamMembership.objects.create(
        pregnancy=pregnancy,
        staff=nurse.staff,
        role=CareTeamMembership.ROLE_NURSE,
    )

    nurse.staff.deactivate(reason="Left the hospital")

    # The membership row itself is untouched — is_active is still True on
    # the membership, exactly like a departed Pregnancy.assigned_staff still
    # "looks" assigned. The correct query must join staff and check both.
    membership.refresh_from_db()
    assert membership.is_active is True

    currently_assigned = CareTeamMembership.objects.filter(
        pregnancy=pregnancy,
        role=CareTeamMembership.ROLE_NURSE,
        is_active=True,
        staff__is_active=True,
    )
    assert not currently_assigned.exists()

    # And the historical fact survives regardless.
    ever_assigned = CareTeamMembership.objects.filter(pregnancy=pregnancy, role=CareTeamMembership.ROLE_NURSE)
    assert ever_assigned.exists()


def test_an_already_deactivated_staff_member_cannot_be_newly_assigned(
    make_hospital, make_staff, pregnancy_for
):
    """Found by manual testing: nothing stopped a brand-new assignment for
    someone who had already left. It never leaked anything (the query-time
    staff__is_active check already caught it), but it let a meaningless row
    get created in the first place - blocked here, at the one point that
    guarantees it regardless of caller (admin form, shell, future API)."""
    hospital = make_hospital("Already Gone Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@alreadygone.test")
    nurse.staff.deactivate(reason="Left before this assignment was attempted")
    pregnancy = pregnancy_for(hospital)

    with pytest.raises(ValidationError):
        CareTeamMembership.objects.create(
            pregnancy=pregnancy,
            staff=nurse.staff,
            role=CareTeamMembership.ROLE_NURSE,
        )


def test_ending_an_existing_membership_still_works_after_staff_deactivates(
    make_hospital, make_staff, pregnancy_for
):
    """The guard is create-only - a row that already existed before the staff
    member left must still be endable, never stuck because of the same check
    that (correctly) blocks a brand-new assignment."""
    hospital = make_hospital("Endable Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@endable.test")
    pregnancy = pregnancy_for(hospital)
    membership = CareTeamMembership.objects.create(
        pregnancy=pregnancy,
        staff=nurse.staff,
        role=CareTeamMembership.ROLE_NURSE,
    )

    nurse.staff.deactivate(reason="Left after the assignment was already made")

    membership.end()  # must not raise
    membership.refresh_from_db()
    assert membership.is_active is False


def test_multiple_concurrent_nurses_are_supported(make_hospital, make_staff, pregnancy_for):
    """Unlike Pregnancy.assigned_staff (one lead), nurse/provider/care_manager
    memberships are many-concurrent by design — this is the whole reason the
    new model exists rather than just widening the existing field."""
    hospital = make_hospital("Multi Nurse Hospital")
    nurse_a = make_staff(hospital.org, settings.ROLE_NURSE, "nurse.a@multi.test")
    nurse_b = make_staff(hospital.org, settings.ROLE_NURSE, "nurse.b@multi.test")
    pregnancy = pregnancy_for(hospital)

    CareTeamMembership.objects.create(pregnancy=pregnancy, staff=nurse_a.staff, role=CareTeamMembership.ROLE_NURSE)
    CareTeamMembership.objects.create(pregnancy=pregnancy, staff=nurse_b.staff, role=CareTeamMembership.ROLE_NURSE)

    active_nurses = CareTeamMembership.objects.filter(
        pregnancy=pregnancy,
        role=CareTeamMembership.ROLE_NURSE,
        is_active=True,
    )
    assert active_nurses.count() == 2


def test_pregnancy_assigned_staff_is_untouched_by_care_team_membership(
    make_hospital, make_staff, pregnancy_for
):
    """The one thing this whole model is explicitly forbidden from doing."""
    hospital = make_hospital("Untouched Hospital")
    lead = make_staff(hospital.org, settings.ROLE_PROVIDER, "lead@untouched.test")
    co_provider = make_staff(hospital.org, settings.ROLE_PROVIDER, "co@untouched.test")
    pregnancy = pregnancy_for(hospital)
    pregnancy.assigned_staff = lead.staff
    pregnancy.save(update_fields=["assigned_staff", "updated_at"])

    CareTeamMembership.objects.create(
        pregnancy=pregnancy,
        staff=co_provider.staff,
        role=CareTeamMembership.ROLE_PROVIDER,
    )

    pregnancy.refresh_from_db()
    assert pregnancy.assigned_staff_id == lead.staff.id
