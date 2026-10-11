# Performance audit 2026-10-10, P16/P17 (#477, #478).
#
# Composite (dec, ra) indexes for cone-search bounding boxes on Transient,
# Host and Candidate; (dec_cen, ra_cen) on SurveyField; single-column indexes
# on SurveyObservation.obs_mjd / mjd_requested and Transient.created_date.
# Index-only: no column changes, so develop (which shares YSE_test) is
# unaffected. See docs/ziggy-runbook.md for the production build notes.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('YSE_App', '0030_shared_db_column_defaults'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='transient',
            index=models.Index(fields=['dec', 'ra'], name='yse_transient_dec_ra_idx'),
        ),
        migrations.AddIndex(
            model_name='transient',
            index=models.Index(fields=['created_date'], name='yse_transient_created_idx'),
        ),
        migrations.AddIndex(
            model_name='host',
            index=models.Index(fields=['dec', 'ra'], name='yse_host_dec_ra_idx'),
        ),
        migrations.AddIndex(
            model_name='candidate',
            index=models.Index(fields=['dec', 'ra'], name='yse_candidate_dec_ra_idx'),
        ),
        migrations.AddIndex(
            model_name='surveyfield',
            index=models.Index(fields=['dec_cen', 'ra_cen'], name='yse_surveyfield_dec_ra_idx'),
        ),
        migrations.AddIndex(
            model_name='surveyobservation',
            index=models.Index(fields=['obs_mjd'], name='yse_surveyobs_obs_mjd_idx'),
        ),
        migrations.AddIndex(
            model_name='surveyobservation',
            index=models.Index(fields=['mjd_requested'], name='yse_surveyobs_mjd_req_idx'),
        ),
    ]
