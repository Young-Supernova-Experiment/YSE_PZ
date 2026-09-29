"""Per-transient photometry statistics (#268, the SkyPortal ``PhotStat`` equivalent).

One ``TransientPhotStat`` row per transient summarises its ``TransientPhotData``
rows: how many points and detections there are, the first, last and peak
detections, the mean and faintest magnitudes, the deepest upper limit, and
the rise and decay rates.  ``YSE_App.services.photstat`` computes and stores
the row; signals on ``TransientPhotData`` and the bulk ingest paths keep it
current, and ``manage.py rebuild_photstats`` backfills or repairs it.

Rules (the ones the dashboards already use, see ``services/photstat.py``):

* a **detection** is a point with a magnitude and no ``data_quality`` flag,
  exactly what ``Transient.recent_mag()`` and the ``recent_mag`` table
  columns count;
* an **upper limit** is an unflagged point without a magnitude whose flux,
  flux error and zero point give ``-2.5 log10(flux + 3 flux_err) + zp``, the
  light-curve plot's rule;
* flagged points are ignored entirely.

MJDs are stored as floats for filtering and arithmetic; the matching
``datetime`` values (``*_date``) are stored too so tables and templates can
show them without a conversion, and so the dashboard ``recent_mag`` /
``recent_magdate`` columns can later read ``last_detected_mag`` /
``last_obs_date`` instead of running two subqueries per row (#270, #331).
"""

import datetime
import json

from django.db import models

from YSE_App.models.photometric_band_models import PhotometricBand
from YSE_App.models.transient_models import Transient

__all__ = ['TransientPhotStat', 'MJD_EPOCH', 'datetime_to_mjd', 'mjd_to_datetime']

MJD_EPOCH = datetime.datetime(1858, 11, 17, tzinfo=datetime.timezone.utc)


def datetime_to_mjd(value):
    """MJD of ``value`` (naive datetimes are taken as UTC); ``None`` passes through."""
    if value is None:
        return None
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        value = datetime.datetime.combine(value, datetime.time())
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return (value - MJD_EPOCH).total_seconds() / 86400.0


def mjd_to_datetime(mjd):
    """Aware UTC ``datetime`` for ``mjd``; ``None`` passes through."""
    if mjd is None:
        return None
    return MJD_EPOCH + datetime.timedelta(days=float(mjd))


class TransientPhotStat(models.Model):
    """Stored photometry aggregates for one transient (see module docstring)."""

    transient = models.OneToOneField(
        Transient, on_delete=models.CASCADE, related_name='photstat'
    )

    # Counts over unflagged points.
    num_obs_global = models.IntegerField(default=0)
    num_det_global = models.IntegerField(default=0)
    num_limits_global = models.IntegerField(default=0)

    # Latest unflagged point of any kind (detection or limit).
    last_obs_mjd = models.FloatField(null=True, blank=True)
    last_obs_date = models.DateTimeField(null=True, blank=True)

    first_detected_mjd = models.FloatField(null=True, blank=True)
    first_detected_date = models.DateTimeField(null=True, blank=True)
    first_detected_mag = models.FloatField(null=True, blank=True)
    first_detected_band = models.ForeignKey(
        PhotometricBand, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )

    last_detected_mjd = models.FloatField(null=True, blank=True)
    last_detected_date = models.DateTimeField(null=True, blank=True)
    last_detected_mag = models.FloatField(null=True, blank=True)
    last_detected_band = models.ForeignKey(
        PhotometricBand, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )

    peak_mjd = models.FloatField(null=True, blank=True)
    peak_date = models.DateTimeField(null=True, blank=True)
    peak_mag = models.FloatField(null=True, blank=True)
    peak_band = models.ForeignKey(
        PhotometricBand, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )

    mean_mag = models.FloatField(null=True, blank=True)
    faintest_mag = models.FloatField(null=True, blank=True)

    # Deepest upper limit (largest limiting magnitude) among the limits taken
    # before the first detection (every limit when there is no detection),
    # when it was taken and in which band.
    deepest_limit = models.FloatField(null=True, blank=True)
    deepest_limit_mjd = models.FloatField(null=True, blank=True)
    deepest_limit_band = models.ForeignKey(
        PhotometricBand, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )

    # Last upper limit before the first detection, its band, and the gap (days).
    last_non_detection_mjd = models.FloatField(null=True, blank=True)
    last_non_detection_band = models.ForeignKey(
        PhotometricBand, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )
    time_to_non_detection = models.FloatField(null=True, blank=True)

    # mag/day, both positive: brightening before peak, fading after it.
    rise_rate = models.FloatField(null=True, blank=True)
    decay_rate = models.FloatField(null=True, blank=True)

    # {"<band_id>": {"name", "n_det", "peak_mag", "peak_mjd", "last_mag", "last_mjd"}}
    # as JSON text (a TextField so no database JSON type is required).
    per_band_json = models.TextField(null=True, blank=True)

    # Fingerprint of the stored values; an unchanged recompute writes nothing.
    phot_hash = models.CharField(max_length=40, blank=True, default='')
    # ``services.photstat.SCHEMA_VERSION`` at the time of the last write; a
    # smaller value marks a row computed with older rules (recomputed on the
    # next page view, signal or ``rebuild_photstats --stale-only``).
    schema_version = models.IntegerField(default=0)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['peak_mag'], name='yse_photstat_peak_mag_idx'),
            models.Index(fields=['last_detected_mjd'], name='yse_photstat_last_det_mjd_idx'),
            models.Index(fields=['last_detected_mag'], name='yse_photstat_last_det_mag_idx'),
            models.Index(fields=['last_obs_date'], name='yse_photstat_last_obs_idx'),
            models.Index(fields=['num_det_global'], name='yse_photstat_num_det_idx'),
            models.Index(fields=['first_detected_mjd'], name='yse_photstat_first_det_idx'),
        ]

    def __str__(self):
        return 'PhotStat: %s' % self.transient_id

    @property
    def per_band(self):
        """Per-band summary dict (``{}`` when nothing is stored)."""
        if not self.per_band_json:
            return {}
        try:
            return json.loads(self.per_band_json)
        except ValueError:
            return {}

    @per_band.setter
    def per_band(self, value):
        self.per_band_json = json.dumps(value, sort_keys=True) if value else None

    @property
    def has_detections(self):
        return self.num_det_global > 0

    @property
    def last_non_detection_date(self):
        """Aware UTC datetime of the last upper limit before the first detection."""
        return mjd_to_datetime(self.last_non_detection_mjd)

    @property
    def deepest_limit_date(self):
        """Aware UTC datetime of the deepest upper limit."""
        return mjd_to_datetime(self.deepest_limit_mjd)
