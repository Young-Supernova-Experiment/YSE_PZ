"""Computed spectral phase (#848): each spectrum's phase under each reference epoch.

Ryan's definitions (2026-10-25, 2026-11-01):

* **Default:** rest-frame days from the Bazin peak, valid only when the peak is no earlier than
  the last non-detection (``BazinFit.peak_after_last_non_detection``). Without a redshift the
  observer-frame Bazin phase is the default.
* **Options:** SALT t0 (rest frame) and days since discovery.

Rows live in ``SpectrumPhase`` (one per spectrum and reference, see the model). They are rewritten:

* for every spectrum of a transient when one of its Bazin / SALT runs writes fit rows
  (:func:`on_fits_recorded`, called from ``fit_tables.on_run_saved``);
* for one spectrum when it is saved (:func:`on_spectrum_saved`, a ``post_save`` hook);
* by ``manage.py backfill_spectral_phases --apply`` (also picks up changed redshifts and
  discovery dates, which do not trigger a recompute).

The fit is the newest non-subset run (``fit_tables.latest_bazin_reference`` / ``latest_salt``).
Hooks never raise into the save that triggered them.

Access: a phase is shown or used only for a spectrum the viewer may see. Phases from a fit are
derived values: they may select (search filter), but the download manifest leaves them out for a
viewer from whom some of the transient's photometry is hidden (the rule of the stored SALT fit).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from django.db import transaction
from django.db.models import Exists, OuterRef, Q

from YSE_App.models.spectral_phase_models import SpectrumPhase

log = logging.getLogger(__name__)

REF_DEFAULT = "default"
REF_BAZIN_REST = SpectrumPhase.REF_BAZIN_REST
REF_BAZIN_OBS = SpectrumPhase.REF_BAZIN_OBS
REF_SALT_T0 = SpectrumPhase.REF_SALT_T0
REF_DISCOVERY = SpectrumPhase.REF_DISCOVERY

FRAME_REST = SpectrumPhase.FRAME_REST
FRAME_OBSERVER = SpectrumPhase.FRAME_OBSERVER

#: What users pick (search filter and downloads); ``default`` resolves per spectrum.
REFERENCE_CHOICES = (
    (REF_DEFAULT, "Bazin peak (rest frame; observer frame without a redshift)"),
    (REF_BAZIN_REST, "Bazin peak, rest frame"),
    (REF_BAZIN_OBS, "Bazin peak, observer frame"),
    (REF_SALT_T0, "SALT t0, rest frame"),
    (REF_DISCOVERY, "Days since discovery"),
)
REFERENCES = tuple(key for key, _ in REFERENCE_CHOICES)
FIT_REFERENCES = (REF_BAZIN_REST, REF_BAZIN_OBS, REF_SALT_T0)


# ------------------------------------------------------------------ pure math


def _finite(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def phase_days(spectrum_mjd: float, reference_mjd: float, redshift: Optional[float] = None) -> float:
    """Days from ``reference_mjd`` to ``spectrum_mjd``; divided by ``1 + z`` for the rest frame."""
    delta = float(spectrum_mjd) - float(reference_mjd)
    if redshift is None:
        return delta
    return delta / (1.0 + float(redshift))


def usable_redshift(value) -> Optional[float]:
    """A redshift usable for time dilation (finite, > -1), else None."""
    z = _finite(value)
    return z if z is not None and z > -1.0 else None


def bazin_peak_valid(peak_mjd, peak_after_last_non_detection) -> bool:
    """The validity rule: a peak, and not earlier than the last non-detection.

    ``peak_after_last_non_detection`` is None when no non-detection is known; nothing then
    contradicts the peak, so it counts as valid.
    """
    return _finite(peak_mjd) is not None and peak_after_last_non_detection is not False


@dataclass
class Epochs:
    """What a transient's phases are computed from (all MJD, None when unknown)."""

    bazin_peak: Optional[float] = None
    bazin_valid: bool = False
    salt_t0: Optional[float] = None
    salt_z: Optional[float] = None
    redshift: Optional[float] = None
    discovery: Optional[float] = None


def compute_phases(spectrum_mjd: float, epochs: Epochs) -> List[Dict]:
    """The rows (as dicts) of one spectrum: reference, phase, frame, redshift, reference MJD, default flag."""
    out: List[Dict] = []
    z = usable_redshift(epochs.redshift)

    def row(reference, ref_mjd, redshift, default=False):
        out.append({
            "reference": reference,
            "phase": phase_days(spectrum_mjd, ref_mjd, redshift),
            "frame": FRAME_REST if redshift is not None else FRAME_OBSERVER,
            "redshift": redshift,
            "reference_mjd": float(ref_mjd),
            "is_default": default,
        })

    peak = _finite(epochs.bazin_peak)
    if peak is not None and epochs.bazin_valid:
        if z is not None:
            row(REF_BAZIN_REST, peak, z, default=True)
        row(REF_BAZIN_OBS, peak, None, default=z is None)
    t0 = _finite(epochs.salt_t0)
    if t0 is not None:
        salt_z = usable_redshift(epochs.salt_z)
        row(REF_SALT_T0, t0, salt_z if salt_z is not None else z)
    disc = _finite(epochs.discovery)
    if disc is not None:
        row(REF_DISCOVERY, disc, None)
    return out


# ------------------------------------------------------------------ writing


def _mjd(value) -> Optional[float]:
    from YSE_App.models.phot_stat_models import datetime_to_mjd

    return datetime_to_mjd(value) if value is not None else None


def transient_epochs(transient) -> Epochs:
    """The epochs of ``transient`` from its newest fits, best redshift and discovery date."""
    from YSE_App.services import fit_tables

    epochs = Epochs()
    bazin = fit_tables.latest_bazin_reference(transient.pk)
    if bazin is not None:
        epochs.bazin_peak = bazin.peak_mjd
        epochs.bazin_valid = bazin_peak_valid(bazin.peak_mjd, bazin.peak_after_last_non_detection)
    salt = fit_tables.latest_salt(transient.pk)
    if salt is not None:
        epochs.salt_t0 = salt.t0
        epochs.salt_z = salt.z
    from YSE_App.services.redshift_precedence import best_redshift

    epochs.redshift = best_redshift(transient).value
    epochs.discovery = _mjd(transient.disc_date)
    return epochs


def _rows_for(spectra, transient_id: int, epochs: Epochs) -> List[SpectrumPhase]:
    rows = []
    for spectrum_id, obs_date in spectra:
        mjd = _mjd(obs_date)
        if mjd is None:
            continue
        for values in compute_phases(mjd, epochs):
            rows.append(SpectrumPhase(spectrum_id=spectrum_id, transient_id=transient_id, spectrum_mjd=mjd,
                                      **values))
    return rows


def recompute_transient(transient_id: int, spectrum_ids: Optional[Iterable[int]] = None) -> int:
    """Rewrite the phase rows of the transient's spectra (or only ``spectrum_ids``); returns rows written."""
    from YSE_App.models import Transient, TransientSpectrum

    transient = Transient.objects.select_related("host").filter(pk=transient_id).first()
    if transient is None:
        return 0
    spectra_qs = TransientSpectrum.objects.filter(transient_id=transient_id)
    if spectrum_ids is not None:
        spectra_qs = spectra_qs.filter(pk__in=list(spectrum_ids))
    spectra = list(spectra_qs.values_list("pk", "obs_date"))
    ids = [pk for pk, _ in spectra]
    rows = _rows_for(spectra, transient_id, transient_epochs(transient)) if spectra else []
    with transaction.atomic():
        stale = SpectrumPhase.objects.filter(transient_id=transient_id)
        if spectrum_ids is not None:
            stale = stale.filter(spectrum_id__in=ids)
        stale.delete()
        # A spectrum moved to another transient keeps no rows under the old one.
        if ids:
            SpectrumPhase.objects.filter(spectrum_id__in=ids).exclude(transient_id=transient_id).delete()
        SpectrumPhase.objects.bulk_create(rows)
    return len(rows)


def on_fits_recorded(transient_id: Optional[int]) -> None:
    """Called after a Bazin / SALT run wrote its fit rows. Never raises."""
    if not transient_id:
        return
    try:
        recompute_transient(transient_id)
    except Exception:  # noqa: BLE001 - the fit is stored; the backfill can redo the phases
        log.exception("spectral phase: could not recompute transient %s", transient_id)


def on_spectrum_saved(sender, instance, created=False, raw=False, **kwargs):
    """``post_save`` hook on ``TransientSpectrum``: rewrite that spectrum's rows. Never raises."""
    if raw or not instance.transient_id:
        return
    try:
        recompute_transient(instance.transient_id, [instance.pk])
    except Exception:  # noqa: BLE001 - the spectrum is saved; the backfill can redo the phases
        log.exception("spectral phase: could not recompute spectrum %s", instance.pk)


# ------------------------------------------------------------------ reading


def reference_q(reference: str, prefix: str = "") -> Q:
    """``Q`` on ``SpectrumPhase`` rows for a user-facing reference (``default`` = the default row)."""
    if reference == REF_DEFAULT or not reference:
        return Q(**{prefix + "is_default": True})
    return Q(**{prefix + "reference": reference})


def phase_rows_q(reference: str, phase_min=None, phase_max=None, prefix: str = "") -> Q:
    q = reference_q(reference, prefix)
    if phase_min is not None:
        q &= Q(**{prefix + "phase__gte": float(phase_min)})
    if phase_max is not None:
        q &= Q(**{prefix + "phase__lte": float(phase_max)})
    return q


def spectra_in_phase_range(spectra_qs, reference: str, phase_min=None, phase_max=None):
    """``spectra_qs`` (``TransientSpectrum``) limited to spectra with a phase in the range under ``reference``."""
    rows = SpectrumPhase.objects.filter(phase_rows_q(reference, phase_min, phase_max), spectrum_id=OuterRef("pk"))
    return spectra_qs.filter(Exists(rows))


def phases_for(spectrum_ids: Iterable[int], reference: str) -> Dict[int, SpectrumPhase]:
    """``{spectrum id: row}`` under ``reference`` (``default`` resolved per spectrum)."""
    ids = list(spectrum_ids)
    if not ids:
        return {}
    rows = SpectrumPhase.objects.filter(reference_q(reference), spectrum_id__in=ids)
    return {row.spectrum_id: row for row in rows}
