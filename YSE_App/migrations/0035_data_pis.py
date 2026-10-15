"""PIs on private data (#446).

* ``PrincipalInvestigator.user``: a nullable FK to the PI's YSE-PZ account.
* ``TransientPhotometry.pis`` and ``TransientSpectrum.pis``: M2M to
  ``PrincipalInvestigator`` (new join tables only).

Safe on the shared YSE_test database: one nullable column on an existing table
plus two new tables, so ``develop`` and ``experimental`` INSERTs keep working
whichever code runs. No data is changed (``manage.py backfill_data_pis`` is an
optional, dry-run-by-default backfill). Named ``0035_data_pis`` so it never
collides with an experimental ``0035_*``.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('YSE_App', '0034_seed_group_profiles'),
    ]

    operations = [
        migrations.AddField(
            model_name='principalinvestigator',
            name='user',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='pi_records',
                to=settings.AUTH_USER_MODEL,
                help_text='YSE-PZ account of this PI (optional). Linked PIs are notified about interest in, and '
                          'requests for, the data they lead, and may share it.',
            ),
        ),
        migrations.AddField(
            model_name='transientphotometry',
            name='pis',
            field=models.ManyToManyField(
                blank=True, related_name='transient_photometry', to='YSE_App.PrincipalInvestigator',
                help_text='Principal investigator(s) of this photometry set.',
            ),
        ),
        migrations.AddField(
            model_name='transientspectrum',
            name='pis',
            field=models.ManyToManyField(
                blank=True, related_name='transient_spectra', to='YSE_App.PrincipalInvestigator',
                help_text='Principal investigator(s) of this spectrum.',
            ),
        ),
    ]
