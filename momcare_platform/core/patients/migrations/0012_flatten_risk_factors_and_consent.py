"""Fold two satellite tables into the records they described.

``PregnancyRiskFactors`` was a one-to-one table holding seven answers that are
given once per pregnancy and never independently of it — so they are now plain
columns on ``Pregnancy``.

``Consent`` was an append-only event log. It becomes a single ``consent_date``
on ``Patient``, matching the reference platform's own shape. Existing data is
carried across before either table is dropped: each patient keeps the date of
her earliest GRANTED consent, so "when did she agree" survives even though the
grant/withdraw history does not.
"""

from django.db import migrations, models


def copy_risk_factors_onto_pregnancy(apps, schema_editor):
    PregnancyRiskFactors = apps.get_model("patients", "PregnancyRiskFactors")
    Pregnancy = apps.get_model("patients", "Pregnancy")
    FACTORS = [
        "previous_c_section",
        "previous_preeclampsia",
        "previous_gestational_diabetes",
        "previous_preterm_birth",
        "chronic_hypertension",
        "diabetes",
        "multiple_pregnancy",
    ]
    updates = []
    for row in PregnancyRiskFactors.objects.select_related("pregnancy").iterator():
        pregnancy = row.pregnancy
        for field in FACTORS:
            setattr(pregnancy, field, getattr(row, field))
        updates.append(pregnancy)
    if updates:
        Pregnancy.objects.bulk_update(updates, FACTORS)


def copy_consent_onto_patient(apps, schema_editor):
    """Earliest GRANTED consent wins — the date she first agreed.

    A patient whose only rows are withdrawals keeps a null consent_date, which
    is the honest answer: there is no grant on record for her.
    """
    Consent = apps.get_model("patients", "Consent")
    Patient = apps.get_model("patients", "Patient")
    first_grant = {}
    for row in Consent.objects.filter(status="granted").order_by("recorded_at").iterator():
        first_grant.setdefault(row.patient_id, row.recorded_at.date())
    if not first_grant:
        return
    patients = list(Patient.objects.filter(id__in=first_grant))
    for patient in patients:
        patient.consent_date = first_grant[patient.id]
    Patient.objects.bulk_update(patients, ["consent_date"])


class Migration(migrations.Migration):
    dependencies = [
        ("patients", "0011_delete_patientprogramenrollment"),
        ("organization", "0016_program_enrollment_row_level_security"),
    ]

    operations = [
        # 1. New columns.
        migrations.AddField(
            model_name="patient",
            name="consent_date",
            field=models.DateField(blank=True, null=True),
        ),
        *[
            migrations.AddField(
                model_name="pregnancy",
                name=name,
                field=models.CharField(
                    choices=[("yes", "Yes"), ("no", "No"), ("unknown", "Unknown")],
                    default="unknown",
                    max_length=10,
                ),
            )
            for name in [
                "previous_c_section",
                "previous_preeclampsia",
                "previous_gestational_diabetes",
                "previous_preterm_birth",
                "chronic_hypertension",
                "diabetes",
                "multiple_pregnancy",
            ]
        ],
        # 2. Carry the data across before anything is dropped.
        migrations.RunPython(copy_risk_factors_onto_pregnancy, migrations.RunPython.noop),
        migrations.RunPython(copy_consent_onto_patient, migrations.RunPython.noop),
        # 3. Drop the satellite tables. Postgres drops each table's RLS policy
        #    with the table itself, so no policy migration is needed.
        migrations.DeleteModel(name="PregnancyRiskFactors"),
        migrations.DeleteModel(name="Consent"),
    ]
