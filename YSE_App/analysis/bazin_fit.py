"""Bazin light-curve fit as an in-process analysis service (#225, #312).

Wraps :func:`YSE_App.services.bazin.fit_bazin_joint` (the joint,
wavelength-correlated fit behind the detail page's "Show Bazin Fit" overlay
and the observing-night "Bazin Mag" column) so a fit can be run once, kept
with its parameters and covariance, and downloaded.

Results per band: ``A``, ``t0``, ``tau_rise``, ``tau_fall``, ``B`` with the
diagonal errors, ``chi2`` / ``dof``, the peak MJD (``t0 + tau_rise *
ln(tau_fall / tau_rise - 1)`` when defined) and peak magnitude, plus the
extrapolated magnitude ``extrapolate_days`` after the last detection. The
top-level results carry the reference band's numbers (``params["band"]``,
else the band with the most detections). Plots: data with the fitted
(solid) and extrapolated (dashed) curves. Files: ``bazin_fit.json``
(parameters and covariances) and ``model_curves.json``.
"""

from __future__ import annotations

import datetime
import math
from collections import defaultdict
from typing import Dict

import numpy as np

from YSE_App.analysis.base import AnalysisError, AnalysisResult, band_colors, clean_json, figure_png, new_figure, param
from YSE_App.common.filter_display import band_effective_wavelength
from YSE_App.services import bazin
from YSE_App.services.analysis_payload import detections

NAME = "Bazin light-curve fit"
DESCRIPTION = ("Joint Bazin fit of every band (rise/fall time scales tied across neighbouring filters) "
               "with the extrapolated magnitude a few days ahead.")
INPUT_SPEC = ["photometry"]
OUTPUT_SPEC = ["results", "plots", "files"]
SUMMARY_KEYS = ["peak_mjd", "peak_mag", "tau_rise", "tau_fall", "mag_extrapolated"]
PARAM_SCHEMA = {
    "band": {"type": "text", "label": "Reference band (blank: most detections)", "default": ""},
    "extrapolate_days": {"type": "number", "label": "Extrapolate (days after last detection)", "default": 7.0},
    "include_flagged": {"type": "boolean", "label": "Include data-quality-flagged points", "default": False},
}


def _today_mjd() -> float:
    return bazin.datetime_to_mjd(datetime.datetime.now(datetime.timezone.utc))


def peak_mjd(fit: bazin.BazinFit):
    _A, t0, tau_fall, tau_rise, _B = fit.params
    ratio = tau_fall / tau_rise - 1.0
    if ratio <= 0:
        return None
    return t0 + tau_rise * math.log(ratio)


def run(payload: Dict, params: Dict) -> AnalysisResult:
    ref_band = str(param(params, "band", "", str) or "").strip()
    extrapolate_days = param(params, "extrapolate_days", 7.0, float)
    include_flagged = param(params, "include_flagged", False, bool)

    rows = detections(payload, drop_flagged=not include_flagged)
    by_band = defaultdict(list)
    for r in rows:
        by_band[r["band"]].append((r["mjd"], r["mag"], r["mag_err"]))
    total = sum(len(v) for v in by_band.values())
    if total < bazin.MIN_DETECTIONS:
        raise AnalysisError("%d usable detection(s); the Bazin fit needs at least %d." % (total, bazin.MIN_DETECTIONS))

    wavelengths = {b: band_effective_wavelength(b) for b in by_band}
    fits = bazin.fit_bazin_joint(dict(by_band), wavelengths)
    if not fits:
        raise AnalysisError("the Bazin fit did not converge on this light curve.")

    if not ref_band or ref_band not in fits:
        ref_band = max(fits, key=lambda b: (fits[b].n_points, -wavelengths.get(b, 0.0)))
    today = _today_mjd()
    last_mjd = max(f.mjd_max for f in fits.values())
    extrap_mjd = last_mjd + extrapolate_days

    per_band = {}
    for band, fit in fits.items():
        A, t0, tau_fall, tau_rise, B = fit.params
        errs = [math.sqrt(v) if math.isfinite(v) and v >= 0 else None for v in (fit.cov[i][i] for i in range(5))]
        pk = peak_mjd(fit)
        per_band[band] = {
            "A": A, "A_err": errs[0], "t0": t0, "t0_err": errs[1],
            "tau_fall": tau_fall, "tau_fall_err": errs[2], "tau_rise": tau_rise, "tau_rise_err": errs[3],
            "B": B, "B_err": errs[4],
            "chi2": fit.chi2, "dof": fit.dof, "reduced_chi2": fit.reduced_chi2, "n_points": fit.n_points,
            "method": fit.method, "mjd_min": fit.mjd_min, "mjd_max": fit.mjd_max,
            "peak_mjd": pk, "peak_mag": bazin.bazin_mag_at(fit, pk) if pk is not None else None,
            "mag_today": bazin.bazin_mag_at(fit, today),
            "mag_extrapolated": bazin.bazin_mag_at(fit, extrap_mjd),
        }
    ref = per_band[ref_band]
    results = {
        "reference_band": ref_band,
        "bands": sorted(fits.keys()),
        "method": ref["method"],
        "n_points": total,
        "t0": ref["t0"], "t0_err": ref["t0_err"],
        "tau_rise": ref["tau_rise"], "tau_rise_err": ref["tau_rise_err"],
        "tau_fall": ref["tau_fall"], "tau_fall_err": ref["tau_fall_err"],
        "peak_mjd": ref["peak_mjd"], "peak_mag": ref["peak_mag"],
        "phase_today": (today - ref["peak_mjd"]) if ref["peak_mjd"] is not None else None,
        "reduced_chi2": ref["reduced_chi2"],
        "mag_today": ref["mag_today"],
        "extrapolate_mjd": extrap_mjd,
        "mag_extrapolated": ref["mag_extrapolated"],
        "per_band": per_band,
    }
    out = AnalysisResult(results=clean_json(results))
    if ref["peak_mag"] is not None:
        out.summary = "%s peak %.2f at MJD %.1f" % (ref_band, ref["peak_mag"], ref["peak_mjd"])

    curves = {"bands": {}}
    fig = new_figure(7.5, 4.8)
    ax = fig.add_subplot(111)
    colors = band_colors(sorted(by_band.keys()))
    for band, pts in sorted(by_band.items()):
        arr = np.asarray(pts, dtype=float)
        ax.errorbar(arr[:, 0], arr[:, 1], yerr=arr[:, 2], fmt="o", ms=4, color=colors[band],
                    label=band, alpha=0.9 if band in fits else 0.4)
        fit = fits.get(band)
        if fit is None:
            continue
        fitted, extrapolated = bazin.bazin_plot_grid(fit, max(today, extrap_mjd))
        fmag = bazin.flux_to_mag(fit.flux_at(fitted))
        emag = bazin.flux_to_mag(fit.flux_at(extrapolated))
        ax.plot(fitted, fmag, "-", color=colors[band], lw=1.2)
        ax.plot(extrapolated, emag, "--", color=colors[band], lw=1.0)
        curves["bands"][band] = {
            "fitted_mjd": fitted.tolist(), "fitted_mag": [None if not math.isfinite(v) else float(v) for v in fmag],
            "extrapolated_mjd": extrapolated.tolist(),
            "extrapolated_mag": [None if not math.isfinite(v) else float(v) for v in emag],
        }
    ax.axvline(today, color="#888", lw=0.8, ls=":")
    ax.invert_yaxis()
    ax.set_xlabel("MJD")
    ax.set_ylabel("mag")
    name = (payload.get("transient") or {}).get("name") or ""
    ax.set_title("%s Bazin fit (%s), %s: peak %s at MJD %s" % (
        name, ref["method"], ref_band,
        "%.2f" % ref["peak_mag"] if ref["peak_mag"] is not None else "-",
        "%.1f" % ref["peak_mjd"] if ref["peak_mjd"] is not None else "-"), fontsize=10)
    ax.legend(loc="best", fontsize=8)
    ax.grid(alpha=0.25)
    out.add_plot("lightcurve.png", figure_png(fig), title="Bazin fit")
    out.add_json_file("bazin_fit.json", {
        "reference_band": ref_band,
        "bands": {b: {"params": list(f.params), "param_names": ["A", "t0", "tau_fall", "tau_rise", "B"],
                      "cov": [list(r) for r in f.cov], "chi2": f.chi2, "dof": f.dof, "n_points": f.n_points,
                      "method": f.method} for b, f in fits.items()},
    }, kind="inference")
    out.add_json_file("model_curves.json", curves, kind="data")
    return out
