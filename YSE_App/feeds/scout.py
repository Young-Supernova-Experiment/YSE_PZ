"""JPL Scout NEO feed, minor-planet screening and NEOfixer ranks (#283).

* **Scout list** (``https://ssd-api.jpl.nasa.gov/scout.api``): the objects
  currently on the MPC's NEO Confirmation Page with Scout's scores. Each one
  becomes a candidate (``broker='scout'``, tag ``NEO``) carrying ``neoScore``,
  ``Vmag``, the sky-plane uncertainty and the rates, refreshed on every poll.
* **Minor-planet screening** (``sb_ident.api``): for a transient, the known
  small bodies inside a small field around its position at its discovery
  epoch. A body within ``FEEDS_MPC_SCREEN_RADIUS_ARCSEC`` (5") writes the
  ``minor_planet`` annotation with ``possible_mpc`` and the separation, whose
  verdict earns a badge on the Summary tab; a clean result is recorded too so
  the sweep does not repeat it.
* **NEOfixer** ranks: optional; ``config.neofixer.url`` names a JSON endpoint
  listing ``{designation, rank}`` rows (NEOfixer's API needs an account), which
  are copied onto matching Scout candidates and, when saved, onto the
  transient as the ``neofixer`` annotation.
"""

from __future__ import annotations

import datetime
import logging
import math
from typing import Dict, Iterable, List, Optional

from django.conf import settings

from YSE_App.brokers import register
from YSE_App.feeds.base import (
    KIND_NEO,
    FeedError,
    FeedMessage,
    FeedProvider,
    http_get_json,
    num,
    parse_coord,
    parse_time_mjd,
)

logger = logging.getLogger(__name__)

SLUG = "scout"
DEFAULT_SCOUT_URL = "https://ssd-api.jpl.nasa.gov/scout.api"
DEFAULT_SBIDENT_URL = "https://ssd-api.jpl.nasa.gov/sb_ident.api"
ORIGIN = "minor_planet"
NEOFIXER_ORIGIN = "neofixer"
VERDICT_MOVING = "moving object"
VERDICT_CLEAN = "clean"
SCOUT_URL_TEMPLATE = "https://cneos.jpl.nasa.gov/scout/#/object/{name}"


def scout_url() -> str:
    return getattr(settings, "FEEDS_SCOUT_API_URL", "") or DEFAULT_SCOUT_URL


def sbident_url() -> str:
    return getattr(settings, "FEEDS_SBIDENT_API_URL", "") or DEFAULT_SBIDENT_URL


def screen_radius_arcsec() -> float:
    return float(getattr(settings, "FEEDS_MPC_SCREEN_RADIUS_ARCSEC", 5.0) or 5.0)


def screen_obs_code() -> str:
    return str(getattr(settings, "FEEDS_MPC_SCREEN_OBS_CODE", "500") or "500")


# --- Scout list -------------------------------------------------------------------

def parse_scout_row(row: Dict) -> Optional[FeedMessage]:
    if not isinstance(row, dict):
        return None
    name = str(row.get("objectName") or row.get("name") or "").strip()
    ra, dec = parse_coord(row.get("ra"), row.get("dec"))
    if not name or ra is None or dec is None:
        return None
    score = num(row.get("neoScore"))
    vmag = num(row.get("Vmag") or row.get("vmag"))
    unc = num(row.get("unc"))  # arcmin
    rate = num(row.get("rate"))  # arcsec/min
    last_run = str(row.get("lastRun") or "").strip()
    n_obs = num(row.get("nObs"))
    arc = num(row.get("arc"))
    rating = str(row.get("rating") or "").strip()
    label = "NEO candidate (score %d)" % int(score) if score is not None else "NEO candidate"
    title = "Scout %s: NEO score %s, V=%s, unc %s arcmin, rate %s arcsec/min, %s obs over %s h" % (
        name, "%.0f" % score if score is not None else "?", "%.1f" % vmag if vmag is not None else "?",
        "%.1f" % unc if unc is not None else "?", "%.2f" % rate if rate is not None else "?",
        int(n_obs) if n_obs is not None else "?", "%.1f" % arc if arc is not None else "?")
    return FeedMessage(
        feed=SLUG, message_id="%s:%s" % (name, last_run), kind=KIND_NEO, object_id=name, ra=ra, dec=dec,
        error_radius_arcsec=unc * 60.0 if unc is not None else None, time_mjd=parse_time_mjd(last_run) if last_run else None,
        reporter="JPL Scout", classification=label, mag=vmag, band="V", score=score / 100.0 if score is not None else None,
        url=SCOUT_URL_TEMPLATE.format(name=name), title=title,
        raw={"neoScore": score, "Vmag": vmag, "unc_arcmin": unc, "rate_arcsec_per_min": rate, "nObs": n_obs, "arc_hours": arc,
             "rating": rating, "lastRun": last_run, "H": num(row.get("H")), "moid": num(row.get("moid")),
             "tisserandScore": num(row.get("tisserandScore")), "geocentricScore": num(row.get("geocentricScore")),
             "phaScore": num(row.get("phaScore")), "ieoScore": num(row.get("ieoScore")), "caDist": num(row.get("caDist")),
             "vInf": num(row.get("vInf")), "elong": num(row.get("elong")), "tag": "NEO"},
    )


@register
class ScoutFeed(FeedProvider):
    slug = SLUG
    name = "JPL Scout"
    description = "NEO Confirmation Page objects scored by JPL Scout, as candidates tagged NEO."
    feed_kind = "scout"
    information_source = "JPL Scout"
    default_auto_save = False
    default_obs_group = "JPL Scout"
    default_instrument = "Unknown"
    config_keys = tuple(FeedProvider.config_keys) + ("min_neo_score", "max_vmag", "max_unc_arcmin", "neofixer")

    def poll(self, source, *, since: Optional[datetime.datetime] = None) -> List[FeedMessage]:
        body = http_get_json(scout_url())
        rows = body.get("data") if isinstance(body, dict) else body
        if not isinstance(rows, list):
            raise FeedError("unexpected Scout payload (no data list)")
        messages = [m for m in (parse_scout_row(r) for r in rows) if m is not None]
        messages = self.apply_cuts(messages)
        ranks = self.neofixer_ranks([m.object_id for m in messages])
        for m in messages:
            if m.object_id in ranks:
                m.raw["neofixer_rank"] = ranks[m.object_id]
        return messages

    def apply_cuts(self, messages: Iterable[FeedMessage]) -> List[FeedMessage]:
        out = list(messages)
        min_score = num(self.option("min_neo_score"))
        if min_score is not None:
            out = [m for m in out if m.score is not None and m.score * 100.0 >= min_score]
        max_vmag = num(self.option("max_vmag"))
        if max_vmag is not None:
            out = [m for m in out if m.mag is not None and m.mag <= max_vmag]
        max_unc = num(self.option("max_unc_arcmin"))
        if max_unc is not None:
            out = [m for m in out if m.error_radius_arcsec is None or m.error_radius_arcsec <= max_unc * 60.0]
        return out

    # -- NEOfixer ----------------------------------------------------------------
    def neofixer_ranks(self, names: Iterable[str]) -> Dict[str, float]:
        cfg = self.option("neofixer") or {}
        url = str(cfg.get("url") or "").strip() if isinstance(cfg, dict) else ""
        if not url:
            return {}
        try:
            body = http_get_json(url, headers={"Authorization": self.credential["neofixer_token"]}
                                 if self.credential.get("neofixer_token") else None)
        except FeedError as exc:
            logger.warning("neofixer ranks unavailable: %s", exc)
            return {}
        rows = body
        if isinstance(body, dict):
            rows = body.get("objects") or body.get("results") or body.get("data") or []
            if isinstance(rows, dict):
                rows = [dict(v, designation=k) if isinstance(v, dict) else {"designation": k, "rank": v} for k, v in rows.items()]
        wanted = set(names)
        ranks = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("designation") or row.get("objectName") or row.get("name") or row.get("desig") or "").strip()
            rank = num(row.get("rank") if row.get("rank") is not None else row.get("priority", row.get("score")))
            if name in wanted and rank is not None:
                ranks[name] = rank
        return ranks

    # -- hooks -----------------------------------------------------------------
    def on_linked(self, source, message: FeedMessage, transient, user) -> None:
        from YSE_App.services import annotations as annotations_svc

        data = {
            "possible_mpc": message.object_id,
            "source": "scout",
            "neo_score": (message.raw or {}).get("neoScore"),
            "vmag": message.mag,
            "unc_arcmin": (message.raw or {}).get("unc_arcmin"),
            "rate_arcsec_per_min": (message.raw or {}).get("rate_arcsec_per_min"),
            "url": message.url,
            "verdict": VERDICT_MOVING,
            "summary": "Position matches NEOCP object %s (Scout NEO score %s)." % (
                message.object_id, (message.raw or {}).get("neoScore")),
        }
        annotations_svc.upsert(transient, ORIGIN, data, user=user, merge=True)
        rank = (message.raw or {}).get("neofixer_rank")
        if rank is not None:
            annotations_svc.upsert(transient, NEOFIXER_ORIGIN, {"rank": rank, "object": message.object_id}, user=user)

    on_created = on_linked


# --- minor-planet screening (sb_ident) --------------------------------------------

def _hms(ra_deg: float) -> str:
    hours = (ra_deg % 360.0) / 15.0
    h = int(hours)
    m = int((hours - h) * 60)
    s = ((hours - h) * 60 - m) * 60
    return "%02d-%02d-%05.2f" % (h, m, s)


def _dms(dec_deg: float) -> str:
    sign = "M" if dec_deg < 0 else ""
    dec = abs(dec_deg)
    d = int(dec)
    m = int((dec - d) * 60)
    s = ((dec - d) * 60 - m) * 60
    return "%s%02d-%02d-%04.1f" % (sign, d, m, s)


def sbident_params(ra: float, dec: float, epoch_mjd: float, *, hwidth_arcmin: float, obs_code: str,
                   vmag_limit: Optional[float] = None) -> Dict:
    params = {
        "sb-kind": "a", "mpc-code": obs_code, "obs-time": "%.6f" % (float(epoch_mjd) + 2400000.5),
        "fov-ra-center": _hms(ra), "fov-dec-center": _dms(dec),
        "fov-ra-hwidth": "%.4f" % hwidth_arcmin, "fov-dec-hwidth": "%.4f" % hwidth_arcmin,
        "two-pass": "true", "suppress-first-pass": "true", "req-elem": "false",
    }
    if vmag_limit is not None:
        params["vmag-lim"] = "%.1f" % vmag_limit
    return params


def _field_index(fields: List[str], *needles: str) -> Optional[int]:
    lowered = [str(f).lower() for f in fields]
    for i, f in enumerate(lowered):
        if all(n in f for n in needles):
            return i
    return None


def parse_sbident(body: Dict) -> List[Dict]:
    """Rows of the sb_ident answer as dicts: name, separation_arcsec, vmag, ra_rate, dec_rate."""
    if not isinstance(body, dict):
        raise FeedError("unexpected sb_ident payload")
    fields = body.get("fields_second") or body.get("fields_first") or []
    data = body.get("data_second_pass") or body.get("data_first_pass") or []
    if not fields or not data:
        return []
    i_name = _field_index(fields, "object") or 0
    i_norm = _field_index(fields, "norm")
    i_dra = _field_index(fields, "dist", "ra")
    i_ddec = _field_index(fields, "dist", "dec")
    i_vmag = _field_index(fields, "magnitude")
    i_rra = _field_index(fields, "ra rate")
    i_rdec = _field_index(fields, "dec rate")
    out = []
    for row in data:
        if not isinstance(row, (list, tuple)) or len(row) <= i_name:
            continue
        sep = num(row[i_norm]) if i_norm is not None and i_norm < len(row) else None
        if sep is None and i_dra is not None and i_ddec is not None and max(i_dra, i_ddec) < len(row):
            dra, ddec = num(row[i_dra]), num(row[i_ddec])
            if dra is not None and ddec is not None:
                sep = math.hypot(dra, ddec)
        out.append({
            "name": str(row[i_name]).strip(),
            "separation_arcsec": abs(sep) if sep is not None else None,
            "vmag": num(row[i_vmag]) if i_vmag is not None and i_vmag < len(row) else None,
            "ra_rate": num(row[i_rra]) if i_rra is not None and i_rra < len(row) else None,
            "dec_rate": num(row[i_rdec]) if i_rdec is not None and i_rdec < len(row) else None,
        })
    out.sort(key=lambda r: (r["separation_arcsec"] is None, r["separation_arcsec"] or 0))
    return out


def discovery_epoch_mjd(transient) -> Optional[float]:
    """Discovery date, else the earliest detection, else the row's creation time."""
    if transient.disc_date is not None:
        return parse_time_mjd(transient.disc_date)
    from YSE_App.models.phot_models import TransientPhotData

    first = (TransientPhotData.objects.filter(photometry__transient=transient, mag__isnull=False)
             .order_by("obs_date").values_list("obs_date", flat=True).first())
    if first is not None:
        return parse_time_mjd(first)
    return parse_time_mjd(transient.created_date) if transient.created_date else None


def screen_transient(transient, *, user=None, radius_arcsec: Optional[float] = None, obs_code: Optional[str] = None,
                     hwidth_arcmin: Optional[float] = None) -> Dict:
    """Query sb_ident at the discovery epoch and write the ``minor_planet`` annotation. Returns the document."""
    from YSE_App.brokers.ingest import audit_user
    from YSE_App.services import annotations as annotations_svc

    radius = float(radius_arcsec if radius_arcsec is not None else screen_radius_arcsec())
    epoch = discovery_epoch_mjd(transient)
    if epoch is None:
        raise FeedError("%s has no discovery epoch to screen at" % transient.name)
    hwidth = float(hwidth_arcmin if hwidth_arcmin is not None else max(1.0, radius / 60.0 * 2.0))
    params = sbident_params(float(transient.ra), float(transient.dec), epoch, hwidth_arcmin=hwidth,
                            obs_code=obs_code or screen_obs_code())
    body = http_get_json(sbident_url(), params=params)
    rows = parse_sbident(body)
    hits = [r for r in rows if r["separation_arcsec"] is not None and r["separation_arcsec"] <= radius]
    document = {
        "epoch_mjd": round(epoch, 5),
        "radius_arcsec": radius,
        "obs_code": params["mpc-code"],
        "n_bodies_in_field": len(rows),
        "source": "sbident",
    }
    if hits:
        best = hits[0]
        document.update({
            "possible_mpc": best["name"],
            "separation_arcsec": round(best["separation_arcsec"], 2),
            "vmag": best["vmag"],
            "ra_rate_arcsec_per_hr": best["ra_rate"],
            "dec_rate_arcsec_per_hr": best["dec_rate"],
            "verdict": VERDICT_MOVING,
            "summary": "Known minor planet %s was %.1f arcsec from this position at the discovery epoch%s." % (
                best["name"], best["separation_arcsec"], " (V=%.1f)" % best["vmag"] if best["vmag"] is not None else ""),
        })
        if len(hits) > 1:
            document["other_matches"] = ", ".join(h["name"] for h in hits[1:4])
    else:
        nearest = rows[0] if rows and rows[0]["separation_arcsec"] is not None else None
        document.update({
            "possible_mpc": None,
            "verdict": VERDICT_CLEAN,
            "summary": "No known minor planet within %.0f arcsec at the discovery epoch%s." % (
                radius, " (nearest: %s at %.0f arcsec)" % (nearest["name"], nearest["separation_arcsec"]) if nearest else ""),
        })
    annotations_svc.upsert(transient, ORIGIN, document, user=audit_user(user))
    return document


__all__ = ["NEOFIXER_ORIGIN", "ORIGIN", "VERDICT_CLEAN", "VERDICT_MOVING", "ScoutFeed", "discovery_epoch_mjd",
           "parse_sbident", "parse_scout_row", "sbident_params", "screen_transient"]
