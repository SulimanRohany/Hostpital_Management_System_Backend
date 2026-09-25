from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0002_alter_auditlog_action')]

    operations = [
        migrations.AlterField(
            model_name='auditlog',
            name='action',
            field=models.CharField(
                choices=[
                    ('create', 'Create'), ('update', 'Update'), ('delete', 'Delete'),
                    ('login', 'Login'), ('logout', 'Logout'), ('export', 'Export'),
                    ('backup', 'Backup'),
                ],
                max_length=20,
            ),
        ),
    ]
