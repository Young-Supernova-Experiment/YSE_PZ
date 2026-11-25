"""Typed tables of light-curve fits that keep every run (#846).

``BazinFit`` has one row per run and band, ``SaltFit`` one row per run. The
rows are written when a Bazin / SALT analysis run succeeds
(``services.fit_tables.record_run``, called from a ``post_save`` hook on
:class:`ExternalServiceRun`), and by ``manage.py backfill_fit_tables`` for
older runs. The run's ``result`` JSON stays the full record; these tables
make the fits queryable and keep their history.

Defaults come from the **most recent** non-subset run, with no pinning
(``services.fit_tables.latest_bazin`` / ``latest_salt``). A *subset* run
(#847: a fit to a chosen part of the light curve) is stored with
``is_subset`` and its ``selection`` and never becomes the default.

Both tables reference ``YSE_App_transient`` and ``YSE_App_externalservicerun``
with database-level ``ON DELETE CASCADE`` (migration 0063), so deletes by
code that does not know these tables still work.
"""

from __future__ import annotations

from django.db import models

from YSE_App.models.external_service_models import ExternalServiceRun
from YSE_App.models.fields import JSONTextField
from YSE_App.models.transient_models import Transient


class FitRowBase(models.Model):
    """What every fit row has besides its run and transient: time, subset marker, fit quality."""

    # When the fit finished (the run's finished_at); the history is ordered by it.
    fit_date = models.DateTimeField(null=True, blank=True)
    # A fit to part of the light curve (#847): never the default value.
    is_subset = models.BooleanField(default=False)
    # The subset's selection (bands, MJD range, point ids, ...), as the run's parameters gave it.
    selection = JSONTextField(null=True, blank=True)
    n_points = models.PositiveIntegerField(null=True, blank=True)
    chi2 = models.FloatField(null=True, blank=True)
    dof = models.IntegerField(null=True, blank=True)
    reduced_chi2 = models.FloatField(null=True, blank=True)
    created_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class BazinFit(FitRowBase):
    """One band of one Bazin run (``analysis/bazin_fit.py``)."""

    run = models.ForeignKey(ExternalServiceRun, on_delete=models.CASCADE, related_name="bazin_fits")
    transient = models.ForeignKey(Transient, on_delete=models.CASCADE, related_name="bazin_fits")

    # The band name the fit used (PhotometricBand.name), as in the run's ``per_band``.
    band = models.CharField(max_length=64)
    # ``normalize_filter_name(band)`` when the row was written; blank when it has none.
    band_key = models.CharField(max_length=16, blank=True, default="")
    # The run's reference band (its top-level peak).
    is_reference_band = models.BooleanField(default=False)
    method = models.CharField(max_length=32, blank=True, default="")
    peak_mjd = models.FloatField(null=True, blank=True)
    peak_mag = models.FloatField(null=True, blank=True)
    t0 = models.FloatField(null=True, blank=True)
    t0_err = models.FloatField(null=True, blank=True)
    tau_rise = models.FloatField(null=True, blank=True)
    tau_rise_err = models.FloatField(null=True, blank=True)
    tau_fall = models.FloatField(null=True, blank=True)
    tau_fall_err = models.FloatField(null=True, blank=True)
    mjd_min = models.FloatField(null=True, blank=True)
    mjd_max = models.FloatField(null=True, blank=True)
    # The last non-detection before the first detection the fit saw, and whether
    # the fitted peak is no earlier than it (None when either is unknown).
    last_non_detection_mjd = models.FloatField(null=True, blank=True)
    peak_after_last_non_detection = models.BooleanField(null=True, blank=True)

    class Meta:
        ordering = ("-fit_date", "-run_id", "band")
        constraints = [
            models.UniqueConstraint(fields=["run", "band"], name="yse_bazinfit_run_band_uniq"),
        ]
        indexes = [
            models.Index(fields=["transient", "fit_date"], name="yse_bazinfit_transient_idx"),
        ]

    def __str__(self):
        return "Bazin %s %s (run %s)" % (self.transient_id, self.band, self.run_id)


class SaltFit(FitRowBase):
    """One SALT run (``analysis/sncosmo_fit.py`` with a SALT source)."""

    run = models.ForeignKey(ExternalServiceRun, on_delete=models.CASCADE, related_name="salt_fits")
    transient = models.ForeignKey(Transient, on_delete=models.CASCADE, related_name="salt_fits")

    # sncosmo source name (salt3, salt2, ...) and its version when the run recorded it.
    model = models.CharField(max_length=32)
    model_version = models.CharField(max_length=32, blank=True, default="")
    t0 = models.FloatField(null=True, blank=True)
    t0_err = models.FloatField(null=True, blank=True)
    x0 = models.FloatField(null=True, blank=True)
    x0_err = models.FloatField(null=True, blank=True)
    x1 = models.FloatField(null=True, blank=True)
    x1_err = models.FloatField(null=True, blank=True)
    c = models.FloatField(null=True, blank=True)
    c_err = models.FloatField(null=True, blank=True)
    m_b = models.FloatField(null=True, blank=True)
    m_b_err = models.FloatField(null=True, blank=True)
    # The redshift the fit used, where it came from (transient, host, fitted) and its error when fitted.
    z = models.FloatField(null=True, blank=True)
    z_err = models.FloatField(null=True, blank=True)
    z_source = models.CharField(max_length=16, blank=True, default="")
    mwebv = models.FloatField(null=True, blank=True)
    # sncosmo band names the fit used.
    bands = JSONTextField(default=list)

    class Meta:
        ordering = ("-fit_date", "-run_id")
        constraints = [
            models.UniqueConstraint(fields=["run"], name="yse_saltfit_run_uniq"),
        ]
        indexes = [
            models.Index(fields=["transient", "fit_date"], name="yse_saltfit_transient_idx"),
        ]

    def __str__(self):
        return "SALT %s (run %s)" % (self.transient_id, self.run_id)
