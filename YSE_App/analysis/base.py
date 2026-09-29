"""What an in-process analysis runner returns, and small helpers for plots (#313).

A runner module exposes::

    def run(payload: dict, params: dict) -> AnalysisResult

``payload`` is the document from :mod:`YSE_App.services.analysis_payload`,
``params`` the run's parameters (service defaults merged with what the user
typed). It returns an :class:`AnalysisResult`: a JSON-serialisable ``results``
dict (the parameter table shown on the Analysis tab), ``plots`` (PNG or other
image bytes) and ``files`` (anything else: inference data, tables). Raising
:class:`AnalysisError` fails the run with that message; any other exception
fails it with the exception text.

Runner modules may also declare ``PARAM_SCHEMA`` (the default parameter form),
``INPUT_SPEC`` / ``OUTPUT_SPEC`` and ``DESCRIPTION``; ``register_analysis_service``
copies them onto the service row.
"""

from __future__ import annotations

import dataclasses
import io
import json
import math
from typing import Dict, List, Optional


class AnalysisError(Exception):
    """The analysis cannot run on this input (too few points, no redshift, ...)."""


@dataclasses.dataclass
class Attachment:
    name: str
    data: bytes
    content_type: str = "application/octet-stream"
    kind: str = "other"  # plot | inference | data | other
    meta: Dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class AnalysisResult:
    results: Dict = dataclasses.field(default_factory=dict)
    plots: List[Attachment] = dataclasses.field(default_factory=list)
    files: List[Attachment] = dataclasses.field(default_factory=list)
    summary: str = ""

    def add_plot(self, name: str, data: bytes, content_type: str = "image/png", **meta) -> Attachment:
        att = Attachment(name=name, data=data, content_type=content_type, kind="plot", meta=meta)
        self.plots.append(att)
        return att

    def add_file(self, name: str, data: bytes, content_type: str = "application/octet-stream",
                 kind: str = "data", **meta) -> Attachment:
        att = Attachment(name=name, data=data, content_type=content_type, kind=kind, meta=meta)
        self.files.append(att)
        return att

    def add_json_file(self, name: str, obj, kind: str = "data", **meta) -> Attachment:
        data = json.dumps(clean_json(obj), indent=1, sort_keys=True).encode("utf-8")
        return self.add_file(name, data, "application/json", kind=kind, **meta)


def clean_json(obj):
    """Recursively turn numpy scalars/arrays into JSON types and non-finite floats into None."""
    if isinstance(obj, dict):
        return {str(k): clean_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean_json(v) for v in obj]
    if hasattr(obj, "tolist"):  # numpy array or scalar
        return clean_json(obj.tolist())
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    try:
        f = float(obj)
    except (TypeError, ValueError):
        return str(obj)
    return f if math.isfinite(f) else None


def param(params: Dict, name: str, default=None, cast=None):
    """``params[name]`` cast with ``cast`` (float/int/str/bool), else ``default``."""
    value = params.get(name, default) if isinstance(params, dict) else default
    if value in (None, ""):
        return default
    if cast is bool:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if cast is not None:
        try:
            return cast(value)
        except (TypeError, ValueError):
            return default
    return value


def figure_png(fig, dpi: int = 110) -> bytes:
    """Render a matplotlib figure to PNG bytes and close it."""
    import matplotlib

    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt

    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    finally:
        plt.close(fig)
    return buf.getvalue()


def new_figure(width: float = 7.0, height: float = 4.5):
    import matplotlib

    matplotlib.use("Agg", force=False)
    from matplotlib.figure import Figure

    return Figure(figsize=(width, height))


def band_colors(names: List[str]) -> Dict[str, str]:
    """A stable colour per band name from the light-curve display palette."""
    from YSE_App.common.filter_display import band_display_color

    out = {}
    for i, name in enumerate(names):
        out[name] = band_display_color(name, None, fallback_index=i)
    return out


def format_value(value, err: Optional[float] = None, digits: int = 3) -> str:
    """``61302.93 +/- 0.08``, ``3.04e-08 +/- 2e-10``: fixed notation with the error's precision when sensible."""
    if value is None:
        return "-"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(v):
        return str(value)
    e = None
    if err is not None:
        try:
            e = abs(float(err))
            if not math.isfinite(e):
                e = None
        except (TypeError, ValueError):
            e = None
    magnitude = abs(v) if v else (e or 0.0)
    if e is None and v == int(v) and magnitude < 1e7:
        return "%d" % int(v)
    if magnitude and (magnitude < 1e-3 or magnitude >= 1e7):
        text = "%.*g" % (digits + 1, v)
        return text if e is None else "%s +/- %.2g" % (text, e)
    if e is not None and e > 0:
        decimals = int(max(0, min(6, -math.floor(math.log10(e)) + 1)))
    elif magnitude >= 100:
        decimals = 2
    else:
        decimals = digits + 1 if magnitude < 1 else digits
    text = "%.*f" % (decimals, v)
    return text if e is None else "%s +/- %.*f" % (text, decimals, e)
