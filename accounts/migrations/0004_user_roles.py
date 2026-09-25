from django.db import migrations, models


def copy_primary_roles(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    for user in User.objects.all().only('id', 'role').iterator():
        user.roles = [user.role]
        user.save(update_fields=('roles',))


class Migration(migrations.Migration):
    dependencies = [('accounts', '0003_user_model_rules')]

    operations = [
        migrations.AddField(
            model_name='user',
            name='roles',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(copy_primary_roles, migrations.RunPython.noop),
    ]
