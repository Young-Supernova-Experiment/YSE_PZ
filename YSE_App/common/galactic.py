"""Galactic coordinates of a transient, in Python and as database expressions (#286).

``Transient.gal_l`` / ``Transient.gal_b`` are stored columns: ``Transient.save``
fills them from ``ra`` / ``dec`` with :func:`galactic_coords`, migration
``0027_transient_galactic_coords`` and ``manage.py backfill_galactic_coords``
fill existing rows in one ``UPDATE`` with the expression forms below, and the
search filters (``gal_b_abs_min`` / ``gal_b_abs_max``, ``ordering=gal_b``) read
the indexed ``gal_b`` column.

Both forms are the same closed-form rotation from ICRS/J2000 to the IAU 1958
galactic frame (north galactic pole at RA 192.85948, Dec +27.12825; the north
celestial pole at l = 122.93192), the values astropy uses; the tests check the
Python and SQL forms against astropy and against each other.  This module
imports no models so the migration can use it.
"""

import math

from django.db.models import F, FloatField, Value
from django.db.models.functions import ASin, ATan2, Cos, Degrees, Greatest, Least, Mod, Radians, Sin

# North galactic pole, J2000 (Reid & Brunthaler 2004 values used by astropy).
NGP_RA_DEG = 192.85948
NGP_DEC_DEG = 27.12825
# Galactic longitude of the north celestial pole.
NCP_L_DEG = 122.93192

_SIN_DEC_NGP = math.sin(math.radians(NGP_DEC_DEG))
_COS_DEC_NGP = math.cos(math.radians(NGP_DEC_DEG))
_RA_NGP_RAD = math.radians(NGP_RA_DEG)


def galactic_coords(ra_deg, dec_deg):
    """``(l, b)`` in degrees for ``ra_deg`` / ``dec_deg`` (J2000); ``(None, None)`` when unusable."""
    try:
        ra = math.radians(float(ra_deg))
        dec = math.radians(float(dec_deg))
    except (TypeError, ValueError):
        return None, None
    if not (math.isfinite(ra) and math.isfinite(dec)):
        return None, None
    dra = ra - _RA_NGP_RAD
    sin_b = math.sin(dec) * _SIN_DEC_NGP + math.cos(dec) * _COS_DEC_NGP * math.cos(dra)
    b = math.degrees(math.asin(max(-1.0, min(1.0, sin_b))))
    y = math.cos(dec) * math.sin(dra)
    x = math.sin(dec) * _COS_DEC_NGP - math.cos(dec) * _SIN_DEC_NGP * math.cos(dra)
    l = (NCP_L_DEG - math.degrees(math.atan2(y, x))) % 360.0
    return l, b


def _float(value):
    return Value(float(value), output_field=FloatField())


def _clamped(expr):
    """``expr`` clamped to [-1, 1] so rounding never takes ASIN out of range."""
    return Least(_float(1.0), Greatest(_float(-1.0), expr))


def galactic_latitude_expression(ra='ra', dec='dec'):
    """Galactic latitude ``b`` (degrees) of the row's ``ra`` / ``dec`` columns, evaluated per row."""
    dec = Radians(F(dec))
    ra = Radians(F(ra))
    sin_b = (
        Sin(dec) * _float(_SIN_DEC_NGP)
        + Cos(dec) * _float(_COS_DEC_NGP) * Cos(ra - _float(_RA_NGP_RAD))
    )
    return Degrees(ASin(_clamped(sin_b)))


def galactic_longitude_expression(ra='ra', dec='dec'):
    """Galactic longitude ``l`` in [0, 360) degrees of the row's ``ra`` / ``dec`` columns."""
    dec = Radians(F(dec))
    ra = Radians(F(ra))
    dra = ra - _float(_RA_NGP_RAD)
    y = Cos(dec) * Sin(dra)
    x = Sin(dec) * _float(_COS_DEC_NGP) - Cos(dec) * _float(_SIN_DEC_NGP) * Cos(dra)
    raw = _float(NCP_L_DEG) - Degrees(ATan2(y, x))
    # MOD keeps the sign of its dividend on both MySQL and sqlite, so shift first.
    return Mod(raw + _float(360.0), _float(360.0), output_field=FloatField())


def backfill_galactic_coords(transient_model, *, all_rows=False):
    """Fill ``gal_l`` / ``gal_b`` from ``ra`` / ``dec`` in one UPDATE; returns the row count.

    ``transient_model`` may be the historical model of a migration. Only rows
    with ``gal_b IS NULL`` are touched unless ``all_rows`` is set.
    """
    qs = transient_model.objects.all()
    if not all_rows:
        qs = qs.filter(gal_b__isnull=True)
    return qs.update(
        gal_l=galactic_longitude_expression(),
        gal_b=galactic_latitude_expression(),
    )


__all__ = [
    'NGP_RA_DEG', 'NGP_DEC_DEG', 'NCP_L_DEG',
    'galactic_coords', 'galactic_latitude_expression', 'galactic_longitude_expression',
    'backfill_galactic_coords',
]
