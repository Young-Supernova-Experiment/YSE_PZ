"""Options of the multi-object downloads (#848): Download -> Spectra and Download -> Photometry.

Parsed from the download form's GET parameters. Options only ever narrow what the viewer may
already download; access is decided by the same per-spectrum and per-point calls as before.

Spectra: ``phase_min`` / ``phase_max`` (days) under ``phase_ref`` (``default``, ``bazin_rest``,
``bazin_obs``, ``salt_t0``, ``discovery``), ``instrument`` (repeat; instrument names) and
``snr_min`` (median flux / error over the pixels with an error).

Photometry: ``mjd_min`` / ``mjd_max``, ``phase_min`` / ``phase_max`` under ``phase_ref`` (the
transient's reference epoch, as for spectra), ``band`` (repeat; band names as in the FLT column)
and ``exclude_flagged`` (drop points with data-quality flags).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, List, Optional

import numpy as np

from YSE_App.services import spectral_phase


class OptionError(ValueError):
    """A download option that cannot be read; the message can be shown to the user."""


def _number(params, key, label) -> Optional[float]:
    raw = (params.get(key) or "").strip()
    if raw == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        raise OptionError("%s must be a number." % label)
    if not np.isfinite(value):
        raise OptionError("%s must be a finite number." % label)
    return value


def _names(params, key) -> FrozenSet[str]:
    values = params.getlist(key) if hasattr(params, "getlist") else [params.get(key)]
    out = set()
    for value in values:
        for part in str(value or "").split(","):
            if part.strip():
                out.add(part.strip().lower())
    return frozenset(out)


def _reference(params) -> str:
    reference = (params.get("phase_ref") or "").strip() or spectral_phase.REF_DEFAULT
    if reference not in spectral_phase.REFERENCES:
        raise OptionError("Unknown phase reference %r." % reference)
    return reference


def _range(params, low_key, high_key, label):
    low = _number(params, low_key, "%s from" % label)
    high = _number(params, high_key, "%s to" % label)
    if low is not None and high is not None and low > high:
        raise OptionError("%s: the start is after the end." % label)
    return low, high


def reference_label(reference: str) -> str:
    return dict(spectral_phase.REFERENCE_CHOICES).get(reference, reference)


@dataclass
class SpectraOptions:
    phase_min: Optional[float] = None
    phase_max: Optional[float] = None
    reference: str = spectral_phase.REF_DEFAULT
    instruments: FrozenSet[str] = field(default_factory=frozenset)
    snr_min: Optional[float] = None

    @classmethod
    def from_params(cls, params) -> "SpectraOptions":
        low, high = _range(params, "phase_min", "phase_max", "Phase")
        return cls(phase_min=low, phase_max=high, reference=_reference(params),
                   instruments=_names(params, "instrument"), snr_min=_number(params, "snr_min", "Minimum S/N"))

    @property
    def phase_range(self) -> bool:
        return self.phase_min is not None or self.phase_max is not None

    def describe(self) -> List[str]:
        lines = ["Phase reference: %s" % reference_label(self.reference)]
        if self.phase_range:
            lines.append("Phase range (days): %s to %s" % (
                "-inf" if self.phase_min is None else "%g" % self.phase_min,
                "+inf" if self.phase_max is None else "%g" % self.phase_max))
        if self.instruments:
            lines.append("Instruments: %s" % ", ".join(sorted(self.instruments)))
        if self.snr_min is not None:
            lines.append("Minimum median S/N: %g (spectra without flux errors are left out)" % self.snr_min)
        return lines


def median_snr(arrays) -> Optional[float]:
    """Median flux / error over pixels with a finite, positive error; None without any."""
    _wave, flux, err = arrays
    if err is None:
        return None
    flux = np.asarray(flux, dtype=float)
    err = np.asarray(err, dtype=float)
    good = np.isfinite(err) & (err > 0) & np.isfinite(flux)
    if not good.any():
        return None
    return float(np.median(flux[good] / err[good]))


@dataclass
class PhotometryOptions:
    mjd_min: Optional[float] = None
    mjd_max: Optional[float] = None
    phase_min: Optional[float] = None
    phase_max: Optional[float] = None
    reference: str = spectral_phase.REF_DEFAULT
    bands: FrozenSet[str] = field(default_factory=frozenset)
    exclude_flagged: bool = False

    @classmethod
    def from_params(cls, params) -> "PhotometryOptions":
        mjd_min, mjd_max = _range(params, "mjd_min", "mjd_max", "MJD")
        low, high = _range(params, "phase_min", "phase_max", "Phase")
        flagged = (params.get("exclude_flagged") or "").lower() in ("1", "true", "on", "yes")
        return cls(mjd_min=mjd_min, mjd_max=mjd_max, phase_min=low, phase_max=high, reference=_reference(params),
                   bands=_names(params, "band"), exclude_flagged=flagged)

    @property
    def phase_range(self) -> bool:
        return self.phase_min is not None or self.phase_max is not None

    @property
    def active(self) -> bool:
        return bool(self.mjd_min is not None or self.mjd_max is not None or self.phase_range or self.bands
                    or self.exclude_flagged)

    def keeps(self, mjd: float, band: str, data_quality: str, phase: Optional[float] = None) -> bool:
        """Whether a point passes the options (``phase`` None: no epoch under the reference)."""
        if self.mjd_min is not None and mjd < self.mjd_min:
            return False
        if self.mjd_max is not None and mjd > self.mjd_max:
            return False
        if self.bands and (band or "").strip().lower() not in self.bands:
            return False
        if self.exclude_flagged and data_quality:
            return False
        if self.phase_range:
            if phase is None:
                return False
            if self.phase_min is not None and phase < self.phase_min:
                return False
            if self.phase_max is not None and phase > self.phase_max:
                return False
        return True

    def describe(self) -> List[str]:
        lines = []
        if self.mjd_min is not None or self.mjd_max is not None:
            lines.append("MJD range: %s to %s" % ("-inf" if self.mjd_min is None else "%g" % self.mjd_min,
                                                  "+inf" if self.mjd_max is None else "%g" % self.mjd_max))
        if self.phase_range:
            lines.append("Phase range (days, %s): %s to %s; transients without that epoch have no points" % (
                reference_label(self.reference),
                "-inf" if self.phase_min is None else "%g" % self.phase_min,
                "+inf" if self.phase_max is None else "%g" % self.phase_max))
        if self.bands:
            lines.append("Bands: %s" % ", ".join(sorted(self.bands)))
        if self.exclude_flagged:
            lines.append("Points with data-quality flags left out")
        return lines


def transient_reference(transient, reference: str):
    """``(reference MJD, redshift or None, reference used)`` of a transient for photometry phases, or None.

    The same epochs and rules as the spectra's phases (``spectral_phase.compute_phases``), with
    the default resolved to bazin_rest / bazin_obs.
    """
    epochs = spectral_phase.transient_epochs(transient)
    rows = {row["reference"]: row for row in spectral_phase.compute_phases(0.0, epochs)}
    if reference == spectral_phase.REF_DEFAULT:
        row = next((r for r in rows.values() if r["is_default"]), None)
    else:
        row = rows.get(reference)
    if row is None:
        return None
    return row["reference_mjd"], row["redshift"], row["reference"]
