import accounts.models
from django.db import migrations, models


def normalize_superusers(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    User.objects.filter(is_superuser=True).update(
        role='administrator', must_change_password=False,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_alter_user_role'),
    ]

    operations = [
        migrations.RunPython(normalize_superusers, migrations.RunPython.noop),
        migrations.AlterModelOptions(
            name='user',
            options={
                'ordering': ('first_name', 'last_name', 'username'),
                'verbose_name': 'user',
                'verbose_name_plural': 'users',
            },
        ),
        migrations.AlterModelManagers(
            name='user',
            managers=[('objects', accounts.models.UserManager())],
        ),
        migrations.AddConstraint(
            model_name='user',
            constraint=models.CheckConstraint(
                condition=models.Q(is_superuser=False) | models.Q(role='administrator'),
                name='superuser_has_administrator_role',
            ),
        ),
        migrations.AddIndex(
            model_name='user',
            index=models.Index(fields=['is_active', 'role'], name='user_active_role_idx'),
        ),
    ]
