"""
Same-origin matching of observations (#888): is a catalogue point or spectrum one YSE_PZ already has?

Two copies of one observation may carry different values (another reduction), so they are matched
by origin, not value:

* photometry: same transient, same filter (``normalize_band``), times within ``PHOT_TIME_TOLERANCE``
  days, and the same instrument;
* spectra: same transient, times within ``SPEC_TIME_TOLERANCE`` days, the same instrument, and
  wavelength ranges overlapping by at least ``SPEC_MIN_OVERLAP`` of the shorter one.

When either side has the ``Unknown`` instrument (most OSC photometry names none) the match is
*weak*; a weak photometry match also needs magnitudes within ``WEAK_MAG_TOLERANCE`` when both have
one. Each existing observation matches at most one catalogue observation (nearest in time first).

:func:`decide` applies Ryan's rule (2026-10-27): YSE_PZ's copy is kept, unless the catalogue copy
comes from a refereed, published paper.

:func:`duplicate_photometry` / :func:`duplicate_spectra` apply the same tests within one transient's
own data and group the copies of each observation (#890, ``manage.py clean_duplicate_data``).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

PHOT_TIME_TOLERANCE = 0.02   # days (about 30 min: start vs mid-exposure, rounding)
SPEC_TIME_TOLERANCE = 0.5    # days (catalogue spectra are often dated to the day)
SPEC_MIN_OVERLAP = 0.5
WEAK_MAG_TOLERANCE = 0.5
# Within one transient's own data (#890) the times of two copies agree much more closely (a repeat
# upload, or our report coming back from TNS), while real repeat exposures in one band can be minutes
# apart (ATLAS quads, ZTF), so the time windows are narrower.
DUPLICATE_PHOT_TIME_TOLERANCE = 0.001   # days (86 s)
DUPLICATE_SPEC_TIME_TOLERANCE = 0.05    # days (72 min)

KEPT_EXISTING, SUPERSEDED_EXISTING, REVIEW, NEW = 'kept_existing', 'superseded_existing', 'review', 'new'

# Filters written differently by different sources.
_BAND_ALIASES = {
    'uvw1': 'UVW1', 'w1': 'UVW1', 'uvm2': 'UVM2', 'm2': 'UVM2', 'uvw2': 'UVW2', 'w2': 'UVW2',
    'uvot-u': 'U', 'uvot-b': 'B', 'uvot-v': 'V',
}


def normalize_band(name: Optional[str]) -> str:
    """``r'`` -> ``r``, ``UVW1``/``W1``/``uvw1`` -> ``UVW1``; case is kept otherwise (r and R differ)."""
    band = (name or '').strip().replace("'", '').replace(' ', '')
    return _BAND_ALIASES.get(band.lower(), band)


@dataclass
class Obs:
    """One observation, catalogue or existing. ``key`` identifies it to the caller (index or id)."""
    key: object
    mjd: float
    instrument_id: Optional[int]
    band: str = ''
    mag: Optional[float] = None
    wavelength_min: Optional[float] = None
    wavelength_max: Optional[float] = None


@dataclass
class Match:
    new: Obs
    existing: Obs
    weak: bool
    time_difference: float


def _same_instrument(a: Obs, b: Obs, unknown_id: Optional[int]):
    """(compatible, weak)."""
    known_a = a.instrument_id is not None and a.instrument_id != unknown_id
    known_b = b.instrument_id is not None and b.instrument_id != unknown_id
    if known_a and known_b:
        return a.instrument_id == b.instrument_id, False
    return True, True


def _overlap(a: Obs, b: Obs) -> float:
    if None in (a.wavelength_min, a.wavelength_max, b.wavelength_min, b.wavelength_max):
        return 1.0  # unknown range: time and instrument decide
    inter = min(a.wavelength_max, b.wavelength_max) - max(a.wavelength_min, b.wavelength_min)
    shorter = min(a.wavelength_max - a.wavelength_min, b.wavelength_max - b.wavelength_min)
    return inter / shorter if shorter > 0 else 0.0


def _pairs(new: Sequence[Obs], existing: Sequence[Obs], unknown_id, tolerance, accept) -> List[Match]:
    candidates = []
    by_time = sorted(existing, key=lambda o: o.mjd)
    times = [o.mjd for o in by_time]
    for n in new:
        lo = bisect.bisect_left(times, n.mjd - tolerance)
        hi = bisect.bisect_right(times, n.mjd + tolerance)
        for e in by_time[lo:hi]:
            ok, weak = _same_instrument(n, e, unknown_id)
            if ok and accept(n, e, weak):
                candidates.append(Match(n, e, weak, abs(n.mjd - e.mjd)))
    # One-to-one, nearest in time first; strong matches before weak at equal time.
    candidates.sort(key=lambda m: (m.time_difference, m.weak))
    used_new, used_existing, out = set(), set(), []
    for m in candidates:
        if id(m.new) in used_new or id(m.existing) in used_existing:
            continue
        used_new.add(id(m.new))
        used_existing.add(id(m.existing))
        out.append(m)
    return out


def _photometry_accept(weak_mag_tolerance):
    def accept(n, e, weak):
        if normalize_band(n.band) != normalize_band(e.band):
            return False
        if weak and n.mag is not None and e.mag is not None and abs(n.mag - e.mag) > weak_mag_tolerance:
            return False
        return True

    return accept


def _spectra_accept(min_overlap):
    return lambda n, e, weak: _overlap(n, e) >= min_overlap


def match_photometry(new: Sequence[Obs], existing: Sequence[Obs], unknown_id: Optional[int] = None, *,
                     tolerance: float = PHOT_TIME_TOLERANCE, weak_mag_tolerance: float = WEAK_MAG_TOLERANCE) -> List[Match]:
    return _pairs(new, existing, unknown_id, tolerance, _photometry_accept(weak_mag_tolerance))


def match_spectra(new: Sequence[Obs], existing: Sequence[Obs], unknown_id: Optional[int] = None, *,
                  tolerance: float = SPEC_TIME_TOLERANCE, min_overlap: float = SPEC_MIN_OVERLAP) -> List[Match]:
    return _pairs(new, existing, unknown_id, tolerance, _spectra_accept(min_overlap))


def _groups(observations: Sequence[Obs], unknown_id, tolerance, accept) -> List[List[Obs]]:
    """Copies of one observation within one list, in time order; only groups of two or more.

    An observation joins a group only if it matches every member (no chaining of a time series
    through near neighbours); otherwise the earliest group it fits, else a new one.
    """
    def same(a, b):
        ok, weak = _same_instrument(a, b, unknown_id)
        return ok and abs(a.mjd - b.mjd) <= tolerance and accept(a, b, weak)

    groups: List[List[Obs]] = []
    open_groups: List[List[Obs]] = []
    for o in sorted(observations, key=lambda x: (x.mjd, str(x.key))):
        open_groups = [g for g in open_groups if o.mjd - g[0].mjd <= tolerance]
        for g in open_groups:
            if all(same(o, m) for m in g):
                g.append(o)
                break
        else:
            g = [o]
            groups.append(g)
            open_groups.append(g)
    return [g for g in groups if len(g) > 1]


def duplicate_photometry(observations: Sequence[Obs], unknown_id: Optional[int] = None, *,
                         tolerance: float = DUPLICATE_PHOT_TIME_TOLERANCE,
                         weak_mag_tolerance: float = WEAK_MAG_TOLERANCE) -> List[List[Obs]]:
    """Groups of points within one transient that are copies of one observation (same tests as
    :func:`match_photometry`)."""
    return _groups(observations, unknown_id, tolerance, _photometry_accept(weak_mag_tolerance))


def duplicate_spectra(observations: Sequence[Obs], unknown_id: Optional[int] = None, *,
                      tolerance: float = DUPLICATE_SPEC_TIME_TOLERANCE,
                      min_overlap: float = SPEC_MIN_OVERLAP) -> List[List[Obs]]:
    """Groups of spectra within one transient that are copies of one observation (same tests as
    :func:`match_spectra`)."""
    return _groups(observations, unknown_id, tolerance, _spectra_accept(min_overlap))


def decide(match: Optional[Match], refereed: bool) -> str:
    """What to do with a catalogue observation: ``new`` (no match), keep YSE's copy, supersede it, or review."""
    if match is None:
        return NEW
    if not refereed:
        return KEPT_EXISTING
    return REVIEW if match.weak else SUPERSEDED_EXISTING


# ---- database side ---------------------------------------------------------------------------

def existing_photometry(transient_id: int, exclude_photometry_ids: Iterable[int] = ()) -> List[Obs]:
    """The transient's photometry points as Obs (key = TransientPhotData id)."""
    from YSE_App.models import TransientPhotData

    rows = (TransientPhotData.objects.filter(photometry__transient_id=transient_id)
            .exclude(photometry_id__in=list(exclude_photometry_ids))
            .values_list('id', 'obs_date', 'band__name', 'photometry__instrument_id', 'mag'))
    return [Obs(key=pk, mjd=_mjd(obs_date), instrument_id=inst, band=band or '', mag=mag)
            for pk, obs_date, band, inst, mag in rows]


def existing_spectra(transient_id: int, exclude_ids: Iterable[int] = ()) -> List[Obs]:
    """The transient's spectra as Obs (key = TransientSpectrum id), with wavelength ranges."""
    from YSE_App.common.spectrum_arrays import wavelength_ranges
    from YSE_App.models import TransientSpectrum

    spectra = list(TransientSpectrum.objects.filter(transient_id=transient_id).exclude(id__in=list(exclude_ids))
                   .values_list('id', 'obs_date', 'instrument_id'))
    ranges = wavelength_ranges(s[0] for s in spectra)
    return [Obs(key=sid, mjd=_mjd(obs_date), instrument_id=inst,
                wavelength_min=ranges.get(sid, (None, None))[0], wavelength_max=ranges.get(sid, (None, None))[1])
            for sid, obs_date, inst in spectra]


def _mjd(dt) -> float:
    import datetime

    epoch = datetime.datetime(1858, 11, 17, tzinfo=datetime.timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return (dt - epoch).total_seconds() / 86400.0
