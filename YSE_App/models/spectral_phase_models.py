"""Computed phase of each transient spectrum under each reference epoch (#848).

One row per (spectrum, reference). ``services.spectral_phase`` writes them when a Bazin / SALT
fit is recorded (the #846 hook), when a spectrum is saved, and from
``manage.py backfill_spectral_phases``. Readers: the "spectrum in phase range" search filter
and the Download -> Spectra manifest.

References:

* ``bazin_rest``: rest-frame days from the Bazin peak (reference band of the newest non-subset
  Bazin run), written only with a redshift;
* ``bazin_obs``: observer-frame days from the same peak;
* ``salt_t0``: rest-frame days from SALT t0 (the fit's own redshift, else the transient's;
  observer frame when neither is known);
* ``discovery``: observer-frame days since the transient's discovery date.

Bazin rows are written only when the peak is no earlier than the last non-detection
(``BazinFit.peak_after_last_non_detection`` is not False). ``is_default`` marks the default
reference of a spectrum: ``bazin_rest``, or ``bazin_obs`` when there is no redshift.

Both foreign keys carry database-level ``ON DELETE CASCADE`` (migration 0068), so deletes by
code that does not know this table still work.
"""

from __future__ import annotations

from django.db import models

from YSE_App.models.spectra_models import TransientSpectrum
from YSE_App.models.transient_models import Transient


class SpectrumPhase(models.Model):
    REF_BAZIN_REST = "bazin_rest"
    REF_BAZIN_OBS = "bazin_obs"
    REF_SALT_T0 = "salt_t0"
    REF_DISCOVERY = "discovery"
    REFERENCE_CHOICES = (
        (REF_BAZIN_REST, "Bazin peak, rest frame"),
        (REF_BAZIN_OBS, "Bazin peak, observer frame"),
        (REF_SALT_T0, "SALT t0, rest frame"),
        (REF_DISCOVERY, "Days since discovery"),
    )

    FRAME_REST = "rest"
    FRAME_OBSERVER = "observer"
    FRAME_CHOICES = ((FRAME_REST, "rest frame"), (FRAME_OBSERVER, "observer frame"))

    spectrum = models.ForeignKey(TransientSpectrum, on_delete=models.CASCADE, related_name="phases")
    transient = models.ForeignKey(Transient, on_delete=models.CASCADE, related_name="spectrum_phases")
    reference = models.CharField(max_length=16, choices=REFERENCE_CHOICES)
    # The spectrum's default reference (bazin_rest, else bazin_obs without a redshift).
    is_default = models.BooleanField(default=False)
    # Days after the reference epoch (negative before it), in ``frame``.
    phase = models.FloatField()
    frame = models.CharField(max_length=8, choices=FRAME_CHOICES)
    # Redshift used for the rest frame (None in the observer frame).
    redshift = models.FloatField(null=True, blank=True)
    # MJD of the reference epoch and of the spectrum.
    reference_mjd = models.FloatField()
    spectrum_mjd = models.FloatField()
    computed_date = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("spectrum_id", "reference")
        constraints = [
            models.UniqueConstraint(fields=["spectrum", "reference"], name="yse_specphase_spec_ref_uniq"),
        ]
        indexes = [
            models.Index(fields=["reference", "phase"], name="yse_specphase_ref_phase_idx"),
            models.Index(fields=["is_default", "phase"], name="yse_specphase_default_idx"),
            models.Index(fields=["transient", "reference"], name="yse_specphase_transient_idx"),
        ]

    def __str__(self):
        return "Spectrum %s: %+.1f d (%s)" % (self.spectrum_id, self.phase, self.reference)
