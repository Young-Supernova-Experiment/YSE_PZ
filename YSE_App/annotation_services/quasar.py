"""Quasar-catalogue check (#318): the Million Quasar Catalog (Milliquas) through VizieR TAP.

Milliquas (Flesch 2023, VizieR ``VII/294``) merges the spectroscopic quasar
catalogues (SDSS DR16Q, LAMOST, 2QZ, ...) with photometric candidates, so one
cone search covers the SDSS QSO flag as well. The nearest match's name, type
code, redshift, magnitudes and quasar probability (``Qpct``) are stored; the
verdict is ``AGN-like`` for any match, ``clean`` for none.
"""

from __future__ import annotations

from YSE_App.annotation_services.base import (
    VERDICT_AGN,
    VERDICT_CLEAN,
    CheckResult,
    nearest,
    num,
    round_or_none,
    tap_query,
    vizier_tap_url,
)

SLUG = "quasar"
MAX_ROWS = 10
MILLIQUAS_TABLE = '"VII/294/catalog"'

ADQL = (
    "SELECT TOP {top} RAJ2000, DEJ2000, Name, Type, Rmag, Bmag, Comment, z, Qpct, Xname, Rname "
    "FROM " + MILLIQUAS_TABLE + " "
    "WHERE 1=CONTAINS(POINT('ICRS', RAJ2000, DEJ2000), CIRCLE('ICRS', {ra:.7f}, {dec:.7f}, {radius_deg:.8f}))"
)

# Milliquas type letters (first character): Q quasar, A AGN, B BL Lac, K NLQSO, N NLAGN,
# R radio, X X-ray, 2 lobes ... followed by flags; lower case = photometric candidate.
TYPE_LABELS = {"Q": "quasar", "A": "AGN", "B": "BL Lac", "K": "narrow-line quasar", "N": "narrow-line AGN",
               "q": "photometric quasar candidate", "a": "AGN candidate", "L": "lensed quasar"}


def adql(ra: float, dec: float, radius_arcsec: float) -> str:
    return ADQL.format(top=MAX_ROWS, ra=ra, dec=dec, radius_deg=radius_arcsec / 3600.0)


def check(ra: float, dec: float, radius_arcsec: float) -> CheckResult:
    rows = tap_query(vizier_tap_url(), adql(ra, dec, radius_arcsec))
    data = {"catalog": "Milliquas v8", "n_matches": len(rows)}
    if not rows:
        return data, VERDICT_CLEAN, "No Milliquas quasar or AGN within %.1f arcsec." % radius_arcsec
    row, sep = nearest(rows, ra, dec, "RAJ2000", "DEJ2000")
    type_code = str(row.get("Type") or "").strip()
    label = TYPE_LABELS.get(type_code[:1], "catalogued object") if type_code else "catalogued object"
    qpct = num(row.get("Qpct"))
    z = num(row.get("z"))
    data.update({
        "name": (str(row.get("Name")).strip() if row.get("Name") is not None else None),
        "type": type_code or None,
        "type_label": label,
        "separation_arcsec": sep,
        "redshift": round_or_none(z, 4),
        "rmag": round_or_none(row.get("Rmag"), 2),
        "bmag": round_or_none(row.get("Bmag"), 2),
        "qpct": round_or_none(qpct, 0),
        "comment": (str(row.get("Comment")).strip() or None) if row.get("Comment") is not None else None,
        "xray_name": (str(row.get("Xname")).strip() or None) if row.get("Xname") is not None else None,
        "radio_name": (str(row.get("Rname")).strip() or None) if row.get("Rname") is not None else None,
    })
    parts = ["Milliquas %s %.2f arcsec away" % (label, sep or 0.0)]
    if z is not None:
        parts.append("z = %.3f" % z)
    if qpct is not None:
        parts.append("quasar probability %d%%" % int(round(qpct)))
    return data, VERDICT_AGN, ", ".join(parts) + "."
