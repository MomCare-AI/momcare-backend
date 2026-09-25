# Note Templates — Design

**Date:** 2026-09-25
**Status:** Approved for implementation.

## Context

Adapted from Neuro_RPM's `NoteTemplate` (`core/organization/models.py`) — reusable canned note
text (title + content) a staff member picks instead of typing a note from scratch.

**Confirmed via investigation: there is no server-side "apply" mechanism in Neuro_RPM.** No
`template_id` field, no linkage of any kind between `NoteTemplate` and their note model. The
frontend fetches a template's `content` and copies it into the note's own text field client-side
— the resulting note is a plain, independent note; nothing records it came from a template. Their
own Postman collection's description of this feature (claiming a `PatientNote` attachment model
with cascade-delete counts) is stale/aspirational and does not match the actual code — confirmed
absent by a full-repo grep. MomCare follows the real, working design, not the stale prose:
`NoteTemplate` is a pure, disconnected content library. `MonitoringNote` gets no new field, no FK.

## Model — `NoteTemplate`, in `core/monitoring` (`clinical_notes` app)

Not Neuro_RPM's `organization` app placement — confirmed that was pure historical migration
baggage (a former standalone `core/notes` app folded in, table name pinned to avoid a rename),
not a meaningful design choice. MomCare's own sibling catalogs (`ClinicalTag`, `StatusLabel`)
already live in `core/monitoring`; `NoteTemplate` joins them for consistency.

| Field | Notes |
|---|---|
| `title` | required |
| `content` | required, the canned text |
| `organization` FK (nullable) XOR `location` FK (nullable) | same `CheckConstraint` shape as `ClinicalTag` |
| `created_by` / `updated_by` | nullable `SET_NULL` FK to User, matches Neuro_RPM's own fields |

**One deliberate departure from Neuro_RPM**: unique `title` per scope (two `UniqueConstraint`s,
same shape as `ClinicalTag`'s `unique_org_clinical_tag_name`/`unique_location_clinical_tag_name`).
Neuro_RPM allows duplicate titles (confirmed: no constraint, no test for it) — two templates
named the same thing in a staff picker is just confusing, and MomCare's own sibling catalogs
already enforce this.

**Copy-to-location**: new signal `copy_org_note_templates_to_new_location`, byte-for-byte the
same pattern as `monitoring/signals.py`'s existing `copy_org_clinical_tags_to_new_location` —
fires on `Location` `post_save` (`created=True` only), independent rows after, never
retroactive. Confirmed this matches Neuro_RPM's own real mechanism for `NoteTemplate` (they use
the signal style here, not `GlobalStatus`'s plain-function-call style) — no conflict.

## API

`GET/POST /api/note-templates/`, `GET/PATCH/DELETE /api/note-templates/{id}/` — flat `APIView`s,
same shape as `ClinicalTagListCreateView`/`ClinicalTagDetailView`, not Neuro_RPM's `ModelViewSet`.

- **Read**: any `IsHospitalStaff` — org-level templates visible from every location (satisfies
  "shown in all branches"), a location's own templates visible only there. Same
  `visible_clinical_tags`-style helper (`visible_note_templates(request, org)`), reused rather
  than porting Neuro_RPM's separate `for-location` dropdown action — MomCare's existing pattern
  (one list endpoint + optional `?location_id=` filter) already answers the same question
  Neuro_RPM needed a second endpoint for, because their plain list's query params couldn't
  express the org-plus-location union and MomCare's `visible_*` helper already does.
- **Write** (create/edit/delete): `hospital_admin` only — matches `ClinicalTag`'s own split, not
  Neuro_RPM's org-owner/location-membership authorization axis, which has no equivalent in
  MomCare's real role system (`hospital_admin`/`provider`/`nurse`/`care_manager`).

## Out of scope

- No `source_template` FK or any linkage from `MonitoringNote` — confirmed nothing like it
  exists in the reference implementation.
- No separate "for-location" dropdown endpoint — the plain list already covers it.
- No duplicate-title test to port from Neuro_RPM (they have none) — MomCare's own uniqueness
  constraint gets its own tests instead.
