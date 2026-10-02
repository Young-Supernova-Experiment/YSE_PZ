"""Gaia DR3 cone search (#318): is there a star at the transient's position?

Queries ``gaiadr3.gaia_source`` through ESA's TAP ``sync`` endpoint (the same
service ``astroquery.gaia`` wraps; ``GAIA_TAP_URL`` overrides it). The nearest
source's parallax, proper motion, G, BP-RP and RUWE are stored; the verdict is
``stellar`` when the parallax or the total proper motion is significant at
``>= 3 sigma``, ``unknown`` for a match without a significant measurement and
``clean`` when nothing lies within the radius.
"""

from __future__ import annotations

import math

from YSE_App.annotation_services.base import (
    VERDICT_CLEAN,
    VERDICT_STELLAR,
    VERDICT_UNKNOWN,
    CheckResult,
    gaia_tap_url,
    nearest,
    num,
    round_or_none,
    tap_query,
)

SLUG = "gaia_dr3"
SIGNIFICANCE = 3.0
MAX_ROWS = 10

ADQL = (
    "SELECT TOP {top} source_id, ra, dec, parallax, parallax_error, parallax_over_error, "
    "pmra, pmra_error, pmdec, pmdec_error, phot_g_mean_mag, bp_rp, ruwe "
    "FROM gaiadr3.gaia_source "
    "WHERE 1=CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra:.7f}, {dec:.7f}, {radius_deg:.8f}))"
)


def adql(ra: float, dec: float, radius_arcsec: float) -> str:
    return ADQL.format(top=MAX_ROWS, ra=ra, dec=dec, radius_deg=radius_arcsec / 3600.0)


def check(ra: float, dec: float, radius_arcsec: float) -> CheckResult:
    rows = tap_query(gaia_tap_url(), adql(ra, dec, radius_arcsec))
    data = {"catalog": "Gaia DR3", "n_matches": len(rows)}
    if not rows:
        return data, VERDICT_CLEAN, "No Gaia DR3 source within %.1f arcsec." % radius_arcsec
    row, sep = nearest(rows, ra, dec, "ra", "dec")
    plx, plx_err = num(row.get("parallax")), num(row.get("parallax_error"))
    plx_over_err = num(row.get("parallax_over_error"))
    if plx_over_err is None and plx is not None and plx_err:
        plx_over_err = plx / plx_err
    pmra, pmdec = num(row.get("pmra")), num(row.get("pmdec"))
    pmra_err, pmdec_err = num(row.get("pmra_error")), num(row.get("pmdec_error"))
    pm = pm_over_error = None
    if pmra is not None and pmdec is not None:
        pm = math.hypot(pmra, pmdec)
        if pmra_err is not None and pmdec_err is not None and pm > 0:
            pm_err = math.sqrt((pmra * pmra_err) ** 2 + (pmdec * pmdec_err) ** 2) / pm
            pm_over_error = pm / pm_err if pm_err > 0 else None
    data.update({
        "source_id": str(row.get("source_id")) if row.get("source_id") is not None else None,
        "separation_arcsec": sep,
        "parallax": round_or_none(plx),
        "parallax_error": round_or_none(plx_err),
        "parallax_over_error": round_or_none(plx_over_err, 2),
        "pmra": round_or_none(pmra, 3),
        "pmdec": round_or_none(pmdec, 3),
        "pm": round_or_none(pm, 3),
        "pm_over_error": round_or_none(pm_over_error, 2),
        "phot_g_mean_mag": round_or_none(row.get("phot_g_mean_mag"), 3),
        "bp_rp": round_or_none(row.get("bp_rp"), 3),
        "ruwe": round_or_none(row.get("ruwe"), 3),
    })
    significant_plx = plx_over_err is not None and plx_over_err >= SIGNIFICANCE and (plx or 0) > 0
    significant_pm = pm_over_error is not None and pm_over_error >= SIGNIFICANCE
    if significant_plx and plx:
        data["distance_pc"] = round(1000.0 / plx, 1)
    if significant_plx or significant_pm:
        reasons = []
        if significant_plx:
            reasons.append("parallax %.2f +/- %.2f mas" % (plx, plx_err or 0.0))
        if significant_pm:
            reasons.append("proper motion %.1f mas/yr (%.1f sigma)" % (pm, pm_over_error))
        summary = "Gaia DR3 star %.2f arcsec away: %s." % (sep or 0.0, "; ".join(reasons))
        return data, VERDICT_STELLAR, summary
    g = data.get("phot_g_mean_mag")
    summary = "Gaia DR3 source %.2f arcsec away (G=%s) without significant parallax or proper motion." % (
        sep or 0.0, ("%.2f" % g) if g is not None else "?")
    return data, VERDICT_UNKNOWN, summary
