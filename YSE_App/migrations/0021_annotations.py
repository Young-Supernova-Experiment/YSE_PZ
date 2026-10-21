# Structured annotations (#316: #317 model + API, #318 catalogue checks, #319 indexed values for search filters).

import YSE_App.models.fields
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('auth', '0012_alter_user_first_name_max_length'),
        ('YSE_App', '0020_analysis_services'),
    ]

    operations = [
        migrations.CreateModel(
            name='TransientAnnotation',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_date', models.DateTimeField(auto_now_add=True)),
                ('modified_date', models.DateTimeField(auto_now=True)),
                ('origin', models.CharField(db_index=True, help_text='Who produced it: a service slug (gaia_dr3, wise, quasar), a broker (antares) or user:<username>.', max_length=64)),
                ('data', YSE_App.models.fields.JSONTextField(blank=True, default=dict, help_text='Flat JSON object: key -> number, string, boolean or null.')),
                ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='transientannotation_created_by', to=settings.AUTH_USER_MODEL)),
                ('groups', models.ManyToManyField(blank=True, help_text='Collaboration groups that may see it; empty = every logged-in user.', related_name='transient_annotations', to='auth.Group')),
                ('modified_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='transientannotation_modified_by', to=settings.AUTH_USER_MODEL)),
                ('run', models.ForeignKey(blank=True, help_text='The service run that last wrote this annotation.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='annotations', to='YSE_App.externalservicerun')),
                ('service', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='annotations', to='YSE_App.externalservice')),
                ('transient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='annotations', to='YSE_App.transient')),
            ],
            options={
                'ordering': ('origin',),
            },
        ),
        migrations.CreateModel(
            name='TransientAnnotationValue',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('origin', models.CharField(max_length=64)),
                ('key', models.CharField(max_length=64)),
                ('value_text', models.CharField(blank=True, max_length=255, null=True)),
                ('value_num', models.FloatField(blank=True, null=True)),
                ('annotation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='values', to='YSE_App.transientannotation')),
                ('transient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='annotation_values', to='YSE_App.transient')),
            ],
        ),
        migrations.AddIndex(
            model_name='transientannotationvalue',
            index=models.Index(fields=['origin', 'key', 'value_num'], name='yse_annval_okn_idx'),
        ),
        migrations.AddIndex(
            model_name='transientannotationvalue',
            index=models.Index(fields=['key', 'value_num'], name='yse_annval_kn_idx'),
        ),
        migrations.AddIndex(
            model_name='transientannotationvalue',
            index=models.Index(fields=['transient', 'origin', 'key'], name='yse_annval_tok_idx'),
        ),
        migrations.AlterUniqueTogether(
            name='transientannotationvalue',
            unique_together={('annotation', 'key')},
        ),
        migrations.AddIndex(
            model_name='transientannotation',
            index=models.Index(fields=['origin', 'modified_date'], name='yse_annot_origin_mod_idx'),
        ),
        migrations.AlterUniqueTogether(
            name='transientannotation',
            unique_together={('transient', 'origin')},
        ),
    ]
