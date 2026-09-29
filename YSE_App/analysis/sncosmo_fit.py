"""SALT3 / sncosmo light-curve fit as an in-process analysis service (#315).

The same fit the Summary tab's synchronous ``salt2plot`` view performs
(``view_utils.lightcurveplot_detail(salt2=True)``), run once on the job queue
with its parameters, errors, covariance and plots stored on the run:

* redshift fixed to the transient's (or the host's) when known; otherwise
  fitted within ``[0.005, z_max]`` when ``fit_redshift`` is on, else the run
  fails with a clear message;
* Milky Way extinction from ``transient.mw_ebv`` through a CCM89 dust effect
  when ``mw_extinction`` is on and the value is known;
* the data window is ``[peak - window_before, peak + window_after]`` days
  around the brightest point with S/N above ``minsnr`` (the Summary tab's
  ``-20 / +40``);
* only bands ``common.bandpassdict`` maps to an sncosmo bandpass are used.

Results: every fitted parameter with its error, ``chi2`` / ``ndof``,
``t0`` (peak MJD), ``phase_today``, the bands used and, for SALT models,
``mB`` (``10.635 - 2.5 log10 x0``, as the Summary tab shows). Plots: the
light curve with the model per band and a Gaussian corner-style plot of the
fitted parameters from the covariance. Files: ``fit_result.json`` (parameters,
errors, covariance, bands) and ``model_curves.json`` (the plotted model
magnitudes per band, so a client can redraw the fit).

``params["source"]`` names any sncosmo source (``salt3`` default, ``salt2``,
or one registered in-process, which the tests use so nothing is downloaded).
"""

from __future__ import annotations

import datetime
import math
from typing import Dict, List

import numpy as np

from YSE_App.analysis.base import AnalysisError, AnalysisResult, band_colors, clean_json, figure_png, new_figure, param
from YSE_App.services import bazin
from YSE_App.services.analysis_payload import detections

NAME = "SALT3 light-curve fit (sncosmo)"
DESCRIPTION = ("Fits an sncosmo model (SALT3 by default) to the visible photometry; "
               "redshift fixed when known, Milky Way extinction from mw_ebv.")
INPUT_SPEC = ["photometry", "redshift"]
OUTPUT_SPEC = ["results", "plots", "files"]
SUMMARY_KEYS = ["t0", "x1", "c", "mB", "z", "reduced_chi2"]
PARAM_SCHEMA = {
    "source": {"type": "choice", "label": "Model", "choices": ["salt3", "salt2"], "default": "salt3",
               "help": "sncosmo source name."},
    "fit_redshift": {"type": "boolean", "label": "Fit redshift when unknown", "default": False},
    "z_max": {"type": "number", "label": "Max redshift (when fitted)", "default": 0.7},
    "minsnr": {"type": "number", "label": "Min S/N for the peak guess", "default": 3.0},
    "window_before": {"type": "number", "label": "Days before peak", "default": 20.0},
    "window_after": {"type": "number", "label": "Days after peak", "default": 40.0},
    "mw_extinction": {"type": "boolean", "label": "Apply Milky Way extinction", "default": True},
}
SALT_SOURCES = ("salt2", "salt3", "salt2-extended", "salt3-nir")
MIN_POINTS = 4
Z_MIN = 0.005


def _today_mjd() -> float:
    return bazin.datetime_to_mjd(datetime.datetime.now(datetime.timezone.utc))


def _data_table(rows: List[Dict]):
    from astropy.table import Table

    mjd = np.array([r["mjd"] for r in rows], dtype=float)
    flux = np.array([r["flux"] for r in rows], dtype=float)
    fluxerr = np.array([r["flux_err"] for r in rows], dtype=float)
    bands = np.array([r["sncosmo_band"] for r in rows], dtype=object)
    zpsys = np.array([r.get("zpsys") or "ab" for r in rows], dtype=object)
    zp = np.full(len(rows), float(rows[0].get("zp") or 27.5))
    return Table([mjd, bands.astype(str), flux, fluxerr, zp, zpsys.astype(str)],
                 names=["mjd", "band", "flux", "fluxerr", "zp", "zpsys"])


def run(payload: Dict, params: Dict) -> AnalysisResult:
    import sncosmo

    source = str(param(params, "source", "salt3", str))
    fit_redshift = param(params, "fit_redshift", False, bool)
    z_max = param(params, "z_max", 0.7, float)
    minsnr = param(params, "minsnr", 3.0, float)
    before = param(params, "window_before", 20.0, float)
    after = param(params, "window_after", 40.0, float)
    mw_extinction = param(params, "mw_extinction", True, bool)

    rows = [r for r in detections(payload) if r.get("sncosmo_band") and r.get("flux") and r.get("flux_err")]
    if len(rows) < MIN_POINTS:
        skipped = sorted({"%s %s" % (r.get("instrument"), r.get("band")) for r in detections(payload) if not r.get("sncosmo_band")})
        hint = (" Bands without an sncosmo bandpass in bandpassdict: %s." % ", ".join(skipped)) if skipped else ""
        raise AnalysisError("%d usable detection(s) with a known bandpass; at least %d are needed.%s"
                            % (len(rows), MIN_POINTS, hint))

    try:
        model = sncosmo.Model(source=source)
    except Exception as exc:  # download failure, unknown source
        raise AnalysisError("could not load sncosmo source %r: %s: %s" % (source, type(exc).__name__, exc))

    mwebv = (payload.get("transient") or {}).get("mw_ebv")
    if mw_extinction and mwebv is not None and math.isfinite(float(mwebv)) and float(mwebv) > 0:
        model = sncosmo.Model(source=source, effects=[sncosmo.CCM89Dust()], effect_names=["mw"], effect_frames=["obs"])
        model.set(mwebv=float(mwebv))

    z = payload.get("redshift")
    z_source = (payload.get("transient") or {}).get("redshift_source") or ""
    vparams = [p for p in model.param_names if p not in ("z", "mwebv", "mwr_v")]
    bounds = {"x1": (-3.0, 3.0), "c": (-0.3, 0.3)}
    if z is not None and math.isfinite(float(z)) and float(z) > 0:
        model.set(z=float(z))
    elif fit_redshift:
        vparams.insert(0, "z")
        bounds["z"] = (Z_MIN, float(z_max))
        z_source = "fitted"
    else:
        raise AnalysisError("no redshift for the transient or its host; enable 'Fit redshift' to fit it.")

    data = _data_table(rows)
    snr = data["flux"] / data["fluxerr"]
    good = snr > minsnr
    if not np.any(good):
        raise AnalysisError("no detection with S/N above %.1f for the peak guess." % minsnr)
    peak_guess = float(data["mjd"][good][np.argmax(data["flux"][good])])
    window = (data["mjd"] > peak_guess - before) & (data["mjd"] < peak_guess + after)
    data = data[window]
    if len(data) < MIN_POINTS:
        raise AnalysisError("%d detection(s) inside the %.0f/%.0f-day fit window around MJD %.1f; at least %d are needed."
                            % (len(data), before, after, peak_guess, MIN_POINTS))
    bounds["t0"] = (peak_guess - 10.0, peak_guess + 10.0)
    bounds = {k: v for k, v in bounds.items() if k in vparams}

    try:
        result, fitted = sncosmo.fit_lc(data, model, vparams, bounds=bounds, minsnr=minsnr)
    except Exception as exc:
        raise AnalysisError("sncosmo.fit_lc failed: %s: %s" % (type(exc).__name__, exc))

    names = list(result.param_names)
    values = {n: float(v) for n, v in zip(names, result.parameters)}
    errors = {n: float(result.errors.get(n)) for n in vparams if result.errors and n in result.errors}
    cov = result.covariance
    cov_list = np.asarray(cov, dtype=float).tolist() if cov is not None else None
    t0 = values.get("t0")
    today = _today_mjd()
    bands_used = sorted(set(str(b) for b in data["band"]))

    results = {
        "model": source,
        "z": values.get("z"),
        "z_source": z_source,
        "t0": t0,
        "phase_today": (today - t0) if t0 is not None else None,
        "chi2": float(result.chisq) if getattr(result, "chisq", None) is not None else None,
        "ndof": int(result.ndof) if getattr(result, "ndof", None) is not None else None,
        "n_points": int(len(data)),
        "bands": bands_used,
        "mwebv": float(mwebv) if mwebv is not None else None,
    }
    if results["chi2"] is not None and results["ndof"]:
        results["reduced_chi2"] = results["chi2"] / results["ndof"]
    for name in vparams:
        results[name] = values.get(name)
        if name in errors:
            results[name + "_err"] = errors[name]
    if source in SALT_SOURCES and values.get("x0"):
        results["mB"] = 10.635 - 2.5 * math.log10(values["x0"])
        if "x0" in errors and values["x0"] > 0:
            results["mB_err"] = 2.5 / math.log(10.0) * errors["x0"] / values["x0"]

    out = AnalysisResult(results=clean_json(results))
    out.summary = "t0 = %.1f, chi2/dof = %s" % (t0 or 0.0, "%.2f" % results["reduced_chi2"] if results.get("reduced_chi2") else "-")

    curves = _model_curves(fitted, data, bands_used, t0)
    out.add_plot("lightcurve.png", _lightcurve_png(data, curves, results, payload), title="Light curve and model")
    if cov_list is not None and len(vparams) >= 2:
        try:
            out.add_plot("corner.png", _corner_png(vparams, values, np.asarray(cov_list)), title="Parameter covariance")
        except Exception:  # a singular covariance must not fail the run
            pass
    out.add_json_file("fit_result.json", {
        "model": source, "param_names": names, "parameters": values, "vparam_names": vparams,
        "errors": errors, "covariance": cov_list, "bands": bands_used, "n_points": int(len(data)),
        "chi2": results["chi2"], "ndof": results["ndof"],
    }, kind="inference")
    out.add_json_file("model_curves.json", curves, kind="data")
    return out


def _model_curves(fitted, data, bands, t0):
    grid = np.arange((t0 or float(data["mjd"].min())) - 20.0, (t0 or float(data["mjd"].max())) + 50.0, 0.5)
    curves = {"mjd": grid.tolist(), "bands": {}}
    for band in bands:
        zpsys = str(data["zpsys"][np.asarray(data["band"]).astype(str) == band][0])
        try:
            flux = fitted.bandflux(band, grid, zp=27.5, zpsys=zpsys)
        except Exception:
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            mag = np.where(flux > 0, -2.5 * np.log10(np.where(flux > 0, flux, 1.0)) + 27.5, np.nan)
        curves["bands"][band] = [None if not math.isfinite(m) else float(m) for m in mag]
    return curves


def _lightcurve_png(data, curves, results, payload) -> bytes:
    fig = new_figure(7.5, 4.8)
    ax = fig.add_subplot(111)
    bands = list(curves["bands"].keys()) or sorted(set(str(b) for b in data["band"]))
    colors = band_colors(bands)
    band_col = np.asarray(data["band"]).astype(str)
    grid = np.asarray(curves["mjd"], dtype=float)
    for band in bands:
        sel = band_col == band
        flux = np.asarray(data["flux"][sel], dtype=float)
        ferr = np.asarray(data["fluxerr"][sel], dtype=float)
        ok = flux > 0
        mag = -2.5 * np.log10(flux[ok]) + 27.5
        magerr = 2.5 / math.log(10.0) * ferr[ok] / flux[ok]
        ax.errorbar(np.asarray(data["mjd"][sel], dtype=float)[ok], mag, yerr=magerr, fmt="o", ms=4,
                    color=colors[band], label=band)
        model = curves["bands"].get(band)
        if model:
            y = np.array([np.nan if v is None else v for v in model], dtype=float)
            ax.plot(grid, y, "-", color=colors[band], lw=1.2)
    ax.invert_yaxis()
    ax.set_xlabel("MJD")
    ax.set_ylabel("mag (zp 27.5)")
    name = (payload.get("transient") or {}).get("name") or ""
    parts = ["%s %s" % (name, results.get("model", ""))]
    if results.get("z") is not None:
        parts.append("z = %.3f (%s)" % (results["z"], results.get("z_source") or ""))
    if results.get("reduced_chi2") is not None:
        parts.append("chi2/dof = %.2f" % results["reduced_chi2"])
    ax.set_title("  ".join(parts), fontsize=10)
    ax.legend(loc="best", fontsize=8)
    ax.grid(alpha=0.25)
    return figure_png(fig)


def _corner_png(names: List[str], values: Dict, cov: np.ndarray) -> bytes:
    """Gaussian corner-style plot: 1-sigma / 2-sigma ellipses from the covariance."""
    from matplotlib.patches import Ellipse

    n = len(names)
    fig = new_figure(1.9 * n + 0.6, 1.9 * n + 0.6)
    axes = fig.subplots(n, n, squeeze=False)
    sig = np.sqrt(np.clip(np.diag(cov), 0, None))
    for i in range(n):
        for j in range(n):
            ax = axes[i][j]
            if j > i:
                ax.set_visible(False)
                continue
            xi, xj = values[names[j]], values[names[i]]
            if i == j:
                s = sig[i] if sig[i] > 0 else 1e-6
                xs = np.linspace(xi - 3.5 * s, xi + 3.5 * s, 200)
                ax.plot(xs, np.exp(-0.5 * ((xs - xi) / s) ** 2), color="#4c72b0")
                ax.set_yticks([])
                ax.set_title("%s = %.3g +/- %.2g" % (names[i], xi, s), fontsize=7)
            else:
                sub = cov[np.ix_([j, i], [j, i])]
                vals, vecs = np.linalg.eigh(sub)
                vals = np.clip(vals, 1e-30, None)
                angle = math.degrees(math.atan2(vecs[1, 0], vecs[0, 0]))
                for k, alpha in ((1, 0.55), (2, 0.25)):
                    ax.add_patch(Ellipse((xi, xj), 2 * k * math.sqrt(vals[0]), 2 * k * math.sqrt(vals[1]),
                                         angle=angle, color="#4c72b0", alpha=alpha))
                ax.plot([xi], [xj], "k+", ms=5)
                sx = sig[j] if sig[j] > 0 else 1e-6
                sy = sig[i] if sig[i] > 0 else 1e-6
                ax.set_xlim(xi - 3.5 * sx, xi + 3.5 * sx)
                ax.set_ylim(xj - 3.5 * sy, xj + 3.5 * sy)
            ax.tick_params(labelsize=6)
            if i == n - 1:
                ax.set_xlabel(names[j], fontsize=8)
            else:
                ax.set_xticklabels([])
            if j == 0 and i != j:
                ax.set_ylabel(names[i], fontsize=8)
            elif i != j:
                ax.set_yticklabels([])
    return figure_png(fig, dpi=100)
