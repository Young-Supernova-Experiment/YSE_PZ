"""Record of the duplicate clean-up (#890; YSE_App/models/duplicate_models.py).

A new table only: no existing table or column changes, so it is safe on the shared database.
Nothing is flagged by the migration; that is ``manage.py clean_duplicate_data --apply``.
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0060_fill_account_names'),
    ]

    operations = [
        migrations.CreateModel(
            name='DuplicateDataDecision',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('photometry', 'photometry'), ('spectrum', 'spectrum')], max_length=16)),
                ('reason', models.CharField(choices=[('tns_copy', 'tns_copy'), ('no_errors', 'no_errors'),
                                                     ('later_upload', 'later_upload')], max_length=16)),
                ('weak', models.BooleanField(default=False)),
                ('time_difference', models.FloatField(blank=True, null=True)),
                ('previous_quality_mask', models.PositiveBigIntegerField()),
                ('created_date', models.DateTimeField(auto_now_add=True)),
                ('reverted_date', models.DateTimeField(blank=True, null=True)),
                ('kept_phot_data', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                     related_name='+', to='YSE_App.transientphotdata')),
                ('kept_spectrum', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                    related_name='+', to='YSE_App.transientspectrum')),
                ('phot_data', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                                related_name='duplicate_decisions', to='YSE_App.transientphotdata')),
                ('spectrum', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                               related_name='duplicate_decisions', to='YSE_App.transientspectrum')),
                ('transient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                                related_name='duplicate_decisions', to='YSE_App.transient')),
            ],
        ),
    ]
