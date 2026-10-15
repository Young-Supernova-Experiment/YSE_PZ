"""AI summaries of transients (#294: #295 history + editing, #296 summariser, #297 search).

* :func:`gather_context` collects what a summary is written from: identity and
  coordinates, discovery, status, classes, redshift, host, PhotStat numbers,
  spectra, follow-ups, annotation verdicts and the last comments. Only
  collaboration-wide content goes in (public comments, ungrouped spectra /
  follow-ups / annotations); with a ``viewer`` the same rows are additionally
  filtered by that user's visibility, so a run never includes a comment the
  requesting user cannot see, and a batch run (``viewer=None``) uses the
  collaboration-wide subset only.
* :func:`build_prompt` turns the context into a fixed, length-capped prompt;
  :func:`render_template_summary` writes the deterministic no-LLM summary the
  ``template`` provider uses (the default: no key, no network).
* :func:`set_summary` is the one writer: it stores the text on the transient,
  appends a :class:`TransientSummaryHistory` version with its provenance and
  refreshes the embedding.
* :func:`request_summary` queues an ``ExternalServiceRun`` of the ``ai_summary``
  service; :func:`run_summary` (registered runner) executes it on the job queue
  and :func:`refresh_stale` is the nightly batch (``summaries.refresh_stale``).
* :func:`search_summaries` ranks transients for a natural-language query by
  cosine similarity over the stored embeddings (numpy over a cached matrix),
  filtered by the user's transient access, with a ``summary__icontains``
  fallback when no embeddings exist.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import Dict, Iterable, List, Optional, Tuple

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count, Max
from django.utils import timezone

from YSE_App.models.external_service_models import ExternalService, ExternalServiceRun
from YSE_App.models.summary_models import (
    EDIT_PERMISSION,
    SOURCE_AI,
    SOURCE_HUMAN,
    TransientSummaryEmbedding,
    TransientSummaryHistory,
    TransientSummaryPreference,
    first_line,
    pack_vector,
)
from YSE_App.models.transient_models import Transient
from YSE_App.services import external_services as runs
from YSE_App.services import llm
from YSE_App.services.job_queue import enqueue, job
from YSE_App.services.visibility import filter_transients_by_user_access, user_can_view_transient

log = logging.getLogger(__name__)

SERVICE_SLUG = "ai_summary"
SERVICE_NAME = "AI summary"
SERVICE_DESCRIPTION = ("Writes a short summary of a transient (what it is, redshift, classification, what has been "
                       "done, open questions) from its comments, classes, spectra, follow-ups and photometry.")
PROMPT_VERSION = "1"
BATCH_JOB_KIND = "summaries.refresh_stale"
NOTIFICATION_KIND = "summary"
TEXT_MAX_CHARS = 4000


class SummaryError(Exception):
    """A summary could not be requested or written."""


def _setting(name, default):
    value = getattr(settings, name, default)
    return default if value is None else value


# --- service row ----------------------------------------------------------------------

def service_row(include_disabled: bool = False) -> Optional[ExternalService]:
    qs = ExternalService.objects.filter(slug=SERVICE_SLUG)
    if not include_disabled:
        qs = qs.filter(enabled=True)
    return qs.prefetch_related("groups").first()


def ensure_service(user: User, enabled: bool = True, params: Optional[Dict] = None) -> ExternalService:
    """Create (or refresh) the ``ai_summary`` ``ExternalService`` row."""
    service, created = ExternalService.objects.get_or_create(
        slug=SERVICE_SLUG,
        defaults={"name": SERVICE_NAME, "kind": ExternalService.KIND_SUMMARY, "description": SERVICE_DESCRIPTION,
                  "enabled": enabled, "default_params": dict(params or {}),
                  "created_by": user, "modified_by": user},
    )
    changed = []
    if service.kind != ExternalService.KIND_SUMMARY:
        service.kind = ExternalService.KIND_SUMMARY
        changed.append("kind")
    if not service.description:
        service.description = SERVICE_DESCRIPTION
        changed.append("description")
    if params:
        merged = dict(service.default_params or {})
        merged.update(params)
        service.default_params = merged
        changed.append("default_params")
    if changed:
        service.modified_by = user
        service.save(update_fields=changed + ["modified_by", "modified_date"])
    return service


# --- permissions ----------------------------------------------------------------------

def can_view(user, transient: Transient) -> bool:
    if user is None or not user.is_authenticated:
        return False
    return user_can_view_transient(user, transient.pk)


def can_edit(user, transient: Transient) -> bool:
    """Human edits: any user who can see the transient, unless SUMMARY_EDIT_STAFF_ONLY (then staff / permission)."""
    if not can_view(user, transient):
        return False
    if user.is_staff or user.is_superuser or user.has_perm("YSE_App." + EDIT_PERMISSION):
        return True
    return not bool(_setting("SUMMARY_EDIT_STAFF_ONLY", False))


def group_allows(user, service: Optional[ExternalService]) -> bool:
    """The group side of the opt-in: staff, or everyone when the service names no group, else members."""
    return bool(service is not None and service.enabled and service.visible_to(user))


def opted_in(user) -> bool:
    return TransientSummaryPreference.enabled_for(user)


def set_opt_in(user, enabled: bool) -> TransientSummaryPreference:
    pref, _ = TransientSummaryPreference.objects.get_or_create(user=user)
    if pref.ai_enabled != bool(enabled):
        pref.ai_enabled = bool(enabled)
        pref.save(update_fields=["ai_enabled", "modified_date"])
    return pref


def can_generate(user, transient: Transient, service: Optional[ExternalService] = None) -> bool:
    """Generate / regenerate: view access + the service allows the user's groups + the per-user opt-in."""
    if service is None:
        service = service_row()
    return can_view(user, transient) and group_allows(user, service) and opted_in(user)


# --- context ----------------------------------------------------------------------

def _fmt(value, digits=3):
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _date(value):
    return value.strftime("%Y-%m-%d") if value else None


def _comments(transient: Transient, viewer, limit: int):
    from YSE_App.models import Log
    from YSE_App.services.visibility import filter_transient_comments_for_user

    # Collaboration-wide only: public comments with no group audience.
    qs = (Log.objects.filter(transient=transient, transient_followup__isnull=True, is_public=True)
          .annotate(_n_groups=Count("groups", distinct=True)).filter(_n_groups=0)
          .select_related("created_by").order_by("-modified_date"))
    if viewer is not None:
        qs = filter_transient_comments_for_user(qs, viewer)
    out = []
    for log_row in qs[:limit]:
        out.append({
            "date": _date(log_row.modified_date),
            "by": log_row.created_by.username if log_row.created_by_id else "",
            "text": " ".join((log_row.comment or "").split()),
        })
    return out


def _spectra(transient: Transient, viewer):
    from YSE_App.data import SpectraService
    from YSE_App.models import TransientSpectrum

    if viewer is not None:
        qs = SpectraService.GetAuthorizedTransientSpectrum_ByUser_ByTransient(viewer, transient.pk)
    else:
        qs = TransientSpectrum.objects.filter(transient=transient)
    qs = qs.annotate(_n_groups=Count("groups", distinct=True)).filter(_n_groups=0)
    out = []
    for spec in qs.select_related("instrument", "instrument__telescope").order_by("obs_date"):
        instrument = spec.instrument
        telescope = getattr(instrument, "telescope", None)
        out.append({
            "date": _date(spec.obs_date),
            "instrument": getattr(instrument, "name", None),
            "telescope": getattr(telescope, "name", None),
            "redshift": _fmt(spec.redshift, 4),
            "phase": _fmt(getattr(spec, "spec_phase", None), 1),
            "notes": " ".join((spec.spectrum_notes or "").split())[:200] or None,
        })
    return out


def _followups(transient: Transient, viewer):
    from YSE_App.models import TransientFollowup
    from YSE_App.services.visibility import filter_transient_followups_for_user

    qs = (TransientFollowup.objects.filter(transient=transient, is_public=True)
          .annotate(_n_groups=Count("groups", distinct=True)).filter(_n_groups=0))
    if viewer is not None:
        qs = filter_transient_followups_for_user(qs, viewer)
    out = []
    for fu in qs.select_related("status", "too_resource", "classical_resource", "queued_resource").order_by("valid_start"):
        resource = fu.too_resource or fu.classical_resource or fu.queued_resource
        telescope = getattr(resource, "telescope", None)
        out.append({
            "status": getattr(fu.status, "name", None),
            "resource": getattr(telescope, "name", None) or (str(resource) if resource else None),
            "window": "%s to %s" % (_date(fu.valid_start), _date(fu.valid_stop)),
        })
    return out


def _annotations(transient: Transient, viewer):
    from YSE_App.models.annotation_models import LEGACY_ORIGIN, TransientAnnotation

    if viewer is not None:
        from YSE_App.services.annotations import visible_annotations

        rows = [a for a in visible_annotations(transient, viewer) if not a.groups.exists()]
    else:
        rows = list(TransientAnnotation.objects.filter(transient=transient, groups__isnull=True).order_by("origin"))
    out = []
    for annotation in rows:
        if annotation.origin == LEGACY_ORIGIN:
            continue
        entry = {"origin": annotation.origin}
        if annotation.verdict:
            entry["verdict"] = annotation.verdict
        if annotation.summary:
            entry["summary"] = annotation.summary
        out.append(entry)
    return out


def _photstat(transient: Transient):
    from YSE_App.services.photstat import stat_for_transient

    try:
        stat = stat_for_transient(transient.pk, compute_missing=True)
    except Exception:  # noqa: BLE001 - the summary must never fail on stats
        log.exception("photstat lookup failed for %s", transient.name)
        return None
    if stat is None:
        return None
    return {
        "detections": stat.num_det_global,
        "points": stat.num_obs_global,
        "first_detected": _date(stat.first_detected_date),
        "first_mag": _fmt(stat.first_detected_mag, 2),
        "first_band": getattr(stat.first_detected_band, "name", None),
        "last_detected": _date(stat.last_detected_date),
        "last_mag": _fmt(stat.last_detected_mag, 2),
        "last_band": getattr(stat.last_detected_band, "name", None),
        "peak_date": _date(stat.peak_date),
        "peak_mag": _fmt(stat.peak_mag, 2),
        "peak_band": getattr(stat.peak_band, "name", None),
        "rise_rate": _fmt(stat.rise_rate, 3),
        "decay_rate": _fmt(stat.decay_rate, 3),
        "deepest_limit": _fmt(stat.deepest_limit, 2),
        "time_to_non_detection": _fmt(stat.time_to_non_detection, 1),
    }


def gather_context(transient: Transient, viewer: Optional[User] = None, max_comments: Optional[int] = None) -> Dict:
    """Everything a summary may be written from (see the module docstring for the visibility rules)."""
    from YSE_App.models import AlternateTransientNames

    max_comments = int(max_comments or _setting("SUMMARY_MAX_COMMENTS", 15) or 15)
    host = transient.host
    redshift = transient.redshift
    redshift_source = transient.redshift_source or ("transient" if redshift is not None else None)
    if redshift is None and host is not None and host.redshift is not None:
        redshift, redshift_source = host.redshift, "host"
    context = {
        "name": transient.name,
        "other_names": sorted(AlternateTransientNames.objects.filter(transient=transient).values_list("name", flat=True)),
        "ra": _fmt(transient.ra, 5),
        "dec": _fmt(transient.dec, 5),
        "discovery_date": _date(transient.disc_date),
        "status": getattr(transient.status, "name", None),
        "survey": getattr(transient.obs_group, "name", None),
        "tags": sorted(t.name for t in transient.tags.all()),
        "spec_class": getattr(transient.best_spec_class, "name", None),
        "tns_spec_class": transient.TNS_spec_class or None,
        "phot_class": getattr(transient.photo_class, "name", None),
        "context_class": getattr(transient.context_class, "name", None),
        "redshift": _fmt(redshift, 4),
        "redshift_err": _fmt(transient.redshift_err, 4),
        "redshift_source": redshift_source,
        "host": {"name": host.name, "redshift": _fmt(host.redshift, 4), "photo_z": _fmt(getattr(host, "photo_z", None), 3)}
        if host is not None else None,
        "mw_ebv": _fmt(transient.mw_ebv, 3),
        "last_non_detection": _date(transient.non_detect_date),
        "photometry": _photstat(transient),
        "spectra": _spectra(transient, viewer),
        "followups": _followups(transient, viewer),
        "annotations": _annotations(transient, viewer),
        "comments": _comments(transient, viewer, max_comments),
        "current_summary": (transient.summary or "").strip() or None,
    }
    return context


def inputs_hash(context: Dict) -> str:
    """sha256 of the canonical JSON of the context (stored as provenance)."""
    return hashlib.sha256(json.dumps(context, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# --- prompt / template ------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You write short, factual summaries of astronomical transients for a survey team's database. "
    "Use only the facts given; never invent measurements, classifications or history. "
    "Write 3 to 6 sentences of plain prose (no headings, no bullet points, no markdown): what the object is "
    "(classification and how secure it is), its redshift and source, how it has evolved photometrically, "
    "what follow-up has been done or requested, and the open questions. Give dates as YYYY-MM-DD. "
    "If little is known, say so in one sentence."
)


def _lines(context: Dict) -> List[str]:
    c = context
    lines = ["Name: %s" % c["name"]]
    if c.get("other_names"):
        lines.append("Other names: %s" % ", ".join(c["other_names"]))
    lines.append("Coordinates (deg): RA %s, Dec %s" % (c.get("ra"), c.get("dec")))
    if c.get("discovery_date"):
        lines.append("Discovery date: %s" % c["discovery_date"])
    lines.append("Status: %s%s" % (c.get("status") or "unknown", " (survey %s)" % c["survey"] if c.get("survey") else ""))
    if c.get("tags"):
        lines.append("Tags: %s" % ", ".join(c["tags"]))
    classes = [("spectroscopic", c.get("spec_class")), ("TNS", c.get("tns_spec_class")),
               ("photometric", c.get("phot_class")), ("context", c.get("context_class"))]
    classes = ["%s: %s" % (k, v) for k, v in classes if v]
    lines.append("Classification: %s" % ("; ".join(classes) if classes else "none yet"))
    if c.get("redshift") is not None:
        err = " +/- %s" % c["redshift_err"] if c.get("redshift_err") is not None else ""
        lines.append("Redshift: %s%s (source: %s)" % (c["redshift"], err, c.get("redshift_source") or "unknown"))
    else:
        lines.append("Redshift: unknown")
    host = c.get("host")
    if host:
        lines.append("Host: %s (z=%s, photo-z=%s)" % (host.get("name") or "unnamed", host.get("redshift"), host.get("photo_z")))
    if c.get("mw_ebv") is not None:
        lines.append("MW E(B-V): %s" % c["mw_ebv"])
    phot = c.get("photometry")
    if phot and phot.get("detections"):
        lines.append("Photometry: %s detections of %s points; first %s at %s %s; peak %s %s on %s; last %s %s on %s; "
                     "rise %s mag/day, decay %s mag/day; deepest pre-detection limit %s; time to non-detection %s d"
                     % (phot["detections"], phot["points"], phot["first_mag"], phot["first_band"], phot["first_detected"],
                        phot["peak_mag"], phot["peak_band"], phot["peak_date"], phot["last_mag"], phot["last_band"],
                        phot["last_detected"], phot["rise_rate"], phot["decay_rate"], phot["deepest_limit"],
                        phot["time_to_non_detection"]))
    else:
        lines.append("Photometry: no detections recorded")
    if c.get("last_non_detection"):
        lines.append("Last non-detection: %s" % c["last_non_detection"])
    spectra = c.get("spectra") or []
    if spectra:
        parts = []
        for s in spectra:
            where = "/".join(x for x in (s.get("telescope"), s.get("instrument")) if x) or "unknown instrument"
            extra = "; z=%s" % s["redshift"] if s.get("redshift") is not None else ""
            notes = "; %s" % s["notes"] if s.get("notes") else ""
            parts.append("%s with %s%s%s" % (s.get("date"), where, extra, notes))
        lines.append("Spectra (%d): %s" % (len(spectra), " | ".join(parts)))
    else:
        lines.append("Spectra: none")
    followups = c.get("followups") or []
    if followups:
        lines.append("Follow-up requests (%d): %s" % (len(followups), " | ".join(
            "%s on %s (%s)" % (f.get("status"), f.get("resource"), f.get("window")) for f in followups)))
    else:
        lines.append("Follow-up requests: none")
    annotations = c.get("annotations") or []
    if annotations:
        lines.append("Catalogue checks: %s" % "; ".join(
            "%s %s%s" % (a["origin"], a.get("verdict") or "", " (%s)" % a["summary"] if a.get("summary") else "")
            for a in annotations))
    return lines


def build_prompt(context: Dict, max_chars: Optional[int] = None) -> Tuple[str, str]:
    """``(system, user)`` prompt text; deterministic for a given context; comments are cut first to fit ``max_chars``."""
    max_chars = int(max_chars or _setting("SUMMARY_PROMPT_MAX_CHARS", 12000) or 12000)
    head = "\n".join(_lines(context))
    comments = list(context.get("comments") or [])
    while True:
        if comments:
            body = "Comments (newest first):\n" + "\n".join(
                "- [%s, %s] %s" % (c.get("date"), c.get("by"), c.get("text")) for c in comments)
        else:
            body = "Comments: none"
        user = head + "\n\n" + body + "\n\nWrite the summary now."
        if len(user) <= max_chars or not comments:
            break
        comments.pop()  # drop the oldest comment
    if len(user) > max_chars:
        user = user[: max_chars - 3] + "..."
    return SYSTEM_PROMPT, user


def render_template_summary(context: Dict) -> str:
    """Deterministic summary without an LLM (the ``template`` provider)."""
    c = context
    sentences = []
    cls = c.get("spec_class") or c.get("tns_spec_class")
    what = ("classified as %s" % cls) if cls else "not yet spectroscopically classified"
    if not cls and c.get("phot_class"):
        what += " (photometric class %s)" % c["phot_class"]
    lead = "%s is a %s transient %s" % (c["name"], (c.get("status") or "").lower() or "catalogued", what)
    if c.get("discovery_date"):
        lead += ", discovered on %s" % c["discovery_date"]
    if c.get("survey"):
        lead += " by %s" % c["survey"]
    sentences.append(lead + ".")
    if c.get("redshift") is not None:
        sentences.append("Its redshift is %s%s (%s)." % (
            c["redshift"], " +/- %s" % c["redshift_err"] if c.get("redshift_err") is not None else "",
            "from the host" if c.get("redshift_source") == "host" else "source: %s" % (c.get("redshift_source") or "unknown")))
    else:
        sentences.append("No redshift is known yet.")
    phot = c.get("photometry")
    if phot and phot.get("detections"):
        s = "It has %d detections" % phot["detections"]
        if phot.get("peak_mag") is not None:
            s += ", peaking at %s in %s on %s" % (phot["peak_mag"], phot.get("peak_band") or "?", phot.get("peak_date"))
        if phot.get("last_mag") is not None:
            s += ", and was last detected at %s in %s on %s" % (phot["last_mag"], phot.get("last_band") or "?", phot.get("last_detected"))
        rates = []
        if phot.get("rise_rate") is not None:
            rates.append("rising at %s mag/day" % phot["rise_rate"])
        if phot.get("decay_rate") is not None:
            rates.append("declining at %s mag/day" % phot["decay_rate"])
        if rates:
            s += " (%s)" % ", ".join(rates)
        sentences.append(s + ".")
    else:
        sentences.append("No detections are recorded in the photometry statistics.")
    spectra = c.get("spectra") or []
    if spectra:
        where = sorted({x.get("telescope") or x.get("instrument") or "unknown" for x in spectra})
        sentences.append("%d %s been taken (%s), the latest on %s." % (
            len(spectra), "spectrum has" if len(spectra) == 1 else "spectra have", ", ".join(where), spectra[-1].get("date")))
    else:
        sentences.append("No spectrum has been taken.")
    followups = c.get("followups") or []
    if followups:
        by_status: Dict[str, int] = {}
        for f in followups:
            by_status[f.get("status") or "unknown"] = by_status.get(f.get("status") or "unknown", 0) + 1
        sentences.append("Follow-up: %s." % ", ".join("%d %s" % (n, k.lower()) for k, n in sorted(by_status.items())))
    verdicts = [a for a in (c.get("annotations") or []) if a.get("verdict") in ("stellar", "AGN-like")]
    if verdicts:
        sentences.append("Catalogue checks flag it as %s." % ", ".join("%s (%s)" % (a["verdict"], a["origin"]) for a in verdicts))
    comments = c.get("comments") or []
    if comments:
        latest = comments[0]
        text = latest.get("text") or ""
        if len(text) > 200:
            text = text[:197] + "..."
        sentences.append("Latest comment (%s, %s): %s" % (latest.get("by"), latest.get("date"), text))
    open_q = []
    if not cls:
        open_q.append("classification")
    if c.get("redshift") is None:
        open_q.append("redshift")
    if open_q:
        sentences.append("Open questions: %s." % " and ".join(open_q))
    return " ".join(sentences)


# --- writing --------------------------------------------------------------------

def _clean_text(text) -> str:
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        raise SummaryError("the summary text is empty")
    if len(text) > TEXT_MAX_CHARS:
        text = text[:TEXT_MAX_CHARS].rstrip()
    return text


def set_summary(transient: Transient, text: str, user: User, *, source: str = SOURCE_HUMAN, provider: str = "",
                model_name: str = "", prompt_version: str = "", inputs_hash_value: str = "",
                run: Optional[ExternalServiceRun] = None) -> TransientSummaryHistory:
    """Make ``text`` the current summary: transient columns, a history version, the embedding."""
    text = _clean_text(text)
    if source not in (SOURCE_AI, SOURCE_HUMAN):
        raise SummaryError("unknown summary source %r" % source)
    now = timezone.now()
    with transaction.atomic():
        TransientSummaryHistory.objects.filter(transient=transient, is_current=True).update(is_current=False)
        version = TransientSummaryHistory.objects.create(
            transient=transient, text=text, source=source, provider=provider or "", model_name=(model_name or "")[:120],
            prompt_version=prompt_version or "", inputs_hash=inputs_hash_value or "", run=run, is_current=True,
            created_by=user, modified_by=user,
        )
        # A queryset update: no Transient.save() signals (auto-publish, annotation autorun) for a summary.
        Transient.objects.filter(pk=transient.pk).update(summary=text, summary_modified=now)
        transient.summary = text
        transient.summary_modified = now
    try:
        refresh_embedding(transient)
    except Exception:  # noqa: BLE001 - the embedding is a search aid, never a reason to lose the text
        log.exception("embedding refresh failed for %s", transient.name)
    return version


def history(transient: Transient, limit: Optional[int] = None) -> List[TransientSummaryHistory]:
    qs = TransientSummaryHistory.objects.filter(transient=transient).select_related("created_by", "run")
    return list(qs[:limit]) if limit else list(qs)


def current_version(transient: Transient) -> Optional[TransientSummaryHistory]:
    return (TransientSummaryHistory.objects.filter(transient=transient, is_current=True)
            .select_related("created_by", "run").first())


# --- embeddings + search -----------------------------------------------------------------

def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def refresh_embedding(transient: Transient, service: Optional[ExternalService] = None) -> Optional[TransientSummaryEmbedding]:
    """(Re)compute the embedding of the transient's current summary; deletes it when the summary is empty."""
    text = (transient.summary or "").strip()
    if not text:
        TransientSummaryEmbedding.objects.filter(transient=transient).delete()
        invalidate_search_cache()
        return None
    service = service or service_row(include_disabled=True)
    params = dict(service.default_params or {}) if service is not None else {}
    vectors, name = llm.embed_texts([text], service=service, params=params)
    vector = vectors[0]
    row, _ = TransientSummaryEmbedding.objects.update_or_create(
        transient=transient,
        defaults={"vector": pack_vector(vector), "dim": len(vector), "model_name": name, "text_hash": text_hash(text)},
    )
    invalidate_search_cache()
    return row


def rebuild_embeddings(model_name: Optional[str] = None) -> int:
    """Recompute every embedding (after changing the embedder); returns the number written."""
    n = 0
    service = service_row(include_disabled=True)
    for transient in Transient.objects.exclude(summary__isnull=True).exclude(summary="").iterator():
        if refresh_embedding(transient, service) is not None:
            n += 1
    return n


_cache_lock = threading.Lock()
_matrix_cache: Dict[str, Tuple[Tuple, List[int], object]] = {}


def invalidate_search_cache() -> None:
    with _cache_lock:
        _matrix_cache.clear()


def _matrix_for(model_name: str):
    """``(ids, matrix)`` of every stored vector of ``model_name``; rebuilt when the table changed."""
    import numpy as np

    qs = TransientSummaryEmbedding.objects.filter(model_name=model_name)
    agg = qs.aggregate(n=Count("id"), latest=Max("modified_date"))
    stamp = (agg["n"] or 0, agg["latest"])
    with _cache_lock:
        cached = _matrix_cache.get(model_name)
        if cached is not None and cached[0] == stamp:
            return cached[1], cached[2]
    ids: List[int] = []
    blobs: List[bytes] = []
    dim = None
    for pk, blob, d in qs.values_list("transient_id", "vector", "dim"):
        if dim is None:
            dim = d
        if d != dim or not blob:
            continue
        ids.append(pk)
        blobs.append(bytes(blob))
    if ids:
        matrix = np.frombuffer(b"".join(blobs), dtype="<f4").reshape(len(ids), dim)
    else:
        matrix = np.zeros((0, dim or 1), dtype="<f4")
    with _cache_lock:
        _matrix_cache[model_name] = (stamp, ids, matrix)
    return ids, matrix


def rank_vectors(query_vector, matrix, limit: int):
    """Indices and scores of the ``limit`` best rows of ``matrix`` for ``query_vector`` (cosine on unit vectors)."""
    import numpy as np

    if matrix.shape[0] == 0:
        return []
    q = np.asarray(query_vector, dtype="<f4")
    norm = float(np.linalg.norm(q))
    if norm:
        q = q / norm
    scores = matrix @ q
    k = min(limit, scores.shape[0])
    top = np.argpartition(-scores, k - 1)[:k] if k < scores.shape[0] else np.arange(scores.shape[0])
    top = top[np.argsort(-scores[top])]
    return [(int(i), float(scores[i])) for i in top]


def snippet(text: str, query: str = "", width: int = 240) -> str:
    """A short window of the summary around the first query word (or its start)."""
    text = " ".join((text or "").split())
    if len(text) <= width:
        return text
    lower = text.lower()
    pos = -1
    for word in llm.tokenize(query):
        pos = lower.find(word)
        if pos >= 0:
            break
    start = max(0, pos - width // 3) if pos > 0 else 0
    end = min(len(text), start + width)
    out = text[start:end]
    return ("..." if start else "") + out + ("..." if end < len(text) else "")


def search_summaries(query: str, user, limit: Optional[int] = None) -> Dict:
    """Rank transients for ``query``: ``{"mode": "embedding"|"text", "model": name, "results": [...]}``.

    Each result is ``{"transient", "score", "snippet"}``; only transients the
    user may see are returned (``filter_transients_by_user_access``).
    """
    query = " ".join((query or "").split())
    limit = int(limit or _setting("SUMMARY_SEARCH_LIMIT", 25) or 25)
    out = {"mode": "text", "model": "", "results": [], "query": query}
    if not query or user is None or not user.is_authenticated:
        return out
    service = service_row(include_disabled=True)
    params = dict(service.default_params or {}) if service is not None else {}
    mode = str(_setting("SUMMARY_SEARCH_MODE", "embedding") or "embedding").lower()
    ranked: List[Tuple[int, float]] = []
    if mode != "text":
        try:
            vectors, name = llm.embed_texts([query], service=service, params=params)
            ids, matrix = _matrix_for(name)
            out["model"] = name
            if ids:
                min_score = float(_setting("SUMMARY_SEARCH_MIN_SCORE", 0.1) or 0.0)
                candidates = rank_vectors(vectors[0], matrix, limit * 4)
                ranked = [(ids[i], score) for i, score in candidates if score >= min_score]
                out["mode"] = "embedding"
        except llm.LLMError as exc:
            log.warning("summary search: embedding failed (%s); falling back to text", exc)
    if out["mode"] == "text":
        qs = Transient.objects.filter(summary__icontains=query).order_by("-summary_modified")[: limit * 4]
        ranked = [(pk, 0.0) for pk in qs.values_list("pk", flat=True)]
    if not ranked:
        return out
    order = {pk: i for i, (pk, _score) in enumerate(ranked)}
    scores = dict(ranked)
    transients = Transient.objects.filter(pk__in=list(order)).select_related("status", "best_spec_class", "host")
    visible = list(filter_transients_by_user_access(user, transients))
    visible.sort(key=lambda t: order[t.pk])
    for transient in visible[:limit]:
        out["results"].append({
            "transient": transient,
            "score": round(scores.get(transient.pk, 0.0), 4),
            "snippet": snippet(transient.summary or "", query),
        })
    return out


# --- runs ----------------------------------------------------------------------

def active_run(transient: Transient) -> Optional[ExternalServiceRun]:
    return (ExternalServiceRun.objects.filter(
        transient=transient, service__slug=SERVICE_SLUG,
        status__in=(ExternalServiceRun.STATUS_PENDING, ExternalServiceRun.STATUS_RUNNING))
        .select_related("service").order_by("-created_date").first())


def latest_run(transient: Transient) -> Optional[ExternalServiceRun]:
    return (ExternalServiceRun.objects.filter(transient=transient, service__slug=SERVICE_SLUG)
            .select_related("service", "created_by").order_by("-created_date").first())


def request_summary(transient: Transient, user: User, *, batch: bool = False, dispatch: bool = True,
                    service: Optional[ExternalService] = None) -> ExternalServiceRun:
    """Queue one summariser run. ``batch`` runs use collaboration-wide visibility and skip the daily cap."""
    service = service or service_row()
    if service is None:
        raise SummaryError("the AI summary service is not registered or is disabled "
                           "(staff: manage.py register_summary_service)")
    if not batch and not can_generate(user, transient, service):
        raise SummaryError("you are not opted in to AI summaries, or the service is not available to your groups")
    ok, reason = llm.chat_available(service, service.default_params or {})
    if not ok:
        raise SummaryError("the summariser is not configured: %s" % reason)
    if active_run(transient) is not None:
        raise SummaryError("a summary is already being generated for %s" % transient.name)
    payload = {"summary": True, "batch": bool(batch), "viewer_id": None if batch else user.pk,
               "prompt_version": PROMPT_VERSION}
    run, _token = runs.start_run(service, user, payload, transient=transient, dispatch=dispatch,
                                 enforce_limit=not batch and not (user.is_staff or user.is_superuser))
    return run


def _provider_for(service: ExternalService) -> Dict:
    return llm.chat_config(service.default_params or {})


def generate_text(transient: Transient, service: ExternalService, viewer: Optional[User]) -> Tuple[str, Dict]:
    """Gather, prompt, call the provider (or render the template): ``(text, provenance)``."""
    context = gather_context(transient, viewer)
    digest = inputs_hash(context)
    cfg = _provider_for(service)
    provenance = {"provider": cfg["provider"], "model": "", "prompt_version": PROMPT_VERSION,
                  "inputs_hash": digest, "viewer": getattr(viewer, "username", None)}
    if cfg["provider"] == llm.PROVIDER_TEMPLATE:
        text = render_template_summary(context)
        provenance["model"] = "template-v" + PROMPT_VERSION
        return text, provenance
    system, user_prompt = build_prompt(context)
    text, meta = llm.chat(system, user_prompt, service=service, params=service.default_params or {})
    provenance["model"] = meta.get("model") or cfg["model"]
    provenance["usage"] = meta.get("usage") or {}
    return text, provenance


def run_summary(run: ExternalServiceRun) -> Dict:
    """Runner for the ``ai_summary`` service (called by ``external_services.execute_run``)."""
    run.mark_running()
    transient = run.transient
    payload = run.request_payload or {}
    viewer = None
    if not payload.get("batch") and payload.get("viewer_id"):
        viewer = User.objects.filter(pk=payload["viewer_id"]).first()
    if transient is None:
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error="the run has no transient")
        return {"ok": False}
    try:
        text, provenance = generate_text(transient, run.service, viewer)
        version = set_summary(transient, text, run.created_by, source=SOURCE_AI, provider=provenance["provider"],
                              model_name=provenance["model"], prompt_version=provenance["prompt_version"],
                              inputs_hash_value=provenance["inputs_hash"], run=run)
    except (llm.LLMError, SummaryError) as exc:
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error=str(exc))
        _notify_failure(run, str(exc))
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - keep the run row truthful, then re-raise for the job log
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error="%s: %s" % (exc.__class__.__name__, exc))
        _notify_failure(run, str(exc))
        raise
    result = dict(provenance)
    result.update({"version_id": version.pk, "chars": len(text)})
    runs.record_completion(run, ExternalServiceRun.STATUS_SUCCEEDED, result=result)
    return {"ok": True, "version_id": version.pk}


def _notify_failure(run: ExternalServiceRun, error: str) -> None:
    if (run.request_payload or {}).get("batch") or not run.created_by_id or run.transient is None:
        return
    try:
        from django.urls import reverse

        from YSE_App.services.notify import notify

        notify([run.created_by], "The AI summary of %s failed: %s" % (run.transient.name, error[:200]),
               reverse("transient_detail", kwargs={"slug": run.transient.slug}), NOTIFICATION_KIND,
               transient=run.transient)
    except Exception:  # noqa: BLE001 - a notification failure must not mask the run failure
        log.exception("could not notify %s about a failed summary", run.created_by_id)


runs.register_runner(SERVICE_SLUG, run_summary)
runs.register_runner("kind:" + ExternalService.KIND_SUMMARY, run_summary)


def fail_stale_runs(max_age_minutes: Optional[int] = None) -> int:
    """Mark pending / running summary runs older than N minutes as failed (a lost worker)."""
    minutes = int(max_age_minutes or _setting("SUMMARY_RUN_STALE_MINUTES", 30) or 30)
    cutoff = timezone.now() - timezone.timedelta(minutes=minutes)
    qs = ExternalServiceRun.objects.filter(
        service__slug=SERVICE_SLUG, created_date__lt=cutoff,
        status__in=(ExternalServiceRun.STATUS_PENDING, ExternalServiceRun.STATUS_RUNNING))
    n = 0
    for run in qs:
        run.mark_failed("no worker completed this run within %d minutes" % minutes)
        n += 1
    return n


# --- nightly batch (#296) ------------------------------------------------------------

def stale_transients(hours: Optional[int] = None, limit: Optional[int] = None) -> List[Transient]:
    """Transients with a new public comment, spectrum, follow-up or edit since their summary was written."""
    from YSE_App.models import Log, TransientFollowup, TransientSpectrum

    hours = int(hours or _setting("SUMMARY_BATCH_HOURS", 24) or 24)
    limit = int(limit or _setting("SUMMARY_BATCH_MAX", 200) or 200)
    since = timezone.now() - timezone.timedelta(hours=hours)
    touched = set(Log.objects.filter(transient__isnull=False, transient_followup__isnull=True, is_public=True,
                                     modified_date__gte=since).values_list("transient_id", flat=True))
    touched.update(TransientSpectrum.objects.filter(modified_date__gte=since).values_list("transient_id", flat=True))
    touched.update(TransientFollowup.objects.filter(modified_date__gte=since).values_list("transient_id", flat=True))
    touched.update(Transient.objects.filter(modified_date__gte=since).values_list("pk", flat=True))
    if not touched:
        return []
    latest_activity = {}
    for model, field in ((Log, "transient_id"), (TransientSpectrum, "transient_id"), (TransientFollowup, "transient_id")):
        rows = model.objects.filter(**{field + "__in": list(touched)}).values(field).annotate(m=Max("modified_date"))
        for row in rows:
            pk = row[field]
            latest_activity[pk] = max(latest_activity.get(pk, row["m"]), row["m"])
    out = []
    for transient in Transient.objects.filter(pk__in=list(touched)).order_by("-modified_date"):
        newest = max(filter(None, (latest_activity.get(transient.pk), transient.modified_date)))
        if transient.summary_modified is not None and transient.summary_modified >= newest:
            continue
        out.append(transient)
        if len(out) >= limit:
            break
    return out


@job(BATCH_JOB_KIND)
def refresh_stale(payload=None, job=None):
    """Queue a batch (collaboration-wide) summariser run for every stale transient."""
    payload = payload or {}
    service = service_row()
    if service is None:
        return {"queued": 0, "skipped": "no enabled ai_summary service"}
    ok, reason = llm.chat_available(service, service.default_params or {})
    if not ok:
        return {"queued": 0, "skipped": reason}
    actor = service.created_by
    queued = []
    for transient in stale_transients(payload.get("hours"), payload.get("limit")):
        if active_run(transient) is not None:
            continue
        try:
            run = request_summary(transient, actor, batch=True, service=service)
        except (SummaryError, runs.ExternalServiceError) as exc:
            log.info("batch summary of %s skipped: %s", transient.name, exc)
            continue
        queued.append(str(run.uuid))
    return {"queued": len(queued), "runs": queued}


def queue_refresh(hours: Optional[int] = None, limit: Optional[int] = None, created_by=None):
    """Enqueue one ``summaries.refresh_stale`` job (the cron calls this)."""
    from YSE_App.models.job_models import Job

    if Job.objects.filter(kind=BATCH_JOB_KIND, status__in=Job.ACTIVE_STATUSES).exists():
        return None
    payload = {}
    if hours:
        payload["hours"] = int(hours)
    if limit:
        payload["limit"] = int(limit)
    return enqueue(BATCH_JOB_KIND, payload, created_by=created_by)


def summaries_for(transients: Iterable[Transient]) -> Dict[int, str]:
    """``{transient_id: first line}`` for table tooltips."""
    return {t.pk: first_line(t.summary) for t in transients if t.summary}


__all__ = [
    "BATCH_JOB_KIND", "PROMPT_VERSION", "SERVICE_SLUG", "SummaryError", "active_run", "build_prompt", "can_edit",
    "can_generate", "can_view", "current_version", "ensure_service", "fail_stale_runs", "gather_context",
    "generate_text", "group_allows", "history", "inputs_hash", "latest_run", "opted_in", "queue_refresh",
    "rank_vectors", "rebuild_embeddings", "refresh_embedding", "refresh_stale", "render_template_summary",
    "request_summary", "run_summary", "search_summaries", "service_row", "set_opt_in", "set_summary", "snippet",
    "stale_transients", "summaries_for",
]
