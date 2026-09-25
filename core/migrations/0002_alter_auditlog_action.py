from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0001_initial')]

    operations = [
        migrations.AlterField(
            model_name='auditlog',
            name='action',
            field=models.CharField(
                choices=[
                    ('create', 'Create'), ('update', 'Update'), ('delete', 'Delete'),
                    ('login', 'Login'), ('logout', 'Logout'), ('export', 'Export'),
                    ('backup', 'Backup'), ('restore', 'Restore'),
                ],
                max_length=20,
            ),
        ),
    ]
