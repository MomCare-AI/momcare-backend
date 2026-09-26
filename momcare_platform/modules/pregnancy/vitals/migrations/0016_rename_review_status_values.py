from django.db import migrations, models

# unreviewed -> pending: exact match, same meaning.
# confirmed  -> reviewed: the doctor agreed with the model -- fully handled.
# corrected  -> reviewed: the doctor disagreed and recorded their own answer
#               -- also fully handled by that correction, not left needing
#               escalation. Under the old single-action `verify` endpoint
#               there was no way to have produced an "escalated" row, so no
#               existing row should silently become one now. See
#               docs/design/2026-09-26-risk-review-workflow-design.md.
OLD_TO_NEW = {
    "unreviewed": "pending",
    "confirmed": "reviewed",
    "corrected": "reviewed",
}


def rename_forwards(apps, schema_editor):
    RiskAssessment = apps.get_model("monitoring", "RiskAssessment")
    for old_value, new_value in OLD_TO_NEW.items():
        RiskAssessment.objects.filter(review_status=old_value).update(review_status=new_value)


def rename_backwards(apps, schema_editor):
    RiskAssessment = apps.get_model("monitoring", "RiskAssessment")
    RiskAssessment.objects.filter(review_status="pending").update(review_status="unreviewed")
    # "reviewed" is a lossy merge of the old confirmed/corrected split -- an
    # unambiguous reverse isn't possible, so it lands back on "confirmed"
    # (the more common case) rather than guessing per row.
    RiskAssessment.objects.filter(review_status="reviewed").update(review_status="confirmed")
    RiskAssessment.objects.filter(review_status="escalated").update(review_status="confirmed")


class Migration(migrations.Migration):
    dependencies = [
        ("monitoring", "0015_vitalreading_source"),
    ]

    operations = [
        migrations.AlterField(
            model_name="riskassessment",
            name="review_status",
            field=models.CharField(
                choices=[("pending", "Pending"), ("reviewed", "Reviewed"), ("escalated", "Escalated")],
                db_index=True,
                default="pending",
                max_length=20,
            ),
        ),
        migrations.RunPython(rename_forwards, rename_backwards),
    ]
