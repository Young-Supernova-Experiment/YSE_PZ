"""WISE colour check (#318): AllWISE, falling back to CatWISE2020, through VizieR TAP.

Stores W1, W2, W3 (W4 when present) with errors and the W1-W2 and W2-W3
colours of the nearest source. The verdict is ``AGN-like`` when
``W1 - W2 >= 0.8`` (the Stern et al. 2012 cut), ``clean`` otherwise and for
no match.
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

SLUG = "wise"
W1_W2_AGN_CUT = 0.8
MAX_ROWS = 10

ALLWISE_TABLE = '"II/328/allwise"'
CATWISE_TABLE = '"II/365/catwise"'

ALLWISE_ADQL = (
    "SELECT TOP {top} AllWISE, RAJ2000, DEJ2000, W1mag, e_W1mag, W2mag, e_W2mag, W3mag, e_W3mag, W4mag, e_W4mag, ccf, ex "
    "FROM " + ALLWISE_TABLE + " "
    "WHERE 1=CONTAINS(POINT('ICRS', RAJ2000, DEJ2000), CIRCLE('ICRS', {ra:.7f}, {dec:.7f}, {radius_deg:.8f}))"
)
CATWISE_ADQL = (
    "SELECT TOP {top} Name, RA_ICRS, DE_ICRS, W1mproPM, e_W1mproPM, W2mproPM, e_W2mproPM "
    "FROM " + CATWISE_TABLE + " "
    "WHERE 1=CONTAINS(POINT('ICRS', RA_ICRS, DE_ICRS), CIRCLE('ICRS', {ra:.7f}, {dec:.7f}, {radius_deg:.8f}))"
)


def _fmt(template: str, ra: float, dec: float, radius_arcsec: float) -> str:
    return template.format(top=MAX_ROWS, ra=ra, dec=dec, radius_deg=radius_arcsec / 3600.0)


def check(ra: float, dec: float, radius_arcsec: float) -> CheckResult:
    url = vizier_tap_url()
    rows = tap_query(url, _fmt(ALLWISE_ADQL, ra, dec, radius_arcsec))
    catalog, ra_key, dec_key, name_key = "AllWISE", "RAJ2000", "DEJ2000", "AllWISE"
    w1_key, w2_key, w3_key, w4_key = "W1mag", "W2mag", "W3mag", "W4mag"
    if not rows:
        rows = tap_query(url, _fmt(CATWISE_ADQL, ra, dec, radius_arcsec))
        catalog, ra_key, dec_key, name_key = "CatWISE2020", "RA_ICRS", "DE_ICRS", "Name"
        w1_key, w2_key, w3_key, w4_key = "W1mproPM", "W2mproPM", None, None
    data = {"catalog": catalog, "n_matches": len(rows)}
    if not rows:
        return data, VERDICT_CLEAN, "No AllWISE or CatWISE2020 source within %.1f arcsec." % radius_arcsec
    row, sep = nearest(rows, ra, dec, ra_key, dec_key)
    w1, w2 = num(row.get(w1_key)), num(row.get(w2_key))
    w3 = num(row.get(w3_key)) if w3_key else None
    w4 = num(row.get(w4_key)) if w4_key else None
    data.update({
        "designation": row.get(name_key),
        "separation_arcsec": sep,
        "w1": round_or_none(w1, 3), "e_w1": round_or_none(row.get("e_" + w1_key), 3),
        "w2": round_or_none(w2, 3), "e_w2": round_or_none(row.get("e_" + w2_key), 3),
    })
    if w3_key:
        data["w3"] = round_or_none(w3, 3)
        data["e_w3"] = round_or_none(row.get("e_" + w3_key), 3)
    if w4_key:
        data["w4"] = round_or_none(w4, 3)
    if row.get("ccf") is not None:
        data["ccf"] = str(row.get("ccf")).strip() or None
    if row.get("ex") is not None:
        data["ext_flag"] = num(row.get("ex"))
    w1_w2 = (w1 - w2) if (w1 is not None and w2 is not None) else None
    w2_w3 = (w2 - w3) if (w2 is not None and w3 is not None) else None
    data["w1_w2"] = round_or_none(w1_w2, 3)
    data["w2_w3"] = round_or_none(w2_w3, 3)
    if w1_w2 is not None and w1_w2 >= W1_W2_AGN_CUT:
        return data, VERDICT_AGN, "%s source %.2f arcsec away with W1-W2 = %.2f (>= %.1f, AGN-like)." % (
            catalog, sep or 0.0, w1_w2, W1_W2_AGN_CUT)
    colour = ("W1-W2 = %.2f" % w1_w2) if w1_w2 is not None else "no W1-W2 colour"
    return data, VERDICT_CLEAN, "%s source %.2f arcsec away, %s (not AGN-like)." % (catalog, sep or 0.0, colour)
