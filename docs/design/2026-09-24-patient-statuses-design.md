# Patient Statuses — Design

**Date:** 2026-09-24
**Status:** Approved for implementation.

## Context

Read from Neuro_RPM's own `GlobalStatus` (`core/organization/models.py`) and `PatientStatus`
(`core/patients/models.py`) — a two-part feature: an admin-managed catalogue of named, colored
status labels, and an append-only log of labels actually applied to a patient. Neuro_RPM's own
UI frames the catalogue as a "System Governance" screen; the log is what a clinician sees on a
patient's own record.

MomCare already tracks status for the two things that turned out to matter clinically —
`Pregnancy.status` (outcome) and `Alert.status` (acknowledgement, with a full `AlertEvent` audit
trail). This feature is different in kind: a **hospital-invented vocabulary** ("Critical",
"Telehealth Connected", "Waiting"...) with no fixed clinical meaning, logged against a patient
over time. It doesn't replace either existing status field.

## Decisions

1. **Free text, decoupled from the catalogue** — matches Neuro_RPM exactly. `PatientStatus` has
   no FK to the catalogue; a staff member can log any name/description/color, whether or not it
   exists in the org's `StatusLabel` list. The catalogue exists to power a frontend picker, not
   to constrain what can be logged.
2. **No "same name once ever" restriction** — Neuro_RPM permanently blocks re-using a status
   name for the same patient (`UniqueConstraint(patient, name)`, forever). Dropped: a pregnancy
   spans months, and a status like "Stable" plausibly recurs. `PatientStatus` has no uniqueness
   constraint at all.
3. **Embedded like Neuro_RPM, on every patient list and detail response** — Neuro_RPM's
   `PatientSerializer.to_representation` unconditionally embeds the patient's *entire* status
   history (`name`/`description`/`color` only, ordered newest-first) into every single patient
   row, in both list and detail views. MomCare replicates this rather than trimming it to just
   the current entry, per explicit instruction — full parity with the reference, at the cost of
   response size on hospitals with a long-lived, heavily-tagged patient.

## Models — both in `core/monitoring` (`clinical_notes` app)

Built by cloning this app's own existing siblings rather than Neuro_RPM's shape line-for-line,
since MomCare already solved the multi-tenant version of both problems.

### `StatusLabel` — cloned from `ClinicalTag`

| Field | Notes |
|---|---|
| `organization` FK (nullable) XOR `location` FK (nullable) | Same `CheckConstraint` + two `UniqueConstraint`s as `ClinicalTag` |
| `name` | required |
| `description` | blank-allowed, matches Neuro_RPM's `GlobalStatus.description` |
| `color` | hex, reuses `HEX_COLOR_VALIDATOR` already in `monitoring/models.py`, nullable (matches `ClinicalTag.color`'s own null=True convention) |

- **Copy-to-location**: new signal `copy_org_status_labels_to_new_location`, byte-identical
  pattern to the existing `copy_org_clinical_tags_to_new_location` in `monitoring/signals.py`
  — fork-once at Location creation, independent rows after.
- **Views**: `StatusLabelListCreateView` / `StatusLabelDetailView`, flat `APIView`s, same shape
  as `ClinicalTagListCreateView`/`ClinicalTagDetailView`.
- **Permissions**: read = any `IsHospitalStaff`; write (create/edit/delete) = hospital_admin
  only. Matches both Neuro_RPM's own split (`IsAdmin | IsLocationAdmin` for writes) and
  MomCare's existing `ClinicalTag` catalogue rule.
- **RLS/scoping**: `StatusLabel → organization XOR location__organization`, identical clause
  shape to `ClinicalTag`'s existing RLS policy.

### `PatientStatus` — cloned from `MonitoringNote`

| Field | Notes |
|---|---|
| `patient` FK | required |
| `pregnancy` FK (nullable) | auto-filled from `patient.current_pregnancy` at creation, matches `MonitoringNote`/`MonitoringSession` |
| `name` | required, non-blank |
| `description` | required (matches Neuro_RPM's `PatientStatus` — stricter than `StatusLabel`) |
| `color` | required, hex-validated, exact `#RRGGBB` |

- **Views**: `PatientStatusListCreateView` / `PatientStatusDetailView`, patient-nested flat
  `APIView`s, `OrganizationScopedQuerysetMixin` with `organization_lookup = "patient__organization"`
  — same as `MonitoringNoteDetailView`.
- **Permissions**: create = any `IsHospitalStaff`. Edit/delete = `IsOwnerOrHospitalAdmin`
  (author or hospital admin) — MomCare's own stricter rule, already used by `MonitoringNote`;
  Neuro_RPM lets any staff role edit/delete anyone else's entry, not carried over.
  A location-mismatched patient resolves to 404, matching every other patient-nested endpoint
  in this app.
- **Mutability**: editable and hard-deletable — same posture as `MonitoringNote`, the sibling
  model it's built from (unlike readings/alerts/assessments, which CLAUDE.md forbids editing).
- **RLS/scoping**: `PatientStatus → patient__organization`, identical to `MonitoringNote`'s
  existing RLS policy and scoping path.

## Where it surfaces

- `GET/POST /api/status-labels/`, `PATCH/PUT/DELETE /api/status-labels/{id}/` — the catalogue.
- `GET/POST /api/patients/{patient_id}/statuses/`, `PATCH/PUT/DELETE /api/patient-statuses/{id}/`
  — the log for one patient.
- `PatientListSerializer` and `PatientDetailSerializer` — a new `statuses` field, the full
  history (`name`/`description`/`color`), newest first, per Decision 3. List view prefetches
  via the same `Prefetch`-based pattern already used for `active_pregnancies`, so this doesn't
  add an N+1 query per row.

## Out of scope

- No dashboard/KPI aggregation, search, or filter by status — Neuro_RPM has none either
  (confirmed absent from its analytics, search, and worklist code).
- No audit trail of status changes/deletions — matches Neuro_RPM and this app's own
  `MonitoringNote` precedent (no `AuditLog` entry on note edits either).
