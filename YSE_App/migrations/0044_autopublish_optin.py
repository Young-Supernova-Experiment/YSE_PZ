"""Opt-in to automatic publishing of restricted data (#691): one new table, no changes to existing tables.

Rows are written only when a PI or deputy opts a dataset in; an empty table
means automatic publishing sends public data only (today's safe default).
"""

from django.conf import settings
from django.db import migrations, models
from django.db.models import Q
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('YSE_App', '0043_data_access_grants'),
    ]

    operations = [
        migrations.CreateModel(
            name='AutopublishOptIn',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_date', models.DateTimeField(auto_now_add=True)),
                ('opted_in_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+',
                    to=settings.AUTH_USER_MODEL)),
                ('photometry', models.OneToOneField(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name='autopublish_optin', to='YSE_App.transientphotometry')),
                ('spectrum', models.OneToOneField(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name='autopublish_optin', to='YSE_App.transientspectrum')),
            ],
        ),
        migrations.AddConstraint(
            model_name='autopublishoptin',
            constraint=models.CheckConstraint(
                check=(Q(photometry__isnull=False, spectrum__isnull=True)
                       | Q(photometry__isnull=True, spectrum__isnull=False)),
                name='yse_autopublishoptin_one_dataset'),
        ),
    ]
