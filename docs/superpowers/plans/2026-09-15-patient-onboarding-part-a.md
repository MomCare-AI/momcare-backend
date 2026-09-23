# Patient Onboarding (Part A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild MomCare's hospital-initiated patient onboarding to match the finalized
design: three direct care-team FK columns (`provider`/`nurse`/`care_manager`) replacing the
`CareTeamMembership` join table, a per-organization-unique CNIC, a direct `Patient.organization`
column, an `emergency_contact_email` field, and a reinstated `PatientProgramEnrollment` model
scoped to one program (`"rpm"` / "Maternal Monitoring") for future extensibility.

**Architecture:** Existing `patients` app models/services/serializers/views are modified in
place, not rewritten — `enrol_patient()` becomes `onboard_patient()`, the same one
`@transaction.atomic` function that creates `Patient` + `Consent` + `Pregnancy` +
`PregnancyRiskFactors` now also opens `PatientProgramEnrollment` rows and validates the care
team through the existing nested `PregnancyWriteSerializer`. Renaming `Pregnancy.assigned_staff`
→ `provider` and adding `nurse`/`care_manager` ripples into four other apps (`staff`,
`common/scoping.py`, `alerts`, `monitoring`) that reference the old field name — each gets its
own task so the rename lands everywhere in one pass, not partially.

**Tech Stack:** Django 5 / DRF, PostgreSQL, pytest-django. No new dependencies.

**Spec:** `docs/design/2026-09-15-patient-onboarding-design.md` — this plan implements Part A
only (Decisions 1–10 and the revised Decision 2). Part B (self-registration) is explicitly not
part of this plan.

## Global Constraints

- Every migration must be reversible or clearly justified if not (Django's default reverse
  behavior is acceptable for `AddField`/`RemoveField`/`RenameField`; data-migrations need an
  explicit (even if no-op) `reverse_code`).
- No endpoint may accept `organization` or `location` from the request body — always resolved
  server-side from the authenticated user, per the existing tenant-isolation rule already
  enforced throughout `patients/api/views.py`.
- Every new/changed model field follows the existing `Deactivatable`/`PROTECT`/UUID-PK
  conventions already used throughout `core/patients/models.py` — no `CASCADE` on any FK to
  `Staff` or `Patient`.
- Role codes are always read via `settings.ROLE_*` constants or `user.role_code` /
  `staff.user.role_code` — never hardcoded strings (existing project rule, `CLAUDE.md`).
- Run `uv run pytest momcare_platform/core -q` after every task — the suite must stay green
  before moving to the next task.

---

### Task 1: `Patient.organization`, CNIC uniqueness, `emergency_contact_email`

**Files:**
- Modify: `momcare_platform/core/patients/models.py:20-98` (`Patient` class)
- Create: `momcare_platform/core/patients/migrations/0007_patient_organization_and_cnic.py`
- Modify: `momcare_platform/core/patients/services.py:29-84` (`enrol_patient`, will be renamed in Task 10 — for now just the CNIC normalization)
- Test: `momcare_platform/core/patients/tests/api/test_patients.py` (append)

**Interfaces:**
- Produces: `Patient.organization` (FK → `organization.Organization`, `on_delete=PROTECT`,
  `related_name="patients"`), `Patient.emergency_contact_email` (`EmailField`, blank),
  `Patient.cnic` now `null=True` (was `blank=True` only), constraint name
  `unique_cnic_per_organization`.

- [ ] **Step 1: Write the failing tests**

Append to `momcare_platform/core/patients/tests/api/test_patients.py`:

```python
def test_patient_organization_is_set_from_the_enrolling_hospital(client, make_hospital, auth):
    hospital = make_hospital("Org Column Hospital")
    response = post_patient(client, auth(hospital.admin.email))

    patient = Patient.objects.get(id=response.json()["id"])
    assert patient.organization_id == hospital.org.id


def test_cnic_is_unique_within_a_hospital(client, make_hospital, auth):
    hospital = make_hospital("CNIC Unique Hospital")
    headers = auth(hospital.admin.email)
    post_patient(client, headers, first_name="First", cnic="61101-1111111-1")

    response = post_patient(client, headers, first_name="Second", cnic="61101-1111111-1")

    assert response.status_code == 400


def test_cnic_can_repeat_across_different_hospitals(client, make_hospital, auth):
    alpha = make_hospital("CNIC Alpha")
    beta = make_hospital("CNIC Beta")

    first = post_patient(client, auth(alpha.admin.email), cnic="61101-2222222-2")
    second = post_patient(client, auth(beta.admin.email), cnic="61101-2222222-2")

    assert first.status_code == 201
    assert second.status_code == 201


def test_two_patients_without_cnic_do_not_collide(client, make_hospital, auth):
    """Blank CNIC is stored as NULL, never '', so two blank values never
    false-positive collide under the unique constraint."""
    hospital = make_hospital("No CNIC Hospital")
    headers = auth(hospital.admin.email)

    first = post_patient(client, headers, first_name="First", cnic="")
    second = post_patient(client, headers, first_name="Second", cnic="")

    assert first.status_code == 201
    assert second.status_code == 201


def test_emergency_contact_email_is_accepted_and_stored(client, make_hospital, auth):
    hospital = make_hospital("Emergency Email Hospital")
    response = post_patient(client, auth(hospital.admin.email), emergency_contact_email="brother@example.test")

    patient = Patient.objects.get(id=response.json()["id"])
    assert patient.emergency_contact_email == "brother@example.test"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest momcare_platform/core/patients/tests/api/test_patients.py -k "organization_is_set or cnic or emergency_contact_email" -v`
Expected: FAIL — `Patient` has no `organization` attribute, no `emergency_contact_email` field, and the CNIC uniqueness test fails because nothing currently blocks the duplicate.

- [ ] **Step 3: Add the fields to `Patient`**

In `momcare_platform/core/patients/models.py`, replace lines 38–42 (the `location` field) through
line 77 (`mrn`) region as follows — add the `organization` FK right after `location`, change
`cnic` to `null=True`, and add `emergency_contact_email` after `emergency_contact_relation`:

```python
    location = models.ForeignKey(
        "locations.Location",
        on_delete=models.PROTECT,
        related_name="patients",
    )
    # Denormalized from location.organization, the same way Device carries its
    # own direct organization FK — needed so CNIC uniqueness (below) can be
    # scoped correctly. A hospital can have several locations; a
    # location-scoped constraint would miss a duplicate CNIC at a different
    # branch of the same hospital.
    organization = models.ForeignKey(
        "organization.Organization",
        on_delete=models.PROTECT,
        related_name="patients",
    )
```

Then change the `cnic` field definition (currently `blank=True, db_index=True`) to:

```python
    # Also not unique across the whole platform — typos happen, and not
    # everyone holds a CNIC. It IS unique within one hospital (see the
    # constraint below); null=True (not blank-string) so two CNIC-less
    # patients at the same hospital never false-positive collide.
    cnic = models.CharField(_("CNIC"), max_length=20, blank=True, null=True, db_index=True)
```

And add, immediately after `emergency_contact_relation`:

```python
    # Captured now for a future notification feature — no send-logic exists
    # yet. Not a new kind of system user; just better-captured data on the
    # existing emergency contact.
    emergency_contact_email = models.EmailField(blank=True, default="")
```

Finally, add the uniqueness constraint to `Patient.Meta`, inside the existing `indexes` list's
sibling `constraints` (there is none yet — add it):

```python
    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["mrn"]),
            models.Index(fields=["phone"]),
            models.Index(fields=["cnic"]),
            models.Index(fields=["last_name", "first_name"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "cnic"],
                condition=models.Q(cnic__isnull=False),
                name="unique_cnic_per_organization",
            ),
        ]
```

- [ ] **Step 4: Write the migration**

Create `momcare_platform/core/patients/migrations/0007_patient_organization_and_cnic.py`:

```python
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_organization(apps, schema_editor):
    Patient = apps.get_model("patients", "Patient")
    for patient in Patient.objects.all().iterator():
        patient.organization_id = patient.location.organization_id
        patient.save(update_fields=["organization"])


def normalize_empty_cnic_to_null(apps, schema_editor):
    Patient = apps.get_model("patients", "Patient")
    Patient.objects.filter(cnic="").update(cnic=None)


def check_no_duplicate_cnics(apps, schema_editor):
    """Fail the migration with a readable message instead of a raw
    IntegrityError from AddConstraint, if any hospital already has two
    patients sharing a CNIC (per design doc: "needs a pre-check ... before
    the constraint can be applied")."""
    from django.db.models import Count

    Patient = apps.get_model("patients", "Patient")
    duplicates = list(
        Patient.objects.exclude(cnic__isnull=True)
        .values("organization_id", "cnic")
        .annotate(n=Count("id"))
        .filter(n__gt=1),
    )
    if duplicates:
        raise RuntimeError(
            "Cannot add unique_cnic_per_organization: found existing duplicate CNICs "
            f"within a hospital: {duplicates}. Resolve these records before migrating.",
        )


class Migration(migrations.Migration):
    dependencies = [
        ("organization", "0007_care_team_row_level_security"),
        ("patients", "0006_careteammembership_ended_by"),
    ]

    operations = [
        migrations.AddField(
            model_name="patient",
            name="organization",
            field=models.ForeignKey(
                to="organization.organization",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="patients",
                null=True,  # temporary — backfilled below, then locked down
            ),
        ),
        migrations.RunPython(backfill_organization, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="patient",
            name="organization",
            field=models.ForeignKey(
                to="organization.organization",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="patients",
            ),
        ),
        migrations.AlterField(
            model_name="patient",
            name="cnic",
            field=models.CharField(max_length=20, blank=True, null=True, db_index=True),
        ),
        migrations.RunPython(normalize_empty_cnic_to_null, migrations.RunPython.noop),
        migrations.RunPython(check_no_duplicate_cnics, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="patient",
            constraint=models.UniqueConstraint(
                fields=["organization", "cnic"],
                condition=models.Q(cnic__isnull=False),
                name="unique_cnic_per_organization",
            ),
        ),
        migrations.AddField(
            model_name="patient",
            name="emergency_contact_email",
            field=models.EmailField(max_length=254, blank=True, default=""),
        ),
    ]
```

- [ ] **Step 5: Wire `organization` and CNIC normalization into enrolment**

In `momcare_platform/core/patients/services.py`, `enrol_patient()` (lines 28–37), the
`Patient.objects.create(...)` call at lines 55-59 needs `organization=organization` added, and
`patient_data["cnic"]` normalized to `None` when blank. Modify the loop body:

```python
        try:
            with transaction.atomic():
                cnic = patient_data.get("cnic") or None
                patient = Patient.objects.create(
                    location=location,
                    organization=organization,
                    mrn=_candidate_mrn(organization, offset=attempt),
                    **{**patient_data, "cnic": cnic},
                )
            break
```

- [ ] **Step 6: Add `emergency_contact_email` to the create/detail serializers**

In `momcare_platform/core/patients/api/serializers.py`:

`PatientCreateSerializer` (around line 374) — add after `emergency_contact_relation`:

```python
    emergency_contact_email = serializers.EmailField(required=False, allow_blank=True, default="")
```

And add `"emergency_contact_email"` to `PATIENT_FIELDS` (line 381-392).

`PatientDetailSerializer.Meta.fields` (line 314-335) — add `"emergency_contact_email"` right
after `"emergency_contact_relation"`.

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest momcare_platform/core/patients/tests/api/test_patients.py -v`
Expected: PASS — including the pre-existing tests in this file (they must still pass unchanged).

- [ ] **Step 8: Run full patients suite, then commit**

Run: `uv run pytest momcare_platform/core -q`
Expected: all pass.

```bash
git add momcare_platform/core/patients/models.py \
        momcare_platform/core/patients/services.py \
        momcare_platform/core/patients/api/serializers.py \
        momcare_platform/core/patients/migrations/0007_patient_organization_and_cnic.py \
        momcare_platform/core/patients/tests/api/test_patients.py
git commit -m "feat(patients): add Patient.organization, per-hospital CNIC uniqueness, emergency_contact_email"
```

---

### Task 2: `Pregnancy.provider` (rename), `nurse`, `care_manager`

**Files:**
- Modify: `momcare_platform/core/patients/models.py:190-270` (`Pregnancy` class)
- Create: `momcare_platform/core/patients/migrations/0008_pregnancy_provider_nurse_care_manager.py`
- Modify: `momcare_platform/core/patients/api/serializers.py` (`PregnancySerializer`, `PregnancyWriteSerializer`)
- Modify: `momcare_platform/core/patients/admin.py` (`PregnancyInline`, `PregnancyAdmin`)
- Test: `momcare_platform/core/patients/tests/api/test_patients.py` (append)

**Interfaces:**
- Consumes: `OrganizationStaffField` from Task-unchanged `patients/api/serializers.py:115-131`.
- Produces: `Pregnancy.provider` (same column as old `assigned_staff`, same `related_name=
  "assigned_pregnancies"`), `Pregnancy.nurse` (`related_name="nursed_pregnancies"`),
  `Pregnancy.care_manager` (`related_name="care_managed_pregnancies"`) — all
  `FK → staff.Staff, on_delete=PROTECT, null=True, blank=True`. Serializer fields `provider`,
  `provider_name`, `provider_is_active`, `nurse`, `nurse_name`, `care_manager`,
  `care_manager_name` on `PregnancySerializer`/`PregnancyWriteSerializer`.

This task **only** renames the field and adds the two new ones — role/org/capacity validation
is Task 9. Tests here only check the fields exist and are writable/readable; not yet that a
wrong-role staff member gets rejected.

- [ ] **Step 1: Write the failing tests**

Append to `momcare_platform/core/patients/tests/api/test_patients.py`:

```python
def test_pregnancy_accepts_provider_nurse_and_care_manager_together(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Full Care Team Hospital")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, "provider@fullcareteam.test")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@fullcareteam.test")
    care_manager = make_staff(hospital.org, settings.ROLE_CARE_MANAGER, "cm@fullcareteam.test")

    response = post_patient(
        client,
        auth(hospital.admin.email),
        pregnancy={
            "lmp": "2026-01-01",
            "provider": str(provider.staff.id),
            "nurse": str(nurse.staff.id),
            "care_manager": str(care_manager.staff.id),
        },
    )

    assert response.status_code == 201
    pregnancy = Patient.objects.get(id=response.json()["id"]).current_pregnancy
    assert pregnancy.provider_id == provider.staff.id
    assert pregnancy.nurse_id == nurse.staff.id
    assert pregnancy.care_manager_id == care_manager.staff.id
```

`make_staff` (from `conftest.py`) returns a `User`; `make_staff(...).staff` is the related
`Staff` row (see `conftest.py:97` — `Staff.objects.create(user=user, ...)`, and Django's
`OneToOneField` reverse accessor `user.staff`).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest momcare_platform/core/patients/tests/api/test_patients.py -k full_care_team -v`
Expected: FAIL — `Pregnancy` has no `nurse`/`care_manager` fields, and the payload key
`provider` doesn't exist yet (still `assigned_staff`).

- [ ] **Step 3: Rename and add the fields on `Pregnancy`**

In `momcare_platform/core/patients/models.py`, replace the `assigned_staff` field (lines
190-203) with:

```python
    # The lead clinician — one accountable name for this pregnancy, the one
    # alert escalation routes to. Renamed from assigned_staff to match the
    # onboarding payload's care_team naming.
    #
    # PROTECT preserves who was responsible. Staff is soft-deleted, so this
    # never blocks anything in practice — it guarantees history survives.
    provider = models.ForeignKey(
        "staff.Staff",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="assigned_pregnancies",
    )
    # One nurse and one care manager at a time — deliberately simpler than a
    # join table (no multi-nurse rotation, no handoff history); matches the
    # reference implementation's shape. See design doc Decision 3.
    nurse = models.ForeignKey(
        "staff.Staff",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="nursed_pregnancies",
    )
    care_manager = models.ForeignKey(
        "staff.Staff",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="care_managed_pregnancies",
    )
```

And update `has_responsible_clinician` (lines 261-270) and `__str__`/anywhere else in this file
referencing `assigned_staff`:

```python
    @property
    def has_responsible_clinician(self) -> bool:
        """Whether someone is actually accountable for this pregnancy.

        A clinician who has left the hospital is soft-deleted, so the FK still
        resolves and the record still *looks* assigned. For a system that will
        route alerts to this person, an inactive assignment is the same silent
        failure as no assignment at all, and both must surface.
        """
        return self.provider is not None and self.provider.is_active
```

- [ ] **Step 4: Write the migration**

Create `momcare_platform/core/patients/migrations/0008_pregnancy_provider_nurse_care_manager.py`:

```python
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("patients", "0007_patient_organization_and_cnic"),
    ]

    operations = [
        migrations.RenameField(
            model_name="pregnancy",
            old_name="assigned_staff",
            new_name="provider",
        ),
        migrations.AddField(
            model_name="pregnancy",
            name="nurse",
            field=models.ForeignKey(
                to="staff.staff",
                on_delete=django.db.models.deletion.PROTECT,
                null=True,
                blank=True,
                related_name="nursed_pregnancies",
            ),
        ),
        migrations.AddField(
            model_name="pregnancy",
            name="care_manager",
            field=models.ForeignKey(
                to="staff.staff",
                on_delete=django.db.models.deletion.PROTECT,
                null=True,
                blank=True,
                related_name="care_managed_pregnancies",
            ),
        ),
    ]
```

- [ ] **Step 5: Update `PregnancySerializer`/`PregnancyWriteSerializer`**

In `momcare_platform/core/patients/api/serializers.py`, replace lines 30-93
(`PregnancySerializer`) with:

```python
class PregnancySerializer(serializers.ModelSerializer):
    """Gestational age is exposed but never accepted — it is derived from the
    EDD on every read, so the client and the risk engine cannot disagree."""

    gestational_age_weeks = serializers.SerializerMethodField()
    gestational_age_days = serializers.SerializerMethodField()
    gestational_age_display = serializers.CharField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    edd_source_display = serializers.CharField(source="get_edd_source_display", read_only=True)
    provider_name = serializers.CharField(source="provider.user.get_full_name", read_only=True, default="")
    nurse_name = serializers.CharField(source="nurse.user.get_full_name", read_only=True, default="")
    care_manager_name = serializers.CharField(
        source="care_manager.user.get_full_name",
        read_only=True,
        default="",
    )
    # Surfaced so the UI can warn: an assignment to someone who has left the
    # hospital is as good as no assignment once alerts start routing.
    has_responsible_clinician = serializers.BooleanField(read_only=True)
    provider_is_active = serializers.BooleanField(source="provider.is_active", read_only=True, default=False)
    risk_factors = PregnancyRiskFactorsSerializer(read_only=True)

    class Meta:
        model = Pregnancy
        fields = [
            "id",
            "patient",
            "lmp",
            "edd",
            "edd_source",
            "edd_source_display",
            "edd_confirmed_at",
            "gestational_age_weeks",
            "gestational_age_days",
            "gestational_age_display",
            "gravida",
            "para",
            "provider",
            "provider_name",
            "provider_is_active",
            "nurse",
            "nurse_name",
            "care_manager",
            "care_manager_name",
            "has_responsible_clinician",
            "status",
            "status_display",
            "outcome_date",
            "notes",
            "risk_factors",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "patient",
            "edd_confirmed_at",
            "provider_is_active",
            "has_responsible_clinician",
            "gestational_age_weeks",
            "gestational_age_days",
            "gestational_age_display",
            "risk_factors",
            "created_at",
            "updated_at",
        ]

    def get_gestational_age_weeks(self, obj) -> int | None:
        age = obj.gestational_age
        return age.weeks if age else None

    def get_gestational_age_days(self, obj) -> int | None:
        age = obj.gestational_age
        return age.days if age else None

    def validate(self, attrs):
        lmp = attrs.get("lmp", getattr(self.instance, "lmp", None))
        edd = attrs.get("edd", getattr(self.instance, "edd", None))
        if lmp is None and edd is None:
            raise serializers.ValidationError(
                "Provide either a last menstrual period or an estimated delivery date — "
                "without one, gestational age cannot be calculated and no reading can be "
                "interpreted.",
            )
        return attrs
```

Then update `PregnancyWriteSerializer` (lines 134-148, right after `OrganizationStaffField`):

```python
class PregnancyWriteSerializer(PregnancySerializer):
    """Create/update, allowing risk factors to be set alongside the pregnancy."""

    risk_factors = PregnancyRiskFactorsSerializer(required=False)
    provider = OrganizationStaffField(required=False, allow_null=True)
    nurse = OrganizationStaffField(required=False, allow_null=True)
    care_manager = OrganizationStaffField(required=False, allow_null=True)

    class Meta(PregnancySerializer.Meta):
        read_only_fields = [f for f in PregnancySerializer.Meta.read_only_fields if f != "risk_factors"]

    def update(self, instance, validated_data):
        factors = validated_data.pop("risk_factors", None)
        pregnancy = super().update(instance, validated_data)
        if factors:
            PregnancyRiskFactors.objects.update_or_create(pregnancy=pregnancy, defaults=factors)
        return pregnancy
```

- [ ] **Step 6: Update `admin.py`**

In `momcare_platform/core/patients/admin.py`:

`PregnancyInline.fields` (line 16):

```python
    fields = ["status", "lmp", "edd", "edd_source", "gravida", "para", "provider", "nurse", "care_manager"]
```

`PregnancyAdmin.list_display` (line 65):

```python
    list_display = ["patient", "status", "gestational_age_display", "edd", "edd_source", "provider"]
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest momcare_platform/core/patients/tests/api/test_patients.py -v`
Expected: this new test passes. **Other existing tests in this file and elsewhere that still
say `assigned_staff` will now fail** — that's expected and is fixed in Task 11, not here. Note
which ones fail so Task 11's list is accurate; do not fix them in this task.

- [ ] **Step 8: Commit**

```bash
git add momcare_platform/core/patients/models.py \
        momcare_platform/core/patients/api/serializers.py \
        momcare_platform/core/patients/admin.py \
        momcare_platform/core/patients/migrations/0008_pregnancy_provider_nurse_care_manager.py \
        momcare_platform/core/patients/tests/api/test_patients.py
git commit -m "feat(patients): rename Pregnancy.assigned_staff to provider, add nurse/care_manager"
```

---

### Task 3: Delete `CareTeamMembership` entirely

**Files:**
- Modify: `momcare_platform/core/patients/models.py` (remove `CareTeamMembership` class)
- Modify: `momcare_platform/core/patients/admin.py` (remove `CareTeamMembershipAdmin`)
- Modify: `momcare_platform/core/patients/api/serializers.py` (remove `CareTeamMembershipSerializer`, `CareTeamMembershipCreateSerializer`)
- Modify: `momcare_platform/core/patients/api/views.py` (remove `CareTeamMembershipListCreateView`, `CareTeamMembershipEndView`, `_can_manage_care_team`)
- Modify: `config/api_router.py` (remove the two care-team URL patterns and their imports)
- Delete: `momcare_platform/core/patients/tests/test_care_team.py`
- Delete: `momcare_platform/core/patients/tests/api/test_care_team.py`
- Create: `momcare_platform/core/patients/migrations/0009_delete_careteammembership.py`

**Interfaces:**
- Nothing later depends on `CareTeamMembership` after this task — Task 5/6/7/8 replace every
  reference to it with the new `provider`/`nurse`/`care_manager` fields.

**Note on RLS:** `organization/migrations/0007_care_team_row_level_security.py` added a Postgres
`ENABLE ROW LEVEL SECURITY` + policy to the `patients_careteammembership` table. Dropping the
table (via `DeleteModel` below) makes Postgres automatically drop that policy along with it —
no separate reverse-RLS migration is needed in the `organization` app.

- [ ] **Step 1: Delete the two test files**

```bash
rm momcare_platform/core/patients/tests/test_care_team.py
rm momcare_platform/core/patients/tests/api/test_care_team.py
```

- [ ] **Step 2: Remove `CareTeamMembership` from `models.py`**

Delete the entire `CareTeamMembership` class, `momcare_platform/core/patients/models.py:273-405`
(from `class CareTeamMembership(...)` through the end of its `end()` method, inclusive of the
blank line before `class PregnancyRiskFactors`).

- [ ] **Step 3: Remove the admin registration**

Delete `CareTeamMembershipAdmin`, `momcare_platform/core/patients/admin.py:115-134`, and remove
`CareTeamMembership` from the import at the top of the file (line 3-10 currently imports it).

- [ ] **Step 4: Remove the serializers**

Delete `CareTeamMembershipSerializer` and `CareTeamMembershipCreateSerializer`,
`momcare_platform/core/patients/api/serializers.py:195-231`, and remove `CareTeamMembership`
from the `momcare_platform.core.patients.models` import at the top of the file (line 3-10).

- [ ] **Step 5: Remove the views**

In `momcare_platform/core/patients/api/views.py`:

- Delete `_can_manage_care_team` (lines 492-517).
- Delete `CareTeamMembershipListCreateView` (lines 520-583).
- Delete `CareTeamMembershipEndView` (lines 586-629).
- Remove `CareTeamMembership` from the `momcare_platform.core.patients.models` import
  (line 41).
- Remove `CareTeamMembershipCreateSerializer`, `CareTeamMembershipSerializer` from the
  `patients.api.serializers` import (lines 27-40).
- Remove the now-unused `from django.conf import settings` import **only if** nothing else in
  this file uses `settings` — check first (`_can_manage_care_team` was the only user of
  `settings.ROLE_HOSPITAL_ADMIN`/`settings.ROLE_CARE_MANAGER` in this file; confirm with
  `grep settings momcare_platform/core/patients/api/views.py` before removing the import).

- [ ] **Step 6: Remove the URL patterns**

In `config/api_router.py`:

- Remove `CareTeamMembershipEndView`, `CareTeamMembershipListCreateView` from the
  `momcare_platform.core.patients.api.views` import (lines 36-44).
- Delete the two `re_path(...)` entries for `care-team-list` and `care-team-end`
  (lines 220-233, including the preceding comment `# Care team — supporting members...`).

- [ ] **Step 7: Write the migration**

Create `momcare_platform/core/patients/migrations/0009_delete_careteammembership.py`:

```python
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("patients", "0008_pregnancy_provider_nurse_care_manager"),
    ]

    operations = [
        migrations.DeleteModel(name="careteammembership"),
    ]
```

- [ ] **Step 8: Run the full suite**

Run: `uv run pytest momcare_platform/core -q`
Expected: failures in `staff`, `alerts`, `monitoring`, and remaining `patients` tests that still
reference `CareTeamMembership`/`assigned_staff` — these are fixed by Tasks 5-8 and 11. Confirm
no import errors or crashes beyond `NameError`/`AttributeError`/assertion failures tied to the
rename — anything else (e.g. a syntax error) means a mistake in this task and must be fixed now.

- [ ] **Step 9: Commit**

```bash
git add momcare_platform/core/patients/models.py \
        momcare_platform/core/patients/admin.py \
        momcare_platform/core/patients/api/serializers.py \
        momcare_platform/core/patients/api/views.py \
        config/api_router.py \
        momcare_platform/core/patients/migrations/0009_delete_careteammembership.py
git rm momcare_platform/core/patients/tests/test_care_team.py \
       momcare_platform/core/patients/tests/api/test_care_team.py
git commit -m "refactor(patients): remove CareTeamMembership, superseded by provider/nurse/care_manager"
```

---

### Task 4: `PatientProgramEnrollment`

**Files:**
- Modify: `momcare_platform/core/patients/models.py` (add `PatientProgramEnrollment`)
- Modify: `momcare_platform/core/patients/services.py` (add `open_program_enrollment`)
- Modify: `momcare_platform/core/patients/admin.py` (register it)
- Create: `momcare_platform/core/patients/migrations/0010_patientprogramenrollment.py`
- Create: `momcare_platform/core/organization/migrations/0008_program_enrollment_row_level_security.py`
- Test: `momcare_platform/core/patients/tests/test_program_enrollment.py` (new)

**Interfaces:**
- Produces: `PatientProgramEnrollment` model, `PatientProgramEnrollment.PROGRAM_RPM = "rpm"`,
  `open_program_enrollment(*, patient, program_code, enrolled_at=None) -> PatientProgramEnrollment`
  in `patients/services.py` (Task 10 wires this into onboarding; this task only builds the
  model + the standalone service function + its own unit tests).

- [ ] **Step 1: Write the failing tests**

Create `momcare_platform/core/patients/tests/test_program_enrollment.py`:

```python
"""PatientProgramEnrollment — kept for future extensibility (e.g. a CCM-style
program later), even though MomCare has exactly one program today. See
docs/design/2026-09-15-patient-onboarding-design.md, Decision 2 (revised).
"""

import pytest
from django.db import IntegrityError

from momcare_platform.core.patients.models import Consent, PatientProgramEnrollment
from momcare_platform.core.patients.services import enrol_patient, open_program_enrollment

pytestmark = pytest.mark.django_db


@pytest.fixture
def patient_for(make_hospital):
    def _make(hospital):
        return enrol_patient(
            organization=hospital.org,
            recorded_by=hospital.admin,
            patient_data={"first_name": "Ayesha", "last_name": "Bibi"},
            consent={"status": Consent.STATUS_GRANTED},
        )

    return _make


def test_opening_an_enrollment_creates_an_open_row(make_hospital, patient_for):
    hospital = make_hospital("Enrollment Hospital")
    patient = patient_for(hospital)

    enrollment = open_program_enrollment(patient=patient, program_code=PatientProgramEnrollment.PROGRAM_RPM)

    assert enrollment.status == PatientProgramEnrollment.STATUS_ENROLLED
    assert enrollment.disenrolled_at is None


def test_opening_an_enrollment_twice_is_idempotent(make_hospital, patient_for):
    """Matches the reference implementation's own enroll_patient(): calling
    it again with no discharge in between returns the same open row rather
    than erroring or duplicating."""
    hospital = make_hospital("Idempotent Enrollment Hospital")
    patient = patient_for(hospital)

    first = open_program_enrollment(patient=patient, program_code=PatientProgramEnrollment.PROGRAM_RPM)
    second = open_program_enrollment(patient=patient, program_code=PatientProgramEnrollment.PROGRAM_RPM)

    assert first.id == second.id
    assert PatientProgramEnrollment.objects.filter(patient=patient).count() == 1


def test_two_open_enrollments_for_the_same_program_are_rejected_at_the_db_level(make_hospital, patient_for):
    hospital = make_hospital("DB Constraint Hospital")
    patient = patient_for(hospital)
    PatientProgramEnrollment.objects.create(patient=patient, program_code=PatientProgramEnrollment.PROGRAM_RPM)

    with pytest.raises(IntegrityError):
        PatientProgramEnrollment.objects.create(patient=patient, program_code=PatientProgramEnrollment.PROGRAM_RPM)


def test_program_code_display_is_maternal_monitoring(make_hospital, patient_for):
    """The internal code stays 'rpm' (matches the reference implementation);
    the display label is globally understandable and carries no US-billing
    assumption — see design doc Decision 2's naming note."""
    hospital = make_hospital("Display Name Hospital")
    patient = patient_for(hospital)

    enrollment = open_program_enrollment(patient=patient, program_code=PatientProgramEnrollment.PROGRAM_RPM)

    assert enrollment.get_program_code_display() == "Maternal Monitoring"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest momcare_platform/core/patients/tests/test_program_enrollment.py -v`
Expected: FAIL — `PatientProgramEnrollment` and `open_program_enrollment` don't exist yet.

- [ ] **Step 3: Add the model**

Append to `momcare_platform/core/patients/models.py` (after `ClinicalNote`, at the end of the
file):

```python
class PatientProgramEnrollment(UUIDPrimaryKeyModel, TimeStampedModel):
    """Is this patient actively in a monitoring program, and since when.

    Deliberately patient-scoped, not pregnancy-scoped: enrollment history
    should survive across her multiple pregnancies, and a future non-
    pregnancy program (e.g. a CCM-style postpartum offering) wouldn't need
    Pregnancy touched at all. Kept even though MomCare has exactly one
    program today, specifically so adding a second one is a new
    ``program_code`` choice, not a schema redesign — see design doc
    Decision 2 (revised).

    The internal code ``"rpm"`` matches the reference implementation and the
    underlying US billing-code family (Remote Patient Monitoring) this kind
    of program is generally called; the display label is deliberately
    plainer and carries no US-billing assumption, since MomCare is a global
    product.
    """

    PROGRAM_RPM = "rpm"
    PROGRAM_CHOICES = [
        (PROGRAM_RPM, "Maternal Monitoring"),
    ]

    STATUS_ENROLLED = "enrolled"
    STATUS_PAUSED = "paused"
    STATUS_DISCHARGED = "discharged"
    STATUS_CHOICES = [
        (STATUS_ENROLLED, "Enrolled"),
        (STATUS_PAUSED, "Paused"),
        (STATUS_DISCHARGED, "Discharged"),
    ]

    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.PROTECT,
        related_name="program_enrollments",
    )
    program_code = models.CharField(max_length=20, choices=PROGRAM_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ENROLLED)
    enrolled_at = models.DateField()
    disenrolled_at = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-enrolled_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["patient", "program_code"],
                condition=models.Q(disenrolled_at__isnull=True),
                name="one_open_enrollment_per_program",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.patient.full_name} — {self.get_program_code_display()} ({self.status})"
```

- [ ] **Step 4: Add the service function**

In `momcare_platform/core/patients/services.py`, add the import and function:

```python
from momcare_platform.core.patients.models import (
    Consent,
    Patient,
    PatientProgramEnrollment,
    Pregnancy,
    PregnancyRiskFactors,
)
```

(replacing the existing narrower import at the top of the file), and append at the end of the
file:

```python
def open_program_enrollment(
    *,
    patient: Patient,
    program_code: str,
    enrolled_at=None,
) -> PatientProgramEnrollment:
    """Open an enrollment, or return the already-open one — idempotent, same
    as the reference implementation's own enrollment function: calling this
    twice with no discharge in between must not error or duplicate.
    """
    existing = PatientProgramEnrollment.objects.filter(
        patient=patient,
        program_code=program_code,
        disenrolled_at__isnull=True,
    ).first()
    if existing:
        return existing

    return PatientProgramEnrollment.objects.create(
        patient=patient,
        program_code=program_code,
        enrolled_at=enrolled_at or timezone.now().date(),
    )
```

- [ ] **Step 5: Write the migration**

Create `momcare_platform/core/patients/migrations/0010_patientprogramenrollment.py`:

```python
import uuid

from django.db import migrations, models
import django.db.models.deletion


def backfill_rpm_enrollment_for_existing_patients(apps, schema_editor):
    """Existing patients (created before this table existed) get a
    backfilled open "rpm" enrollment, dated to when they were created — see
    design doc's migration notes: pre-existing records must not be left
    without one."""
    Patient = apps.get_model("patients", "Patient")
    PatientProgramEnrollment = apps.get_model("patients", "PatientProgramEnrollment")
    for patient in Patient.objects.all().iterator():
        PatientProgramEnrollment.objects.create(
            patient=patient,
            program_code="rpm",
            status="enrolled",
            enrolled_at=patient.created_at.date(),
        )


class Migration(migrations.Migration):
    dependencies = [
        ("patients", "0009_delete_careteammembership"),
    ]

    operations = [
        migrations.CreateModel(
            name="PatientProgramEnrollment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "program_code",
                    models.CharField(choices=[("rpm", "Maternal Monitoring")], max_length=20),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("enrolled", "Enrolled"), ("paused", "Paused"), ("discharged", "Discharged")],
                        default="enrolled",
                        max_length=20,
                    ),
                ),
                ("enrolled_at", models.DateField()),
                ("disenrolled_at", models.DateField(blank=True, null=True)),
                (
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="program_enrollments",
                        to="patients.patient",
                    ),
                ),
            ],
            options={"ordering": ["-enrolled_at"]},
        ),
        migrations.AddConstraint(
            model_name="patientprogramenrollment",
            constraint=models.UniqueConstraint(
                condition=models.Q(("disenrolled_at__isnull", True)),
                fields=("patient", "program_code"),
                name="one_open_enrollment_per_program",
            ),
        ),
        migrations.RunPython(backfill_rpm_enrollment_for_existing_patients, migrations.RunPython.noop),
    ]
```

- [ ] **Step 6: Add Row-Level Security for the new table**

This new table needs RLS, the same as every other tenant-owned table in this project
(`patients_patient` etc. already have it via `organization/migrations/0006_row_level_security.py`,
and `patients_careteammembership` had it via `0007_care_team_row_level_security.py` before Task 3
deleted that table). Create
`momcare_platform/core/organization/migrations/0008_program_enrollment_row_level_security.py`:

```python
"""Row-Level Security for PatientProgramEnrollment -- the same second layer
every other tenant-owned table already has. Same fail-closed design, same
bypass paths as 0006/0007 -- see 0006's own docstring for the full
explanation of why NULLIF/SET LOCAL/FORCE are each necessary.
"""

from django.db import migrations

_TABLE = "patients_patientprogramenrollment"

_USING = """EXISTS (
    SELECT 1 FROM patients_patient p
    JOIN locations_location l ON l.id = p.location_id
    WHERE p.id = patients_patientprogramenrollment.patient_id
      AND l.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
)"""

_BYPASS = "current_setting('app.rls_bypass', true) = 'on'"


def _enable_sql() -> str:
    condition = f"({_USING}) OR {_BYPASS}"
    return "\n".join(
        [
            f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY;",
            f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY;",
            f"CREATE POLICY tenant_isolation ON {_TABLE} FOR ALL USING ({condition});",
        ],
    )


def _disable_sql() -> str:
    return "\n".join(
        [
            f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE};",
            f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY;",
            f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY;",
        ],
    )


class Migration(migrations.Migration):
    dependencies = [
        ("organization", "0007_care_team_row_level_security"),
        ("patients", "0010_patientprogramenrollment"),
    ]

    operations = [
        migrations.RunSQL(sql=_enable_sql(), reverse_sql=_disable_sql()),
    ]
```

- [ ] **Step 7: Register in admin**

In `momcare_platform/core/patients/admin.py`, add the import and a simple admin:

```python
from momcare_platform.core.patients.models import (
    ClinicalNote,
    Consent,
    Patient,
    PatientProgramEnrollment,
    Pregnancy,
    PregnancyRiskFactors,
)
```

```python
@admin.register(PatientProgramEnrollment)
class PatientProgramEnrollmentAdmin(admin.ModelAdmin):
    list_display = ["patient", "program_code", "status", "enrolled_at", "disenrolled_at"]
    list_filter = ["program_code", "status"]
    search_fields = ["patient__mrn", "patient__first_name", "patient__last_name"]
    readonly_fields = ["created_at", "updated_at"]
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest momcare_platform/core/patients/tests/test_program_enrollment.py -v`
Expected: PASS.

- [ ] **Step 9: Run migrations and full suite, then commit**

Run: `uv run python manage.py migrate`
Run: `uv run pytest momcare_platform/core -q`

```bash
git add momcare_platform/core/patients/models.py \
        momcare_platform/core/patients/services.py \
        momcare_platform/core/patients/admin.py \
        momcare_platform/core/patients/migrations/0010_patientprogramenrollment.py \
        momcare_platform/core/patients/tests/test_program_enrollment.py \
        momcare_platform/core/organization/migrations/0008_program_enrollment_row_level_security.py
git commit -m "feat(patients): reinstate PatientProgramEnrollment scoped to the rpm program"
```

---

### Task 5: Update `Staff.current_patient_count` for the new FK shape

**Files:**
- Modify: `momcare_platform/core/staff/models.py:72-93`
- Test: `momcare_platform/core/staff/tests/api/test_staff_access.py` or a new
  `momcare_platform/core/staff/tests/test_capacity.py` (new — check first whether a capacity
  test already exists with `grep -rl current_patient_count momcare_platform/core/staff/tests`;
  if one exists, extend it there instead of creating a new file)

**Interfaces:**
- Consumes: `Pregnancy.STATUS_ACTIVE`, `Pregnancy.assigned_pregnancies` /
  `.nursed_pregnancies` / `.care_managed_pregnancies` reverse accessors from Task 2.
- Produces: `Staff.current_patient_count` (unchanged signature/behavior — same property, new
  implementation), `Staff.has_capacity` (unchanged).

- [ ] **Step 1: Write the failing test**

Check for an existing capacity test first:

Run: `grep -rl current_patient_count momcare_platform/core/staff/tests`

If found, open that file and add the case below alongside the existing tests. If not found,
create `momcare_platform/core/staff/tests/test_capacity.py`:

```python
"""Staff.current_patient_count — after CareTeamMembership was retired in
favor of direct provider/nurse/care_manager FKs (see
docs/design/2026-09-15-patient-onboarding-design.md, Decision 3), this counts
across all three roles a staff member can hold, not just the lead."""

import pytest
from django.conf import settings

from momcare_platform.core.patients.models import Consent
from momcare_platform.core.patients.services import enrol_patient

pytestmark = pytest.mark.django_db


def test_current_patient_count_includes_nurse_and_care_manager_roles(make_hospital, make_staff):
    hospital = make_hospital("Capacity Count Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@capacitycount.test")

    enrol_patient(
        organization=hospital.org,
        recorded_by=hospital.admin,
        patient_data={"first_name": "Ayesha"},
        consent={"status": Consent.STATUS_GRANTED},
        pregnancy_data={"lmp": "2026-01-01", "nurse": nurse.staff},
    )

    assert nurse.staff.current_patient_count == 1


def test_current_patient_count_does_not_double_count_one_patient_in_two_roles(make_hospital, make_staff):
    """The same staff member as both provider and nurse on one pregnancy
    (unusual, but not forbidden) counts once, not twice."""
    hospital = make_hospital("No Double Count Hospital")
    staff_member = make_staff(hospital.org, settings.ROLE_PROVIDER, "dual@nodoublecount.test")

    enrol_patient(
        organization=hospital.org,
        recorded_by=hospital.admin,
        patient_data={"first_name": "Ayesha"},
        consent={"status": Consent.STATUS_GRANTED},
        pregnancy_data={"lmp": "2026-01-01", "provider": staff_member.staff},
    )

    assert staff_member.staff.current_patient_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest momcare_platform/core/staff/tests/test_capacity.py -v`
Expected: FAIL — `enrol_patient`'s `pregnancy_data={"nurse": ...}` currently raises because
`Pregnancy` doesn't accept `nurse` before Task 2 lands (it will by the time this task runs,
since tasks are sequential) — but `current_patient_count` still reads the deleted
`care_team_memberships` relation and will raise `AttributeError` or return 0 incorrectly.

- [ ] **Step 3: Rewrite `current_patient_count`**

Replace `momcare_platform/core/staff/models.py:72-93`:

```python
    @property
    def current_patient_count(self) -> int:
        """Active caseload: patients whose pregnancy this staff member leads
        (``provider``), is the ``nurse`` for, or is the ``care_manager`` for —
        restricted to pregnancies still ``STATUS_ACTIVE`` and patients still
        active. Computed at runtime, never stored (blueprint §9).
        """
        from momcare_platform.core.patients.models import Pregnancy  # noqa: PLC0415

        active_patient = {"status": Pregnancy.STATUS_ACTIVE, "patient__is_active": True}
        provider_ids = self.assigned_pregnancies.filter(**active_patient).values_list("patient_id", flat=True)
        nurse_ids = self.nursed_pregnancies.filter(**active_patient).values_list("patient_id", flat=True)
        care_manager_ids = self.care_managed_pregnancies.filter(**active_patient).values_list(
            "patient_id",
            flat=True,
        )
        return len(set(provider_ids) | set(nurse_ids) | set(care_manager_ids))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest momcare_platform/core/staff/tests/test_capacity.py -v`
Expected: PASS.

- [ ] **Step 5: Run full staff suite, then commit**

Run: `uv run pytest momcare_platform/core/staff -q`

```bash
git add momcare_platform/core/staff/models.py momcare_platform/core/staff/tests/test_capacity.py
git commit -m "fix(staff): current_patient_count reads provider/nurse/care_manager, not the retired join table"
```

---

### Task 6: Update `scope_to_assigned_staff` for the new FK shape

**Files:**
- Modify: `momcare_platform/core/common/scoping.py:132-183`
- Test: `momcare_platform/core/patients/tests/api/test_patients.py` (append — the `?assigned_to=me`
  case; check first with `grep -rn assigned_to=me momcare_platform/core` for the existing test(s)
  to extend rather than duplicate)

**Interfaces:**
- Produces: `scope_to_assigned_staff(queryset, request, *, path_prefix="")` — same signature,
  simpler implementation (direct FK filter per role, no more `CareTeamMembership` join, no more
  `.distinct()` needed since there's no join-table fan-out).

- [ ] **Step 1: Write the failing test**

Run: `grep -rln "assigned_to.*me\|assigned_to=me" momcare_platform/core/patients/tests momcare_platform/core/alerts/tests`

Open whichever file(s) that finds. Add (or adapt an existing test to cover) this case —
append to `momcare_platform/core/patients/tests/api/test_patients.py`:

```python
def test_assigned_to_me_returns_patients_where_caller_is_nurse_or_care_manager(
    client,
    make_hospital,
    make_staff,
    auth,
):
    hospital = make_hospital("Assigned To Me Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@assignedtome.test")
    other_nurse = make_staff(hospital.org, settings.ROLE_NURSE, "other@assignedtome.test")

    post_patient(
        client,
        auth(hospital.admin.email),
        first_name="Mine",
        pregnancy={"lmp": "2026-01-01", "nurse": str(nurse.staff.id)},
    )
    post_patient(
        client,
        auth(hospital.admin.email),
        first_name="NotMine",
        pregnancy={"lmp": "2026-01-01", "nurse": str(other_nurse.staff.id)},
    )

    response = client.get(f"{PATIENTS}?assigned_to=me", **auth("nurse@assignedtome.test"))

    names = [row["full_name"] for row in response.json()["results"]]
    assert names == ["Mine Bibi"]
```

`settings` is already imported at the top of this test file (line 11).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest momcare_platform/core/patients/tests/api/test_patients.py -k assigned_to_me -v`
Expected: FAIL — `scope_to_assigned_staff` still filters on `care_team_memberships`, which no
longer exists.

- [ ] **Step 3: Rewrite `scope_to_assigned_staff`**

Replace `momcare_platform/core/common/scoping.py:132-183`:

```python
def scope_to_assigned_staff(queryset, request, *, path_prefix: str = ""):
    """``?assigned_to=me`` — a clinician's honest "my patients"/"my alerts"/
    "my worklist", not the whole hospital wearing that label.

    Shared by ``PatientListCreateView``, ``AlertListView`` and the worklist
    view rather than reimplemented per view.

    ``path_prefix`` is the ORM path from ``queryset``'s model to
    ``Pregnancy``: empty when the queryset already *is* Pregnancy (the
    worklist), ``"pregnancy__"`` for a model with a direct FK to it
    (``Alert``), or ``"pregnancies__"`` for Patient's reverse FK.

    Each role maps to exactly one direct field on Pregnancy now that
    CareTeamMembership has been retired in favor of provider/nurse/
    care_manager — see docs/design/2026-09-15-patient-onboarding-design.md,
    Decision 3.
    """
    if request.query_params.get("assigned_to") != "me":
        return queryset

    staff = getattr(request.user, "staff", None)
    if staff is None:
        return queryset.none()

    role = request.user.role_code
    if role not in ("provider", "nurse", "care_manager"):
        # hospital_admin and anyone else: "my X" isn't a concept that
        # applies to them - an empty, honest result rather than silently
        # ignoring the param and returning everyone under a label that
        # would be wrong for this role.
        return queryset.none()

    return queryset.filter(**{f"{path_prefix}{role}": staff})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest momcare_platform/core/patients/tests/api/test_patients.py -k assigned_to_me -v`
Expected: PASS.

- [ ] **Step 5: Run patients + alerts suites, then commit**

Run: `uv run pytest momcare_platform/core/patients momcare_platform/core/alerts -q`
(Alerts tests using `?assigned_to=me` are expected to still fail here if they reference
`CareTeamMembership` directly in fixtures — that's Task 7's job, not this one; confirm the
*scoping logic itself* works via the patients-app test above.)

```bash
git add momcare_platform/core/common/scoping.py momcare_platform/core/patients/tests/api/test_patients.py
git commit -m "fix(scoping): scope_to_assigned_staff reads provider/nurse/care_manager directly"
```

---

### Task 7: Rename `assigned_staff` → `provider` in `alerts`

**Files:**
- Modify: `momcare_platform/core/alerts/services.py:47`
- Modify: `momcare_platform/core/alerts/api/views.py:51`
- Modify: `momcare_platform/core/alerts/api/serializers.py:43,77-86`
- Modify: `momcare_platform/core/alerts/tests/api/test_alerts.py` (fixture + `CareTeamMembership` usages)

**Interfaces:**
- Consumes: `Pregnancy.provider` (Task 2), `Pregnancy.nurse`/`Pregnancy.care_manager` (Task 2).

- [ ] **Step 1: Update `alerts/services.py`**

Line 47, `recipients_for_tier`:

```python
    if tier == escalation.TIER_CLINICIAN:
        staff = alert.pregnancy.provider
```

Lines 312 and 332, `select_related("pregnancy__assigned_staff__user", ...)` →
`select_related("pregnancy__provider__user", ...)` (both occurrences).

- [ ] **Step 2: Update `alerts/api/views.py`**

Line 51: `"pregnancy__assigned_staff__user"` → `"pregnancy__provider__user"`.

- [ ] **Step 3: Update `alerts/api/serializers.py`**

Line 43: `assigned_staff_name = serializers.SerializerMethodField()` →
`provider_name = serializers.SerializerMethodField()`.

Line 73 (in `Meta.fields`): `"assigned_staff_name"` → `"provider_name"`.

Lines 77-86:

```python
    def get_provider_name(self, obj) -> str:
        """Empty when nobody is responsible — which the interface must show.

        A soft-deleted clinician still leaves the foreign key populated, so an
        inactive one is reported as no clinician rather than as cover.
        """
        staff = obj.pregnancy.provider
        if staff and staff.is_active and staff.user:
            return staff.user.get_full_name()
        return ""
```

- [ ] **Step 4: Update the test fixture and `CareTeamMembership` usages**

In `momcare_platform/core/alerts/tests/api/test_alerts.py`:

Remove the `CareTeamMembership` import (line 21) — it's no longer needed anywhere in this file.

Lines 37-43 (the `pregnancy_for` fixture body):

```python
        pregnancy = patient.current_pregnancy
        if clinician is not None:
            pregnancy.provider = clinician.staff
            pregnancy.save(update_fields=["provider", "updated_at"])
```

Replace `test_a_providers_assigned_to_me_includes_lead_and_co_provider_cases` (lines 451-482) in
full — its premise (a "co-provider" distinct from the lead, via a `CareTeamMembership` row) no
longer exists once `provider` is a single direct slot:

```python
def test_a_providers_assigned_to_me_includes_their_own_case_only(
    client,
    make_hospital,
    make_staff,
    pregnancy_for,
    auth,
):
    """Each provider only sees the pregnancy where they hold the provider
    slot — there is no longer a separate "co-provider" concept once
    CareTeamMembership was retired in favor of a single provider FK."""
    hospital = make_hospital("Provider Alerts Hospital")
    lead = make_staff(hospital.org, settings.ROLE_PROVIDER, "lead@provideralerts.test")
    bystander = make_staff(hospital.org, settings.ROLE_PROVIDER, "bystander@provideralerts.test")

    lead_case = pregnancy_for(hospital, first_name="Lead", clinician=lead)
    go_critical(lead_case)

    body = client.get(f"{ALERTS}?assigned_to=me", **auth(lead.email)).json()
    assert body["count"] == 1
    assert body["results"][0]["patient_name"] == "Lead Bibi"

    body = client.get(f"{ALERTS}?assigned_to=me", **auth(bystander.email)).json()
    assert body["count"] == 0
```

Replace `test_a_nurses_assigned_to_me_is_membership_only` (lines 485-501) in full:

```python
def test_a_nurses_assigned_to_me_sees_their_assigned_case(
    client,
    make_hospital,
    make_staff,
    pregnancy_for,
    auth,
):
    hospital = make_hospital("Nurse Alerts Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@nursealerts.test")
    pregnancy = pregnancy_for(hospital)
    pregnancy.nurse = nurse.staff
    pregnancy.save(update_fields=["nurse", "updated_at"])
    go_critical(pregnancy)

    body = client.get(f"{ALERTS}?assigned_to=me", **auth(nurse.email)).json()

    assert body["count"] == 1
```

Replace `test_an_ended_membership_no_longer_surfaces_the_alert` (lines 503-523) in full — "ending
a membership" is now "un-assigning the direct FK":

```python
def test_unassigning_a_nurse_no_longer_surfaces_the_alert(
    client,
    make_hospital,
    make_staff,
    pregnancy_for,
    auth,
):
    hospital = make_hospital("Ended Alerts Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@endedalerts.test")
    pregnancy = pregnancy_for(hospital)
    pregnancy.nurse = nurse.staff
    pregnancy.save(update_fields=["nurse", "updated_at"])
    go_critical(pregnancy)

    pregnancy.nurse = None
    pregnancy.save(update_fields=["nurse", "updated_at"])

    body = client.get(f"{ALERTS}?assigned_to=me", **auth(nurse.email)).json()

    assert body["count"] == 0
```

In `test_assigned_to_me_removed_would_leak_everyones_alerts` (lines 543-563+), replace lines
555-557:

```python
    mine = pregnancy_for(hospital, first_name="Mine")
    mine.nurse = nurse.staff
    mine.save(update_fields=["nurse", "updated_at"])
    go_critical(mine)
```

(the rest of that test — the unfiltered-vs-filtered comparison — is unchanged.)

- [ ] **Step 5: Run the alerts suite**

Run: `uv run pytest momcare_platform/core/alerts -q`
Expected: PASS. If the co-provider test rewrite in Step 4 needs more context than the excerpt
above provided, read the full test function before finalizing — do not guess at assertions.

- [ ] **Step 6: Commit**

```bash
git add momcare_platform/core/alerts/services.py \
        momcare_platform/core/alerts/api/views.py \
        momcare_platform/core/alerts/api/serializers.py \
        momcare_platform/core/alerts/tests/api/test_alerts.py
git commit -m "refactor(alerts): rename assigned_staff to provider, update care-team fixtures"
```

---

### Task 8: Rename `assigned_staff` → `provider` in `monitoring`

**Files:**
- Modify: `momcare_platform/core/monitoring/api/views.py:272,293-295`
- Modify: `momcare_platform/core/monitoring/api/serializers.py:307`
- Modify: `momcare_platform/core/monitoring/tests/test_reassess_risk.py:86-87`
- Modify: `momcare_platform/core/monitoring/tests/api/test_risk.py:52-53`

- [ ] **Step 1: Update `monitoring/api/views.py`**

Line 272: `.select_related("patient", "assigned_staff__user")` →
`.select_related("patient", "provider__user")`.

Lines 293-295:

```python
                    "provider_name": (
                        pregnancy.provider.user.get_full_name() if pregnancy.provider_id else ""
                    ),
```

- [ ] **Step 2: Update `monitoring/api/serializers.py`**

Line 307: `assigned_staff_name = serializers.CharField(allow_blank=True)` →
`provider_name = serializers.CharField(allow_blank=True)`.

- [ ] **Step 3: Update the two test fixtures**

In both `momcare_platform/core/monitoring/tests/test_reassess_risk.py:86-87` and
`momcare_platform/core/monitoring/tests/api/test_risk.py:52-53`:

```python
        if clinician is not None:
            pregnancy.provider = clinician.staff
            pregnancy.save(update_fields=["provider", "updated_at"])
```

- [ ] **Step 4: Run the monitoring suite**

Run: `uv run pytest momcare_platform/core/monitoring -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add momcare_platform/core/monitoring/api/views.py \
        momcare_platform/core/monitoring/api/serializers.py \
        momcare_platform/core/monitoring/tests/test_reassess_risk.py \
        momcare_platform/core/monitoring/tests/api/test_risk.py
git commit -m "refactor(monitoring): rename assigned_staff to provider"
```

---

### Task 9: Care-team role/org/capacity validation

**Files:**
- Modify: `momcare_platform/core/patients/api/serializers.py` (`PregnancyWriteSerializer.validate`)
- Test: `momcare_platform/core/patients/tests/api/test_patients.py` (append)

**Interfaces:**
- Consumes: `settings.ROLE_PROVIDER`/`ROLE_NURSE`/`ROLE_CARE_MANAGER`, `Staff.has_capacity`
  (Task 5), `OrganizationStaffField` (unchanged — already restricts to same-org active staff,
  which is the "org match" rule; this task adds the "role match" and "capacity" rules on top).

This is the validation described in design doc Decision 4 — role match, org match (already
enforced structurally by `OrganizationStaffField`), capacity. Runs identically whether triggered
from onboarding (nested inside `PatientCreateSerializer.pregnancy`), opening a new pregnancy
episode, or a later `PATCH` on an existing pregnancy — because all three already go through this
one `PregnancyWriteSerializer`.

- [ ] **Step 1: Write the failing tests**

Append to `momcare_platform/core/patients/tests/api/test_patients.py`:

```python
def test_assigning_a_nurse_to_the_provider_slot_is_rejected(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Role Mismatch Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@rolemismatch.test")

    response = post_patient(
        client,
        auth(hospital.admin.email),
        pregnancy={"lmp": "2026-01-01", "provider": str(nurse.staff.id)},
    )

    assert response.status_code == 400
    assert "provider" in response.json()["pregnancy"]


def test_assigning_a_staff_member_already_at_capacity_is_rejected(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Over Capacity Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@overcapacity.test")
    nurse.staff.max_patients = 1
    nurse.staff.save(update_fields=["max_patients"])
    post_patient(
        client,
        auth(hospital.admin.email),
        first_name="First",
        pregnancy={"lmp": "2026-01-01", "nurse": str(nurse.staff.id)},
    )

    response = post_patient(
        client,
        auth(hospital.admin.email),
        first_name="Second",
        pregnancy={"lmp": "2026-01-01", "nurse": str(nurse.staff.id)},
    )

    assert response.status_code == 400
    assert "nurse" in response.json()["pregnancy"]


def test_reassigning_the_same_nurse_on_update_does_not_recheck_capacity(client, make_hospital, make_staff, auth):
    """Idempotent re-save of an unchanged assignment must not fail even when
    the staff member is now at capacity from other patients."""
    hospital = make_hospital("Idempotent Reassign Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@idempotentreassign.test")
    nurse.staff.max_patients = 1
    nurse.staff.save(update_fields=["max_patients"])
    created = post_patient(
        client,
        auth(hospital.admin.email),
        pregnancy={"lmp": "2026-01-01", "nurse": str(nurse.staff.id)},
    ).json()
    patient_id = created["id"]
    pregnancy_id = created["current_pregnancy"]["id"]

    response = client.patch(
        f"/api/patients/{patient_id}/pregnancies/{pregnancy_id}/",
        data=json.dumps({"notes": "unchanged nurse, just adding a note"}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest momcare_platform/core/patients/tests/api/test_patients.py -k "role_mismatch or over_capacity or idempotent_reassign" -v`
Expected: FAIL — nothing currently checks role or capacity.

- [ ] **Step 3: Add the validation to `PregnancyWriteSerializer`**

In `momcare_platform/core/patients/api/serializers.py`, add imports at the top:

```python
from django.conf import settings
from django.db import transaction
```

(`Staff` is already imported at line 11.) Then extend `PregnancyWriteSerializer.validate`
(currently inherited unchanged from `PregnancySerializer` — override it):

```python
class PregnancyWriteSerializer(PregnancySerializer):
    """Create/update, allowing risk factors to be set alongside the pregnancy."""

    risk_factors = PregnancyRiskFactorsSerializer(required=False)
    provider = OrganizationStaffField(required=False, allow_null=True)
    nurse = OrganizationStaffField(required=False, allow_null=True)
    care_manager = OrganizationStaffField(required=False, allow_null=True)

    ROLE_FOR_FIELD = {
        "provider": settings.ROLE_PROVIDER,
        "nurse": settings.ROLE_NURSE,
        "care_manager": settings.ROLE_CARE_MANAGER,
    }

    class Meta(PregnancySerializer.Meta):
        read_only_fields = [f for f in PregnancySerializer.Meta.read_only_fields if f != "risk_factors"]

    def validate(self, attrs):
        attrs = super().validate(attrs)

        errors = {}
        for field_name, role_code in self.ROLE_FOR_FIELD.items():
            if field_name not in attrs:
                continue
            staff = attrs[field_name]
            if staff is None:
                continue

            already_assigned = (
                self.instance is not None
                and getattr(
                    self.instance,
                    f"{field_name}_id",
                    None,
                )
                == staff.id
            )
            if already_assigned:
                continue

            if staff.user.role_code != role_code:
                errors[field_name] = f"This staff member is not a {role_code}."
                continue

            with transaction.atomic():
                locked = Staff.objects.select_for_update().get(pk=staff.pk)
                if not locked.has_capacity:
                    errors[field_name] = "This staff member is already at their patient capacity."

        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def update(self, instance, validated_data):
        factors = validated_data.pop("risk_factors", None)
        pregnancy = super().update(instance, validated_data)
        if factors:
            PregnancyRiskFactors.objects.update_or_create(pregnancy=pregnancy, defaults=factors)
        return pregnancy
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest momcare_platform/core/patients/tests/api/test_patients.py -k "role_mismatch or over_capacity or idempotent_reassign" -v`
Expected: PASS.

- [ ] **Step 5: Run full patients suite, then commit**

Run: `uv run pytest momcare_platform/core/patients -q`

```bash
git add momcare_platform/core/patients/api/serializers.py momcare_platform/core/patients/tests/api/test_patients.py
git commit -m "feat(patients): validate care-team role match and staff capacity on assignment"
```

---

### Task 10: Rename `enrol_patient` → `onboard_patient`, wire `program_enrollments`

**Files:**
- Modify: `momcare_platform/core/patients/services.py` (rename function + exception, add
  `program_enrollments` handling)
- Modify: `momcare_platform/core/patients/api/serializers.py` (`PatientCreateSerializer`,
  `PatientDetailSerializer`, new `PatientProgramEnrollmentSerializer`/
  `ProgramEnrollmentInputSerializer`)
- Modify: `momcare_platform/core/patients/api/views.py` (call site + exception name)
- Test: `momcare_platform/core/patients/tests/api/test_patients.py` (append)

**Interfaces:**
- Produces: `onboard_patient(*, organization, recorded_by, patient_data, pregnancy_data=None,
  risk_factor_data=None, consent=None, program_enrollments=None) -> Patient` (replaces
  `enrol_patient`), `OnboardingError` (replaces `EnrolmentError`).

- [ ] **Step 1: Write the failing test**

Append to `momcare_platform/core/patients/tests/api/test_patients.py`:

```python
def test_onboarding_opens_the_rpm_program_enrollment(client, make_hospital, auth):
    hospital = make_hospital("Program Enrollment Onboarding Hospital")

    response = post_patient(
        client,
        auth(hospital.admin.email),
        program_enrollments=[{"program_code": "rpm"}],
    )

    assert response.status_code == 201
    patient = Patient.objects.get(id=response.json()["id"])
    enrollment = patient.program_enrollments.get()
    assert enrollment.program_code == "rpm"
    assert enrollment.disenrolled_at is None


def test_onboarding_without_program_enrollments_creates_none(client, make_hospital, auth):
    """program_enrollments is optional — omitting it must not error, and must
    not silently open an enrollment nobody asked for."""
    hospital = make_hospital("No Program Onboarding Hospital")

    response = post_patient(client, auth(hospital.admin.email))

    assert response.status_code == 201
    patient = Patient.objects.get(id=response.json()["id"])
    assert not patient.program_enrollments.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest momcare_platform/core/patients/tests/api/test_patients.py -k program_enrollment -v`
Expected: FAIL — `PatientCreateSerializer` has no `program_enrollments` field.

- [ ] **Step 3: Rename the service function and exception**

In `momcare_platform/core/patients/services.py`:

```python
class OnboardingError(Exception):
    """Raised when a patient cannot be onboarded; message is safe to show a user."""


@transaction.atomic
def onboard_patient(
    *,
    organization,
    recorded_by,
    patient_data: dict,
    pregnancy_data: dict | None = None,
    risk_factor_data: dict | None = None,
    consent: dict | None = None,
    program_enrollments: list[dict] | None = None,
) -> Patient:
    """Create a patient, optionally her current pregnancy, record consent,
    and open any requested program enrollments.

    One transaction: a patient stored without the consent that authorised
    storing her would be a record nobody agreed to.

    The location comes from the hospital, never from the request, so
    onboarding cannot place a patient inside another tenant.
    """
    location = ensure_default_location(organization)

    patient = None
    for attempt in range(MRN_MAX_ATTEMPTS):
        try:
            with transaction.atomic():
                cnic = patient_data.get("cnic") or None
                patient = Patient.objects.create(
                    location=location,
                    organization=organization,
                    mrn=_candidate_mrn(organization, offset=attempt),
                    **{**patient_data, "cnic": cnic},
                )
            break
        except IntegrityError:
            if attempt == MRN_MAX_ATTEMPTS - 1:
                raise OnboardingError(
                    "Could not allocate a medical record number. Please try again.",
                ) from None

    assert patient is not None

    if consent:
        Consent.objects.create(
            patient=patient,
            status=consent.get("status", Consent.STATUS_GRANTED),
            version=consent.get("version", "v1.0"),
            method=consent.get("method", Consent.METHOD_IN_PERSON),
            note=consent.get("note", ""),
            recorded_by=recorded_by,
        )

    if pregnancy_data:
        create_pregnancy(patient=patient, data=pregnancy_data, risk_factor_data=risk_factor_data)

    for enrollment in program_enrollments or []:
        open_program_enrollment(
            patient=patient,
            program_code=enrollment["program_code"],
            enrolled_at=enrollment.get("enrolled_at"),
        )

    return patient
```

Update `create_pregnancy`'s own `raise EnrolmentError(...)` (line 96) to `raise
OnboardingError(...)`. `open_program_enrollment` (Task 4) must be defined **above**
`onboard_patient` in the file, or referenced only inside the function body (Python resolves
names at call time, so definition order across module-level functions doesn't matter here —
leave `open_program_enrollment` where Task 4 put it, at the end of the file).

- [ ] **Step 4: Add the program-enrollment serializers**

In `momcare_platform/core/patients/api/serializers.py`, add near
`PregnancyRiskFactorsSerializer` (top of file):

```python
class PatientProgramEnrollmentSerializer(serializers.ModelSerializer):
    program_code_display = serializers.CharField(source="get_program_code_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = PatientProgramEnrollment
        fields = [
            "id",
            "program_code",
            "program_code_display",
            "status",
            "status_display",
            "enrolled_at",
            "disenrolled_at",
        ]
        read_only_fields = fields


class ProgramEnrollmentInputSerializer(serializers.Serializer):
    program_code = serializers.ChoiceField(choices=PatientProgramEnrollment.PROGRAM_CHOICES)
    enrolled_at = serializers.DateField(required=False, allow_null=True)
```

Add `PatientProgramEnrollment` to the `momcare_platform.core.patients.models` import at the top
of the file (line 3-10).

- [ ] **Step 5: Wire `program_enrollments` into `PatientCreateSerializer` and expose it on detail**

In `PatientCreateSerializer` (around line 378), add:

```python
    pregnancy = PregnancyWriteSerializer(required=False)
    consent = ConsentInputSerializer()
    program_enrollments = ProgramEnrollmentInputSerializer(many=True, required=False)
```

Update `split()` (lines 394-399) to also return the program enrollments:

```python
    def split(self) -> tuple[dict, dict | None, dict | None, dict, list[dict]]:
        data = self.validated_data
        patient_data = {k: v for k, v in data.items() if k in self.PATIENT_FIELDS}
        pregnancy = data.get("pregnancy")
        risk_factors = pregnancy.pop("risk_factors", None) if pregnancy else None
        return patient_data, pregnancy, risk_factors, data["consent"], data.get("program_enrollments", [])
```

In `PatientDetailSerializer` (line 305-346), add:

```python
    program_enrollments = PatientProgramEnrollmentSerializer(many=True, read_only=True)
```

and add `"program_enrollments"` to both `Meta.fields` and `Meta.read_only_fields`.

- [ ] **Step 6: Update the view**

In `momcare_platform/core/patients/api/views.py`:

Update the import (lines 27-42) — replace `PatientCreateSerializer` usage's downstream call,
and replace `EnrolmentError`, `enrol_patient` with `OnboardingError`, `onboard_patient` in the
import at line 42.

`PatientListCreateView.post` (lines 149-172):

```python
    def post(self, request):
        org, error = self.hospital_or_error(request)
        if error:
            return error

        serializer = PatientCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        patient_data, pregnancy_data, risk_factors, consent, program_enrollments = serializer.split()

        try:
            patient = onboard_patient(
                organization=org,
                recorded_by=request.user,
                patient_data=patient_data,
                pregnancy_data=pregnancy_data,
                risk_factor_data=risk_factors,
                consent=consent,
                program_enrollments=program_enrollments,
            )
        except OnboardingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(PatientDetailSerializer(patient).data, status=status.HTTP_201_CREATED)
```

`PregnancyListCreateView.post` (lines 349-367) — replace `EnrolmentError` with `OnboardingError`
in its `except` clause (the `create_pregnancy` call itself is unchanged).

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest momcare_platform/core/patients/tests/api/test_patients.py -v`
Expected: PASS — including every earlier test in this file (all of Tasks 1, 2, 6, 9's additions).

- [ ] **Step 8: Run the full suite, then commit**

Run: `uv run pytest momcare_platform/core -q`
Expected: all pass **except** the tests Task 11 will fix (any remaining `assigned_staff`/
`CareTeamMembership`/`enrol_patient`/`EnrolmentError` references outside what's already been
touched).

```bash
git add momcare_platform/core/patients/services.py \
        momcare_platform/core/patients/api/serializers.py \
        momcare_platform/core/patients/api/views.py \
        momcare_platform/core/patients/tests/api/test_patients.py
git commit -m "feat(patients): rename enrol_patient to onboard_patient, wire program_enrollments"
```

---

### Task 11: Fix remaining references in existing tests

**Files:**
- Modify: `momcare_platform/core/patients/tests/api/test_worklist.py`
- Modify: `momcare_platform/core/staff/tests/api/test_staff_lifecycle.py`
- Modify: any other file still failing (found via Step 1 below)

**Interfaces:** none new — this task only fixes call sites broken by Tasks 1-10's renames.

- [ ] **Step 1: Find every remaining reference**

Run: `grep -rln "enrol_patient\|EnrolmentError\|assigned_staff\|CareTeamMembership" momcare_platform/core --include=*.py`

Expected remaining hits at this point: `patients/tests/api/test_worklist.py`,
`staff/tests/api/test_staff_lifecycle.py`, and possibly `patients/services.py`'s own
docstrings/comments (harmless, but fix wording if found) — confirm the exact list before
proceeding, since earlier tasks may have already caught some of these.

- [ ] **Step 2: Fix `patients/tests/api/test_worklist.py`**

Every occurrence of `pregnancy_data={"..., "assigned_staff": X}` → `"provider": X`, and
`pregnancy.assigned_staff = clinician.staff` / `pregnancy.save(update_fields=["assigned_staff", ...])`
→ `pregnancy.provider = clinician.staff` / `pregnancy.save(update_fields=["provider", ...])`
(this is the same `pregnancy_for` fixture pattern already fixed in Tasks 7/8 for other apps —
apply identically here, lines 37-53 and every `assigned_staff=` keyword argument at call sites
throughout the file, e.g. lines 232, 249, 261, 298, 340, 350, 376).

- [ ] **Step 3: Fix `staff/tests/api/test_staff_lifecycle.py`**

- Remove the `CareTeamMembership` import (line 16).
- Lines 210, 230, 249, 354: `pregnancy_data={..., "assigned_staff": nurse.staff}` →
  `"provider": nurse.staff}` (or `"nurse":` if the test's intent is specifically about the nurse
  role — read each call site's surrounding test name/assertions first; the grep excerpt earlier
  showed these all used `assigned_staff`, meaning they were testing the *lead* clinician
  specifically, so rename to `provider`, not `nurse`).
- Replace `test_deactivate_is_blocked_by_an_active_care_team_membership_too` (lines 240-259) in
  full — its point (deactivation is blocked for a *supporting* role too, not just the lead) still
  holds, just expressed with the direct `nurse` FK instead of a `CareTeamMembership` row:

  ```python
  def test_deactivate_is_blocked_while_assigned_as_nurse_too(client, make_hospital, make_staff, auth):
      hospital = make_hospital("Care Team Block Hospital")
      lead = make_staff(hospital.org, settings.ROLE_PROVIDER, "lead@careteamblock.test")
      supporting = make_staff(hospital.org, settings.ROLE_NURSE, "supporting@careteamblock.test")
      onboard_patient(
          organization=hospital.org,
          recorded_by=hospital.admin,
          patient_data={"first_name": "Ayesha", "last_name": "Bibi"},
          consent={"status": Consent.STATUS_GRANTED},
          pregnancy_data={
              "lmp": datetime.date(2026, 1, 1),
              "provider": lead.staff,
              "nurse": supporting.staff,
          },
      )

      response = post(client, auth(hospital.admin.email), deactivate_url(supporting.staff.id))

      assert response.status_code == 400
  ```
- Line 358: `patient.pregnancies.get().assigned_staff_id` → `.provider_id`.
- Any `enrol_patient` call site in this file → `onboard_patient` (check the import at the top of
  the file too).

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest momcare_platform/core -q`
Expected: **all tests pass.** This is the first point in the plan where the entire suite is
expected to be green again.

- [ ] **Step 5: Confirm nothing was missed**

Run: `grep -rln "enrol_patient\|EnrolmentError\|assigned_staff\|CareTeamMembership\b" momcare_platform/core --include=*.py`
Expected: no output (or only doc/comment references that don't affect behavior — read any hit
found and confirm it's inert prose, not live code, before leaving it).

- [ ] **Step 6: Commit**

```bash
git add momcare_platform/core/patients/tests/api/test_worklist.py \
        momcare_platform/core/staff/tests/api/test_staff_lifecycle.py
git commit -m "test: fix remaining assigned_staff/CareTeamMembership/enrol_patient references"
```

---

### Task 12: Update `CLAUDE.md`'s stale references

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:** none — documentation only.

- [ ] **Step 1: Update the scoping table**

`CLAUDE.md` documents `Pregnancy → patient__location__organization` and doesn't mention
`Patient.organization` directly, or the retired `CareTeamMembership`/renamed
`assigned_staff`/`enrol_patient`. Update:

- The scoping-paths table: note `Patient` now also carries a direct `organization` column
  (used for CNIC uniqueness), even though the scoping lookup itself is unchanged
  (`location__organization` remains correct and is not being replaced).
- Remove or correct any prose mentioning `CareTeamMembership` as additive to
  `Pregnancy.assigned_staff` — it's `provider`/`nurse`/`care_manager` now, and
  `CareTeamMembership` no longer exists.
- Update `enrol_patient` → `onboard_patient` wherever named.

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for the care-team and onboarding rename"
```

---

## Self-Review Notes (for the implementer, not a task)

- Tasks 7/8's exact line numbers for test-file edits were read from `grep -C` excerpts, not full
  file reads — the plan says explicitly, at each such point, to read the full surrounding test
  before editing. Do not skip that reading step; the excerpts may not show a test's full
  assertions.
- Task 3 deliberately does not touch the `organization` app's existing RLS migration
  (`0007_care_team_row_level_security.py`) — dropping the table drops its policy automatically.
  Do not write a migration that tries to `DROP POLICY` before the `DeleteModel` runs; it's
  unnecessary and Django will already have applied `0007` historically in any real database.
