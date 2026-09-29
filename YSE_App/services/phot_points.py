"""One classification of a photometry row, shared by the light-curve plot
and the stored statistics (#368).

``view_utils.lightcurveplot_detail`` decided on its own which rows are
detections and which are upper limits, and ``services.photstat`` had a
second, different rule ("a magnitude means a detection").  ZTF forced
photometry uploads a ``mag``/``mag_err`` for every epoch, S/N < 3 ones
included, so the plot drew inverted triangles before the first detection
while the statistics counted the same rows as detections and reported no
pre-detection limits.  Both now call the two predicates here.

* :func:`is_detection`: ``mag`` and ``mag_err`` set and finite, and either
  no flux information or ``mag_err <= MAX_MAG_ERR`` (a magnitude error of
  0.36 is S/N 3; a noisier forced-photometry magnitude is not a detection);
* :func:`limiting_mag`: ``flux`` and ``flux_err`` set, ``flux_err != 0``
  and ``flux / flux_err < LIMIT_SNR``; the value is
  ``-2.5 log10(flux + 3 flux_err) + zp`` (``DEFAULT_ZERO_POINT`` when the
  row has no zero point), ``None`` when that is not a finite number.

A row can satisfy both (a magnitude with a small error whose flux is below
3 sigma); the plot draws both markers, the statistics count it as a
detection.  Neither predicate looks at ``data_quality``: the plot renders
flagged rows (the tooltip shows the flag), the statistics and the Bazin fit
skip them.
"""

from __future__ import annotations

import math
from typing import Optional

MAX_MAG_ERR = 0.36
LIMIT_SNR = 3.0
DEFAULT_ZERO_POINT = 27.5


def finite(value) -> Optional[float]:
    """``float(value)`` when it is a finite number, else ``None``."""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def is_detection(mag, mag_err, flux=None, flux_err=None) -> bool:
    """The light-curve plot's detection rule (see the module docstring)."""
    mag, mag_err = finite(mag), finite(mag_err)
    if mag is None or mag_err is None:
        return False
    if flux is not None and flux_err is not None and mag_err > MAX_MAG_ERR:
        return False
    return True


def limiting_mag(flux, flux_err, zero_point=None) -> Optional[float]:
    """The light-curve plot's upper limit for a row, or ``None`` when the row is not one."""
    flux, flux_err = finite(flux), finite(flux_err)
    if flux is None or flux_err is None or flux_err == 0:
        return None
    if flux / flux_err >= LIMIT_SNR:
        return None
    total = flux + 3.0 * flux_err
    if total <= 0:
        return None
    zp = finite(zero_point)
    if zp is None:
        zp = DEFAULT_ZERO_POINT
    value = -2.5 * math.log10(total) + zp
    return value if math.isfinite(value) else None
