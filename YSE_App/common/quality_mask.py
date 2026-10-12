"""Bit definitions for ``quality_mask`` and ``pixel_mask`` (#79).

Final spec: issue #79, "FINAL quality_mask spec (v6)". Human-readable tables and
the rules for adding a flag: ``docs/quality-mask.md``.

Rules
-----
* Each set bit is a *deficiency*; a flag is set only when it applies, so
  ``0`` is the best possible data.
* Higher bits are more serious. Integer order therefore triages quality:
  ``mask < 2**k`` means "nothing at bit k or above". Use :func:`quality_lt`
  for the queryset form.
* Flags sit on even bits; odd bits are reserved so that a flag can later be
  inserted between two others without renumbering stored masks.
* Fixed positions in every set: ``REJECTED = 51``, ``OTHER = 50`` (catch-all,
  someone should look), ``NOT_PUBLISHED = 0``. Publication is tested as a bit
  (:func:`is_published`), never with ``<``: good data can simply be
  unpublished, and every ``<`` cut ignores bit 0.
* Every value stays below ``2**53``, so masks are exact in JavaScript/Bokeh.

There is one photometry table (shared by ``TransientPhotData`` and
``HostPhotData``, each using a subset) and one spectra table (shared by
``TransientSpectrum`` and ``HostSpectrum``; bit 18 is HOST_CONTAMINATED on a
transient spectrum and TRANSIENT_CONTAMINATED on a host spectrum).

This module only defines bits; it never touches the database.
"""

from __future__ import annotations

import enum
from typing import Dict, Iterable, List, Optional

from django.db import models
from django.db.models import Q

# --------------------------------------------------------------------------
# Positions shared by every set
# --------------------------------------------------------------------------

REJECTED_BIT = 51
OTHER_BIT = 50
NOT_PUBLISHED_BIT = 0

#: Highest bit any set may use. Keeps every mask below 2**53 (JS-safe).
MAX_BIT = 51

# --------------------------------------------------------------------------
# Named thresholds (the same in every set). Compare with ``<``.
# --------------------------------------------------------------------------

#: ``mask < USABLE``: not rejected.
USABLE = 1 << REJECTED_BIT
#: ``mask < NO_OTHER``: not rejected and no unexplained (OTHER) issue.
NO_OTHER = 1 << OTHER_BIT


class PhotFlag(enum.IntFlag):
    """Photometry bits for ``TransientPhotData`` and ``HostPhotData`` (least to most serious)."""

    NOT_PUBLISHED = 1 << 0
    NON_PHOTOMETRIC = 1 << 2        # transient
    HIGH_BACKGROUND = 1 << 4
    APERTURE = 1 << 6               # transient: aperture, not PSF
    NOT_FORCED = 1 << 8             # transient
    NOT_FULLY_REDUCED = 1 << 10
    NO_SUPERCAL = 1 << 12           # transient
    NOT_DIFFERENCED = 1 << 14       # transient
    NOT_BKG_SUBTRACTED = 1 << 16    # transient
    POOR_SEEING = 1 << 18           # or bad PSF fit
    LOW_SN = 1 << 20                # host
    TEMPLATE_SUSPECT = 1 << 22      # transient
    BAD_SUBTRACTION = 1 << 23       # difference image is bad (legacy DataQuality 'Bad Subtraction'; v7)
    QUICKLOOK = 1 << 24
    BLENDED = 1 << 26
    SHREDDED = 1 << 28              # host: split by the deblender
    TRANSIENT_CONTAMINATED = 1 << 30  # host
    NEAR_EDGE = 1 << 32
    NOT_CALIBRATED = 1 << 34
    CR = 1 << 36
    TRAIL_GHOST = 1 << 38           # satellite, asteroid, reflection
    BAD_PIXEL = 1 << 40
    MASKED = 1 << 41                # masked (legacy DataQuality 'Masking'; v7)
    SATURATED = 1 << 42             # or non-linear
    OTHER = 1 << OTHER_BIT
    REJECTED = 1 << REJECTED_BIT


class SpecFlag(enum.IntFlag):
    """Spectrum bits for ``TransientSpectrum`` (least to most serious)."""

    NOT_PUBLISHED = 1 << 0
    NOT_ABS_CALIBRATED = 1 << 2     # not scaled to photometry
    STITCHED = 1 << 4
    BOXCAR_EXTRACTION = 1 << 6      # good = optimal extraction
    DEREDSHIFTED = 1 << 8           # good = observed frame
    DEREDDENED = 1 << 10            # good = not dereddened
    HIGH_AIRMASS = 1 << 12
    LOW_SN = 1 << 14
    NOT_FULLY_REDUCED = 1 << 16
    HOST_CONTAMINATED = 1 << 18
    QUICKLOOK = 1 << 20
    NOT_PARALLACTIC = 1 << 22
    WAVELENGTH_SUSPECT = 1 << 24
    SECOND_ORDER = 1 << 26
    NOT_TELLURIC_CORRECTED = 1 << 28
    WAVELENGTH_MEDIUM_UNKNOWN = 1 << 30  # derived from ``wavelength_medium``
    NOT_FLUX_CALIBRATED = 1 << 32
    OTHER = 1 << OTHER_BIT
    REJECTED = 1 << REJECTED_BIT


class HostSpecFlag(enum.IntFlag):
    """Spectrum bits for ``HostSpectrum``: as :class:`SpecFlag`, bit 18 is TRANSIENT_CONTAMINATED."""

    NOT_PUBLISHED = 1 << 0
    NOT_ABS_CALIBRATED = 1 << 2
    STITCHED = 1 << 4
    BOXCAR_EXTRACTION = 1 << 6
    DEREDSHIFTED = 1 << 8
    DEREDDENED = 1 << 10
    HIGH_AIRMASS = 1 << 12
    LOW_SN = 1 << 14
    NOT_FULLY_REDUCED = 1 << 16
    TRANSIENT_CONTAMINATED = 1 << 18
    QUICKLOOK = 1 << 20
    NOT_PARALLACTIC = 1 << 22
    WAVELENGTH_SUSPECT = 1 << 24
    SECOND_ORDER = 1 << 26
    NOT_TELLURIC_CORRECTED = 1 << 28
    WAVELENGTH_MEDIUM_UNKNOWN = 1 << 30
    NOT_FLUX_CALIBRATED = 1 << 32
    OTHER = 1 << OTHER_BIT
    REJECTED = 1 << REJECTED_BIT


class PixelFlag(enum.IntFlag):
    """``pixel_mask`` on ``TransientSpecData`` / ``HostSpecData``: any set bit excludes the pixel."""

    BAD_PIXEL = 1 << 0  # or chip gap
    CR = 1 << 1
    SKY_RESIDUAL = 1 << 2
    FRINGING = 1 << 3


def _members(flag_cls) -> List[enum.IntFlag]:
    """Single-bit members of ``flag_cls`` in bit order (no aliases, no composites)."""
    return sorted(flag_cls.__members__.values(), key=int)


def _union(flags: Iterable[enum.IntFlag]) -> int:
    out = 0
    for f in flags:
        out |= int(f)
    return out


_SHARED_PHOT = {
    PhotFlag.NOT_PUBLISHED, PhotFlag.HIGH_BACKGROUND, PhotFlag.NOT_FULLY_REDUCED,
    PhotFlag.POOR_SEEING, PhotFlag.QUICKLOOK, PhotFlag.BLENDED, PhotFlag.NEAR_EDGE,
    PhotFlag.NOT_CALIBRATED, PhotFlag.CR, PhotFlag.TRAIL_GHOST, PhotFlag.BAD_PIXEL,
    PhotFlag.BAD_SUBTRACTION, PhotFlag.MASKED,
    PhotFlag.SATURATED, PhotFlag.OTHER, PhotFlag.REJECTED,
}
#: Bits a ``TransientPhotData.quality_mask`` may use.
TRANSIENT_PHOT_ALLOWED = _union(_SHARED_PHOT | {
    PhotFlag.NON_PHOTOMETRIC, PhotFlag.APERTURE, PhotFlag.NOT_FORCED, PhotFlag.NO_SUPERCAL,
    PhotFlag.NOT_DIFFERENCED, PhotFlag.NOT_BKG_SUBTRACTED, PhotFlag.TEMPLATE_SUSPECT,
})
#: Bits a ``HostPhotData.quality_mask`` may use.
HOST_PHOT_ALLOWED = _union(_SHARED_PHOT | {
    PhotFlag.LOW_SN, PhotFlag.SHREDDED, PhotFlag.TRANSIENT_CONTAMINATED,
})
SPEC_ALLOWED = _union(_members(SpecFlag))
HOST_SPEC_ALLOWED = _union(_members(HostSpecFlag))
PIXEL_ALLOWED = _union(_members(PixelFlag))

#: The flag class and allowed bits per model, keyed by model name.
FLAG_SETS = {
    'TransientPhotData': (PhotFlag, TRANSIENT_PHOT_ALLOWED),
    'HostPhotData': (PhotFlag, HOST_PHOT_ALLOWED),
    'TransientSpectrum': (SpecFlag, SPEC_ALLOWED),
    'HostSpectrum': (HostSpecFlag, HOST_SPEC_ALLOWED),
    'TransientSpecData': (PixelFlag, PIXEL_ALLOWED),
    'HostSpecData': (PixelFlag, PIXEL_ALLOWED),
}

# --------------------------------------------------------------------------
# Group boundaries. ``mask < X`` = nothing in X's group or any more serious one.
# --------------------------------------------------------------------------

#: Photometry: no image artefact or calibration problem (bits 32-42: edge,
#: calibration, CR, trail/ghost, bad pixel, saturation).
PHOT_CLEAN = 1 << 32
#: Photometry: also not quicklook, blended, shredded or contaminated (bits 24-30).
PHOT_SCIENCE = 1 << 24
#: Photometry: also no reduction-stage issue (bits 10-22: not fully reduced,
#: supercal, differencing, background, seeing/PSF, low S/N, template).
PHOT_REDUCED = 1 << 10
#: Photometry: also no measurement-method caveat (bits 2-8); only NOT_PUBLISHED may be set.
PHOT_BEST = 1 << 2

#: Spectra: no calibration problem (bits 22-32: parallactic angle, wavelength
#: solution, second order, telluric, wavelength medium, flux calibration).
SPEC_CALIBRATED = 1 << 22
#: Spectra: also not quicklook (bit 20).
SPEC_SCIENCE = 1 << 20
#: Spectra: also no observing or reduction caveat (bits 12-18: airmass,
#: low S/N, not fully reduced, contamination).
SPEC_GOOD_CONDITIONS = 1 << 12
#: Spectra: also no processing choice flagged (bits 2-10); only NOT_PUBLISHED may be set.
SPEC_BEST = 1 << 2

# --------------------------------------------------------------------------
# Choices for the metadata fields that sit beside the mask
# --------------------------------------------------------------------------


class WavelengthMedium(models.TextChoices):
    VACUUM = 'vacuum', 'Vacuum'
    AIR = 'air', 'Air'
    UNKNOWN = 'unknown', 'Unknown'


class HostSubtraction(models.TextChoices):
    LOCAL = 'local', 'Local'
    ARCHIVAL = 'archival', 'Archival'
    TEMPLATE = 'template', 'Template'
    NONE = 'none', 'None'
    UNKNOWN = 'unknown', 'Unknown'


class PhotType(models.TextChoices):
    TOTAL = 'total', 'Total'
    R25 = 'r25', 'R25'
    KRON = 'kron', 'Kron'
    PETROSIAN = 'petrosian', 'Petrosian'
    APERTURE = 'aperture', 'Aperture'
    MODEL = 'model', 'Model'
    PSF = 'psf', 'PSF'
    OTHER = 'other', 'Other'


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def is_published(mask: Optional[int]) -> bool:
    """Whether NOT_PUBLISHED (bit 0) is clear. A bit test: never use ``<`` for this."""
    return not (int(mask or 0) & (1 << NOT_PUBLISHED_BIT))


def flags_from_mask(mask: Optional[int], flag_cls=PhotFlag) -> List[str]:
    """Names of the flags set in ``mask``, least serious first.

    Bits that ``flag_cls`` does not define (reserved, or another set's) are
    reported as ``BIT_<n>`` so nothing is silently dropped.
    """
    mask = int(mask or 0)
    if mask < 0:
        raise ValueError('quality masks are non-negative')
    known = {int(f): f.name for f in _members(flag_cls)}
    out = []
    bit = 0
    while mask >> bit:
        value = 1 << bit
        if mask & value:
            out.append(known.get(value, 'BIT_%d' % bit))
        bit += 1
    return out


def mask_from_flags(flags: Iterable, flag_cls=PhotFlag) -> int:
    """The mask for ``flags`` (names or members of ``flag_cls``); unknown names raise ``KeyError``."""
    mask = 0
    for f in flags:
        if isinstance(f, str):
            f = flag_cls[f.upper()]
        elif not isinstance(f, flag_cls):
            raise TypeError('%r is not a %s' % (f, flag_cls.__name__))
        mask |= int(f)
    return mask


def quality_lt(threshold: int, field: str = 'quality_mask') -> Q:
    """``Q(quality_mask__lt=threshold)``: rows with nothing at the threshold's bit or above."""
    return Q(**{'%s__lt' % field: int(threshold)})


def wavelength_medium_unknown(medium: Optional[str]) -> bool:
    """A missing (NULL) or ``unknown`` medium counts as unknown; air and vacuum are both fine."""
    return medium not in (WavelengthMedium.AIR, WavelengthMedium.VACUUM)


def apply_wavelength_medium(mask: Optional[int], medium: Optional[str]) -> int:
    """``mask`` with WAVELENGTH_MEDIUM_UNKNOWN (bit 30) set or cleared to match ``medium``."""
    bit = int(SpecFlag.WAVELENGTH_MEDIUM_UNKNOWN)
    mask = int(mask or 0)
    return (mask | bit) if wavelength_medium_unknown(medium) else (mask & ~bit)


def allowed_mask(model_name: str) -> int:
    """The bits ``model_name``'s mask may use (see :data:`FLAG_SETS`)."""
    return FLAG_SETS[model_name][1]


# --------------------------------------------------------------------------
# DataQuality names -> bits (#81)
# --------------------------------------------------------------------------
# Spec v7 (#79): while the DataQuality ManyToMany stays in use, the mask
# follows it. Each name sets its own bit; only "Bad" sets REJECTED. A name with
# no bit here adds OTHER. Names compare case-insensitively, ignoring spaces.
#
# Today every row with any DataQuality is left out of the "good data" queries,
# so the readers keep exactly that behaviour with ``mask & LEGACY_EXCLUDE == 0``
# (:func:`legacy_good`), where LEGACY_EXCLUDE is the OR of every bit this
# mapping can set. Bits that never come from DataQuality (NOT_PUBLISHED,
# WAVELENGTH_MEDIUM_UNKNOWN, ...) do not exclude data. ``<`` triage cuts are
# opt-in per view.

DATA_QUALITY_PHOT_BITS = {
    'bad': int(PhotFlag.REJECTED),
    'quicklook': int(PhotFlag.QUICKLOOK),
    'masking': int(PhotFlag.MASKED),
    'bad subtraction': int(PhotFlag.BAD_SUBTRACTION),
    'saturation': int(PhotFlag.SATURATED),
    'crowding': int(PhotFlag.BLENDED),
    'bad calibration': int(PhotFlag.NOT_CALIBRATED),
}
DATA_QUALITY_SPEC_BITS = {
    'bad': int(SpecFlag.REJECTED),
    'quicklook': int(SpecFlag.QUICKLOOK),
    'bad calibration': int(SpecFlag.NOT_FLUX_CALIBRATED),
}
_DATA_QUALITY_BITS = {
    'TransientPhotData': DATA_QUALITY_PHOT_BITS,
    'HostPhotData': DATA_QUALITY_PHOT_BITS,
    'TransientSpectrum': DATA_QUALITY_SPEC_BITS,
    'HostSpectrum': DATA_QUALITY_SPEC_BITS,
}

#: Every bit the legacy DataQuality mapping can set (mapped bits plus OTHER).
PHOT_LEGACY_EXCLUDE = _union(DATA_QUALITY_PHOT_BITS.values()) | NO_OTHER
SPEC_LEGACY_EXCLUDE = _union(DATA_QUALITY_SPEC_BITS.values()) | NO_OTHER
LEGACY_EXCLUDE = {
    'TransientPhotData': PHOT_LEGACY_EXCLUDE,
    'HostPhotData': PHOT_LEGACY_EXCLUDE,
    'TransientSpectrum': SPEC_LEGACY_EXCLUDE,
    'HostSpectrum': SPEC_LEGACY_EXCLUDE,
}


def data_quality_bit(name: Optional[str], model_name: str) -> int:
    """The bit DataQuality ``name`` sets on ``model_name``; OTHER if it has none."""
    bit = _DATA_QUALITY_BITS[model_name].get((name or '').strip().lower())
    if bit is None or not (bit & allowed_mask(model_name)):
        return 1 << OTHER_BIT
    return bit


def data_quality_mask(names: Iterable[Optional[str]], model_name: str) -> int:
    """The DataQuality-managed part of the mask for a row flagged with ``names`` (0 if none)."""
    mask = 0
    for name in names:
        mask |= data_quality_bit(name, model_name)
    return mask


def data_quality_managed(model_name: str) -> int:
    """Bits that :func:`merge_data_quality` owns on ``model_name``: :data:`LEGACY_EXCLUDE`."""
    return LEGACY_EXCLUDE[model_name]


def merge_data_quality(old_mask: Optional[int], names: Iterable[Optional[str]], model_name: str) -> int:
    """``old_mask`` with its DataQuality-managed bits replaced by those of ``names``; other bits kept."""
    return (int(old_mask or 0) & ~data_quality_managed(model_name)) | data_quality_mask(names, model_name)


def is_legacy_flagged(mask: Optional[int], model_name: str) -> bool:
    """Whether ``mask`` carries a bit the legacy DataQuality mapping sets (the old "flagged")."""
    return bool(int(mask or 0) & LEGACY_EXCLUDE[model_name])


class BitsClear(models.Lookup):
    """``field__bits_clear=X``: ``(field & X) = 0`` (registered on PositiveBigIntegerField)."""

    lookup_name = 'bits_clear'

    def as_sql(self, compiler, connection):
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        return '(%s & %s) = 0' % (lhs, rhs), list(lhs_params) + list(rhs_params)


models.PositiveBigIntegerField.register_lookup(BitsClear)


def legacy_good(model_name: str, field: str = 'quality_mask') -> Q:
    """The "good data" filter: no bit the legacy DataQuality mapping sets (spec v7)."""
    return Q(**{'%s__bits_clear' % field: LEGACY_EXCLUDE[model_name]})


def describe() -> Dict[str, List[tuple]]:
    """``{set name: [(bit, flag name), ...]}``; used by the docs test to keep the tables in step."""
    out = {}
    for label, cls in (('phot', PhotFlag), ('spec', SpecFlag), ('host_spec', HostSpecFlag),
                       ('pixel', PixelFlag)):
        out[label] = [(int(f).bit_length() - 1, f.name) for f in _members(cls)]
    return out
