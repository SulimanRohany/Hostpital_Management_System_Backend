from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('patients', '0003_patientnote_amended_at_patientnote_amended_by_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='patientnote', name='is_archived',
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name='patientnote', name='archived_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='patientnote', name='archive_reason',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='patientnote', name='archived_by',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name='archived_patient_notes', to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
