from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('monitoring', '0014_remove_riskassessment_doctor_notified_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='vitalreading',
            name='source',
            field=models.CharField(
                choices=[('device', 'Device'), ('manual', 'Manual entry')],
                db_index=True,
                default='manual',
                max_length=10,
            ),
            preserve_default=False,
        ),
    ]
