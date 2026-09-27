from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('pharmacy', '0004_medicinebatch_batch_sale_not_below_cost'),
    ]

    operations = [
        migrations.AlterField(
            model_name='medicinebatch',
            name='supplier',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name='batches', to='pharmacy.supplier',
            ),
        ),
        migrations.AlterField(
            model_name='stockmovement',
            name='movement_type',
            field=models.CharField(
                choices=[
                    ('purchase', 'Purchase'), ('opening_stock', 'Opening stock'),
                    ('sale', 'Sale'), ('adjustment_in', 'Adjustment in'),
                    ('adjustment_out', 'Adjustment out'), ('void', 'Void or reversal'),
                ],
                db_index=True, max_length=20,
            ),
        ),
    ]
