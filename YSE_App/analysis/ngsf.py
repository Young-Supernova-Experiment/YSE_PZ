"""NGSF (Next Generation SuperFit) spectral classification as an analysis service (#315).

NGSF (Goldwasser et al. 2022, https://github.com/oyaron/NGSF) matches an
observed spectrum against a bank of supernova templates combined with host
galaxy templates, over a grid of redshift, extinction and phase, and ranks
the combinations by chi2/dof. It is a separate program with a large template
bank, so it is **not vendored** here: this runner shells out to an installed
copy through a small, mockable boundary (:func:`run_ngsf`) and reads the CSV
and PNG files NGSF writes. ``docs/ngsf.md`` has the install steps.

Configuration (``settings.ini`` ``[site_settings]``, see ``YSE_PZ/settings.py``):

* ``NGSF_COMMAND`` - the program (``ngsf`` on PATH, a path, or a full command
  such as ``python /opt/NGSF/run.py``); ``{params}`` marks where the
  ``parameters.json`` path goes, otherwise it is appended;
* ``NGSF_HOME`` - the directory NGSF runs in (its template bank and a base
  ``parameters.json`` whose settings are kept unless a run overrides them);
* ``NGSF_SUBPROCESS_TIMEOUT`` - seconds one subprocess may take.

:func:`availability` tells the Summary tab whether NGSF is installed; the
runner raises :class:`AnalysisError` with the same reason so a run on a stack
without NGSF fails with a clear message instead of a traceback.

What a run does:

1. picks the spectrum (``params["spectrum_id"]``, else the most recent one
   with data) from the payload and writes it as a two- or three-column ASCII
   file in a scratch directory;
2. writes ``parameters.json``: the base file from ``NGSF_HOME`` (if any)
   updated with the object path, the redshift window (``z +/- z_window`` when
   the transient or its host has one, else ``0 .. z_max``), the number of
   results, the resolution and the plot settings;
3. runs NGSF with a timeout, then parses the results CSV (columns
   ``SPECTRUM, GALAXY, CONST_SN, CONST_GAL, Z, A_v, Phase, Band, Frac(SN),
   Frac(gal), CHI2/dof, CHI2/dof2``; names are matched loosely) and collects
   the PNG comparison plots.

Results: ``best_type`` / ``best_template`` / ``best_phase`` / ``best_z`` /
``best_av`` / ``best_chi2_dof`` / ``best_galaxy`` / ``best_frac_sn`` for the
lowest-chi2 match, ``type_votes`` (how many of the top matches share each
type), ``matches`` (the ranked table) and the spectrum used. Files: the CSV,
``parameters.json`` and the NGSF log; plots: NGSF's PNGs.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections import Counter
from typing import Dict, List, Optional

from django.conf import settings

from YSE_App.analysis.base import AnalysisError, AnalysisResult, clean_json, param

NAME = "NGSF spectral classification"
DESCRIPTION = ("Next Generation SuperFit: matches one spectrum against supernova + host templates "
               "over redshift, extinction and phase and ranks the matches by chi2/dof.")
INPUT_SPEC = ["spectra", "redshift"]
OUTPUT_SPEC = ["results", "plots", "files"]
SUMMARY_KEYS = ["best_type", "best_template", "best_phase", "best_chi2_dof"]
PARAM_SCHEMA = {
    "spectrum_id": {"type": "text", "label": "Spectrum id (blank: most recent)", "default": ""},
    "z_window": {"type": "number", "label": "Redshift half-window around the known z", "default": 0.02, "min": 0},
    "z_max": {"type": "number", "label": "Max redshift when z is unknown", "default": 0.3, "min": 0},
    "n": {"type": "integer", "label": "Matches to keep", "default": 5},
    "resolution": {"type": "number", "label": "Resolution (Angstrom)", "default": 10.0},
    "mask_galaxy_lines": {"type": "boolean", "label": "Mask host galaxy lines", "default": False},
    "mask_telluric": {"type": "boolean", "label": "Mask telluric bands", "default": False},
    "how_many_plots": {"type": "integer", "label": "Comparison plots to keep", "default": 3},
}
DEFAULT_COMMAND = "ngsf"
DEFAULT_TIMEOUT = 1500
MIN_SPECTRUM_POINTS = 50
LOG_TAIL = 4000

# NGSF result columns (case/space-insensitive match; see _column).
COLUMN_ALIASES = {
    "spectrum": ("spectrum", "sn", "template"),
    "galaxy": ("galaxy", "gal"),
    "const_sn": ("const_sn",),
    "const_gal": ("const_gal",),
    "z": ("z", "redshift"),
    "av": ("a_v", "av", "alam"),
    "phase": ("phase",),
    "band": ("band",),
    "frac_sn": ("frac(sn)", "frac_sn", "fracsn"),
    "frac_gal": ("frac(gal)", "frac_gal", "fracgal"),
    "chi2_dof": ("chi2/dof", "chi2_dof", "chi2dof", "chi2"),
    "chi2_dof2": ("chi2/dof2", "chi2_dof2", "chi2dof2"),
}


# --- configuration ------------------------------------------------------------------

def command_setting() -> str:
    return str(getattr(settings, "NGSF_COMMAND", DEFAULT_COMMAND) or "").strip()


def home_setting() -> str:
    return str(getattr(settings, "NGSF_HOME", "") or "").strip()


def timeout_setting() -> int:
    try:
        return int(getattr(settings, "NGSF_SUBPROCESS_TIMEOUT", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def resolve_command(command: Optional[str] = None) -> List[str]:
    """``NGSF_COMMAND`` split into argv with its program resolved on PATH; ``[]`` when not found."""
    text = command_setting() if command is None else str(command or "").strip()
    if not text:
        return []
    try:
        argv = shlex.split(text)
    except ValueError:
        return []
    if not argv:
        return []
    program = argv[0]
    if os.path.sep in program or (os.path.altsep and os.path.altsep in program):
        path = os.path.abspath(os.path.expanduser(program))
        if not os.path.isfile(path):
            return []
        program = path
    else:
        found = shutil.which(program)
        if not found:
            return []
        program = found
    return [program] + argv[1:]


def availability() -> Dict:
    """``{"available", "command", "home", "reason"}`` for the Summary tab and the runner."""
    text = command_setting()
    home = home_setting()
    if not text:
        return {"available": False, "command": "", "home": home,
                "reason": "NGSF_COMMAND is empty; NGSF is not installed on this server."}
    argv = resolve_command(text)
    if not argv:
        return {"available": False, "command": text, "home": home,
                "reason": "NGSF command %r was not found on this server (NGSF_COMMAND)." % text}
    if home and not os.path.isdir(home):
        return {"available": False, "command": " ".join(argv), "home": home,
                "reason": "NGSF_HOME %r is not a directory on this server." % home}
    return {"available": True, "command": " ".join(argv), "home": home, "reason": ""}


def is_available() -> bool:
    return bool(availability().get("available"))


# --- the subprocess boundary ---------------------------------------------------------------

def build_argv(params_path: str, command: Optional[str] = None) -> List[str]:
    argv = resolve_command(command)
    if not argv:
        raise AnalysisError(availability().get("reason") or "NGSF is not installed on this server.")
    if any("{params}" in a for a in argv):
        return [a.replace("{params}", params_path) for a in argv]
    return argv + [params_path]


def run_ngsf(params_path: str, cwd: str, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    """Run NGSF on ``params_path``; the only place a process is spawned (tests replace it)."""
    argv = build_argv(params_path)
    env = dict(os.environ)
    env.setdefault("MPLBACKEND", "Agg")
    try:
        return subprocess.run(  # noqa: S603 - argv comes from settings, not the user
            argv, cwd=cwd, env=env, capture_output=True, text=True,
            timeout=timeout_setting() if timeout is None else timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AnalysisError("NGSF did not finish within %d s." % int(exc.timeout))
    except OSError as exc:
        raise AnalysisError("could not start NGSF (%s): %s" % (" ".join(argv[:1]), exc))


# --- input ----------------------------------------------------------------------------------

def choose_spectrum(payload: Dict, spectrum_id: str = "") -> Dict:
    spectra = [s for s in (payload.get("spectra") or []) if isinstance(s, dict)]
    if not spectra:
        raise AnalysisError("no spectrum is available for this transient (or none you may see).")
    if spectrum_id:
        for s in spectra:
            if str(s.get("id")) == str(spectrum_id):
                if not s.get("wavelength"):
                    raise AnalysisError("spectrum %s has no data points." % spectrum_id)
                return s
        raise AnalysisError("spectrum %s is not one of this transient's visible spectra." % spectrum_id)
    with_data = [s for s in spectra if s.get("wavelength") and s.get("flux")]
    if not with_data:
        raise AnalysisError("none of the %d spectra has data points." % len(spectra))
    return max(with_data, key=lambda s: (s.get("mjd") or 0.0, s.get("id") or 0))


def spectrum_text(spectrum: Dict) -> str:
    """Two- or three-column ASCII (wavelength, flux[, flux_err]) with finite rows only."""
    waves = spectrum.get("wavelength") or []
    fluxes = spectrum.get("flux") or []
    errs = spectrum.get("flux_err") or None
    lines = []
    for i, (w, f) in enumerate(zip(waves, fluxes)):
        if w is None or f is None:
            continue
        try:
            w, f = float(w), float(f)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(w) and math.isfinite(f)):
            continue
        e = None
        if errs is not None and i < len(errs) and errs[i] is not None:
            try:
                e = float(errs[i])
                if not math.isfinite(e):
                    e = None
            except (TypeError, ValueError):
                e = None
        lines.append("%.4f %.6e" % (w, f) + ("" if e is None else " %.6e" % e))
    if len(lines) < MIN_SPECTRUM_POINTS:
        raise AnalysisError("spectrum %s has %d usable points; NGSF needs at least %d."
                            % (spectrum.get("id"), len(lines), MIN_SPECTRUM_POINTS))
    return "\n".join(lines) + "\n"


def base_parameters(home: str) -> Dict:
    """The deployment's ``parameters.json`` under ``NGSF_HOME`` (template lists, wavelength range ...)."""
    if not home:
        return {}
    path = os.path.join(home, "parameters.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def parameters_for(payload: Dict, params: Dict, object_path: str, results_dir: str, home: str) -> Dict:
    doc = dict(base_parameters(home))
    z = payload.get("redshift")
    z_window = param(params, "z_window", 0.02, float)
    z_max = param(params, "z_max", 0.3, float)
    n = param(params, "n", 5, int)
    plots = param(params, "how_many_plots", 3, int)
    if z is not None and math.isfinite(float(z)) and float(z) >= 0:
        z = float(z)
        doc.update({"z_start": round(max(0.0, z - z_window), 5), "z_end": round(z + z_window, 5),
                    "use_exact_z": 1 if z_window == 0 else 0})
        doc.setdefault("z_int", 0.005)
        if z_window == 0:
            doc["z_exact"] = z
    else:
        doc.update({"z_start": 0.0, "z_end": float(z_max), "use_exact_z": 0})
        doc.setdefault("z_int", 0.01)
    doc.update({
        "object_to_fit": object_path,
        "saving_results_path": results_dir,
        "show_plot": 0,
        "how_many_plots": max(0, plots),
        "n": max(1, n),
        "resolution": param(params, "resolution", 10.0, float),
        "mask_galaxy_lines": 1 if param(params, "mask_galaxy_lines", False, bool) else 0,
        "mask_telluric": 1 if param(params, "mask_telluric", False, bool) else 0,
    })
    return doc


# --- output ----------------------------------------------------------------------------------

def _norm(name: str) -> str:
    return re.sub(r"\s+", "", str(name or "")).lower()


def _column(row: Dict, key: str):
    aliases = COLUMN_ALIASES[key]
    for k, v in row.items():
        if _norm(k) in aliases:
            return v
    return None


def _float(value) -> Optional[float]:
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def split_template(spectrum_name: str):
    """``("Ia", "sn2011fe")`` from ``.../sne/Ia/sn2011fe/sn2011fe-visit3.dat`` (or ``Ia/sn2011fe``)."""
    text = str(spectrum_name or "").strip().replace("\\", "/")
    parts = [p for p in text.split("/") if p and p != "."]
    if "sne" in parts:
        parts = parts[parts.index("sne") + 1:]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return "", os.path.splitext(parts[0])[0]
    return parts[0], parts[1]


def parse_results_csv(text: str) -> List[Dict]:
    """NGSF's results CSV as ranked dicts (lowest chi2/dof first)."""
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for raw in reader:
        if not raw or not isinstance(raw, dict):
            continue
        raw = {k: v for k, v in raw.items() if k is not None}
        name = _column(raw, "spectrum")
        if name in (None, ""):
            continue
        sn_type, template = split_template(name)
        rows.append({
            "spectrum": str(name).strip(),
            "type": sn_type,
            "template": template,
            "galaxy": (str(_column(raw, "galaxy") or "").strip() or None),
            "z": _float(_column(raw, "z")),
            "av": _float(_column(raw, "av")),
            "phase": _float(_column(raw, "phase")),
            "band": (str(_column(raw, "band") or "").strip() or None),
            "frac_sn": _float(_column(raw, "frac_sn")),
            "frac_gal": _float(_column(raw, "frac_gal")),
            "chi2_dof": _float(_column(raw, "chi2_dof")),
            "chi2_dof2": _float(_column(raw, "chi2_dof2")),
            "const_sn": _float(_column(raw, "const_sn")),
            "const_gal": _float(_column(raw, "const_gal")),
        })
    rows.sort(key=lambda r: (r["chi2_dof"] is None, r["chi2_dof"] if r["chi2_dof"] is not None else 0.0))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def find_results(results_dir: str):
    """``(csv path or None, [png paths])`` NGSF wrote under ``results_dir``."""
    csvs, pngs = [], []
    for root, _dirs, files in os.walk(results_dir):
        for name in files:
            lower = name.lower()
            path = os.path.join(root, name)
            if lower.endswith(".csv"):
                csvs.append(path)
            elif lower.endswith(".png"):
                pngs.append(path)
    csvs.sort(key=lambda p: (-os.path.getmtime(p), p))
    pngs.sort()
    return (csvs[0] if csvs else None), pngs


def summarise(matches: List[Dict], n: int) -> Dict:
    top = matches[:max(1, n)]
    best = top[0] if top else {}
    votes = Counter(m["type"] or "?" for m in top)
    return {
        "best_type": best.get("type") or None,
        "best_template": best.get("template") or None,
        "best_phase": best.get("phase"),
        "best_z": best.get("z"),
        "best_av": best.get("av"),
        "best_chi2_dof": best.get("chi2_dof"),
        "best_galaxy": best.get("galaxy"),
        "best_frac_sn": best.get("frac_sn"),
        "n_matches": len(matches),
        "type_votes": dict(votes.most_common()),
        "matches": top,
    }


# --- the runner ------------------------------------------------------------------------------

def run(payload: Dict, params: Dict) -> AnalysisResult:
    avail = availability()
    if not avail["available"]:
        raise AnalysisError(avail["reason"] + " See docs/ngsf.md.")
    spectrum = choose_spectrum(payload, str(param(params, "spectrum_id", "", str) or "").strip())
    text = spectrum_text(spectrum)
    name = (payload.get("transient") or {}).get("name") or "transient"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", "%s_spec%s" % (name, spectrum.get("id")))
    n = param(params, "n", 5, int)
    home = avail["home"]

    workdir = tempfile.mkdtemp(prefix="ngsf-")
    try:
        object_path = os.path.join(workdir, safe + ".dat")
        results_dir = os.path.join(workdir, "results")
        os.makedirs(results_dir, exist_ok=True)
        with open(object_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        parameters = parameters_for(payload, params, object_path, results_dir, home)
        params_path = os.path.join(workdir, "parameters.json")
        with open(params_path, "w", encoding="utf-8") as fh:
            json.dump(parameters, fh, indent=1, sort_keys=True)

        proc = run_ngsf(params_path, cwd=home or workdir)
        log_text = "$ %s\n\n[stdout]\n%s\n\n[stderr]\n%s\n" % (
            " ".join(build_argv(params_path)), (proc.stdout or "")[-LOG_TAIL:], (proc.stderr or "")[-LOG_TAIL:])
        if proc.returncode != 0:
            tail = ((proc.stderr or "").strip() or (proc.stdout or "").strip())[-600:]
            raise AnalysisError("NGSF exited with status %d%s" % (proc.returncode, (": " + tail) if tail else ""))
        csv_path, pngs = find_results(results_dir)
        if csv_path is None:
            raise AnalysisError("NGSF finished but wrote no results CSV under its results directory.")
        with open(csv_path, encoding="utf-8", errors="replace") as fh:
            csv_text = fh.read()
        matches = parse_results_csv(csv_text)
        if not matches:
            raise AnalysisError("NGSF found no template match for spectrum %s." % spectrum.get("id"))

        results = summarise(matches, n)
        results.update({
            "spectrum_id": spectrum.get("id"),
            "spectrum_mjd": spectrum.get("mjd"),
            "spectrum_instrument": spectrum.get("instrument"),
            "z_start": parameters.get("z_start"),
            "z_end": parameters.get("z_end"),
            "z_source": (payload.get("transient") or {}).get("redshift_source") or "",
        })
        out = AnalysisResult(results=clean_json(results))
        best = matches[0]
        bits = [best.get("type") or "?"]
        if best.get("template"):
            bits.append(best["template"])
        if best.get("phase") is not None:
            bits.append("phase %+.0f d" % best["phase"])
        if best.get("chi2_dof") is not None:
            bits.append("chi2/dof %.2f" % best["chi2_dof"])
        out.summary = "NGSF: " + ", ".join(bits)
        for i, png in enumerate(pngs[:max(0, param(params, "how_many_plots", 3, int))], 1):
            try:
                with open(png, "rb") as fh:
                    out.add_plot("ngsf_match_%d.png" % i, fh.read(), title="NGSF match %d" % i)
            except OSError:
                continue
        out.add_file("ngsf_results.csv", csv_text.encode("utf-8"), "text/csv", kind="data")
        out.add_json_file("parameters.json", parameters, kind="data")
        out.add_file("ngsf_log.txt", log_text.encode("utf-8", errors="replace"), "text/plain", kind="other")
        return out
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
