import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0003_remove_restore_audit_action')]

    operations = [
        migrations.CreateModel(
            name='HospitalSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('hospital_name', models.CharField(default='Hospital +', max_length=150)),
                ('logo', models.ImageField(blank=True, null=True, upload_to='hospital/branding/', validators=[django.core.validators.FileExtensionValidator(('png', 'jpg', 'jpeg', 'webp'))])),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'verbose_name_plural': 'hospital settings'},
        ),
    ]
