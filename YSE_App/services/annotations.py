"""Read and write :class:`TransientAnnotation` rows and start annotation services (#316).

* :func:`upsert` is the one call ingest code, the API and the runners use to
  write an origin's document; it replaces the document, keeps ``created_by``,
  records the run / service that produced it and rebuilds the indexed value
  rows.
* :func:`visible_annotations` / :func:`annotations_for_transients` apply the
  group audience (staff see everything).
* :func:`legacy_annotation` presents the annotation-like columns that already
  live on ``Transient`` (``point_source_probability``, ``real_bogus_score``,
  ``antares_classification``, ``mw_ebv``, ``has_hst``, ``has_jwst`` ...) as a read-only
  ``origin='legacy'`` document, so the API and the detail panel show one list
  and the search filters accept ``legacy.<column>`` (no data migration).
* :func:`annotation_services` lists the ``ExternalService`` rows of kind
  ``annotation``; :func:`start_annotation_run` puts a run on the job queue
  (``external_services.start_run``) whose runner (``YSE_App.annotation_services``)
  writes the annotation and completes the run. :func:`ensure_builtin_services`
  registers the three built-in checks (Gaia DR3, WISE, quasar catalogue).
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional, Tuple

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone

from YSE_App.models.annotation_models import (
    BADGE_VERDICTS,
    LEGACY_ORIGIN,
    USER_ORIGIN_PREFIX,
    VERDICT_KEY,
    TransientAnnotation,
)
from YSE_App.models.external_service_models import ExternalService, ExternalServiceRun
from YSE_App.models.transient_models import Transient
from YSE_App.services import external_services as runs

log = logging.getLogger(__name__)


class AnnotationError(Exception):
    """Bad input to :func:`upsert` or a write the caller may not make."""


# --- built-in annotation services (#318) ---------------------------------------
# slug -> (name, description). The runners live in YSE_App.annotation_services.
BUILTIN_SERVICES = (
    ("gaia_dr3", "Gaia DR3 check",
     "Cone search of Gaia DR3: parallax, proper motion, G, BP-RP and a stellar verdict."),
    ("wise", "WISE colour check",
     "AllWISE (CatWISE fallback) via VizieR: W1, W2, W3 and the W1-W2 colour with an AGN-like verdict."),
    ("quasar", "Quasar catalogue check",
     "Million Quasar Catalog (Milliquas) via VizieR: type, redshift, quasar probability."),
)
BUILTIN_SLUGS = tuple(slug for slug, _, _ in BUILTIN_SERVICES)


def default_radius_arcsec() -> float:
    return float(getattr(settings, "ANNOTATION_SEARCH_RADIUS_ARCSEC", 3.0) or 3.0)


# --- writing ----------------------------------------------------------------------

def _clean_data(data) -> Dict:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise AnnotationError("annotation data must be a JSON object (key -> value)")
    out = {}
    for key, value in data.items():
        key = str(key).strip()
        if not key:
            raise AnnotationError("annotation keys must be non-empty strings")
        if len(key) > 64:
            raise AnnotationError("annotation key %r is longer than 64 characters" % key)
        out[key] = value
    return out


def _clean_origin(origin) -> str:
    origin = str(origin or "").strip()
    if not origin:
        raise AnnotationError("an annotation needs an origin")
    if len(origin) > 64:
        raise AnnotationError("origin is longer than 64 characters")
    if origin == LEGACY_ORIGIN:
        raise AnnotationError("'%s' is reserved for the Transient columns" % LEGACY_ORIGIN)
    return origin


def user_origin(user) -> str:
    return USER_ORIGIN_PREFIX + user.username


def can_write_origin(user, origin: str) -> bool:
    """Staff and superusers write any origin; a user writes only ``user:<their username>``."""
    if user is None or not user.is_authenticated:
        return False
    if user.is_staff or user.is_superuser:
        return True
    return origin == user_origin(user)


def can_manage(user, annotation: TransientAnnotation) -> bool:
    return can_write_origin(user, annotation.origin)


def upsert(
    transient: Transient,
    origin: str,
    data: Dict,
    user: Optional[User] = None,
    groups: Optional[Iterable[Group]] = None,
    run: Optional[ExternalServiceRun] = None,
    service: Optional[ExternalService] = None,
    merge: bool = False,
) -> Tuple[TransientAnnotation, bool]:
    """Write ``origin``'s document on ``transient``; ``(annotation, created)``.

    The document replaces the stored one (``merge=True`` updates keys instead).
    ``user`` becomes ``created_by`` on a new row and ``modified_by`` always;
    without one the transient's creator is used. ``groups`` (when not None)
    replaces the audience. The indexed value rows are rebuilt.
    """
    origin = _clean_origin(origin)
    data = _clean_data(data)
    actor = user if user is not None and getattr(user, "pk", None) else transient.created_by
    with transaction.atomic():
        annotation = TransientAnnotation.objects.filter(transient=transient, origin=origin).first()
        created = annotation is None
        if created:
            annotation = TransientAnnotation(transient=transient, origin=origin, created_by=actor)
        elif merge:
            merged = dict(annotation.data if isinstance(annotation.data, dict) else {})
            merged.update(data)
            data = merged
        annotation.data = data
        annotation.modified_by = actor
        if run is not None:
            annotation.run = run
            annotation.service = run.service
        if service is not None:
            annotation.service = service
        annotation.save()
        if groups is not None:
            annotation.groups.set(list(groups))
        annotation.sync_values()
    return annotation, created


def delete_annotation(annotation: TransientAnnotation) -> None:
    annotation.delete()


# --- reading ----------------------------------------------------------------------

def _base_qs():
    return (TransientAnnotation.objects
            .select_related("created_by", "modified_by", "run", "service")
            .prefetch_related(Prefetch("groups", queryset=Group.objects.only("id", "name"))))


def _visible_filter(qs, user):
    if user is None or not user.is_authenticated:
        return qs.none()
    if user.is_staff or user.is_superuser:
        return qs
    return qs.filter(groups__isnull=True) | qs.filter(groups__in=user.groups.all())


def visible_annotations(transient: Transient, user) -> List[TransientAnnotation]:
    """The transient's annotations ``user`` may see, ordered by origin (one query + prefetch)."""
    qs = _visible_filter(_base_qs().filter(transient=transient), user)
    return list(qs.distinct().order_by("origin"))


def annotations_for_transients(transients: Iterable[Transient], user) -> Dict[int, List[TransientAnnotation]]:
    """``{transient_id: [annotations]}`` for many transients in one query."""
    ids = [t.pk for t in transients]
    out: Dict[int, List[TransientAnnotation]] = {pk: [] for pk in ids}
    if not ids:
        return out
    qs = _visible_filter(_base_qs().filter(transient_id__in=ids), user).distinct().order_by("transient_id", "origin")
    for annotation in qs:
        out.setdefault(annotation.transient_id, []).append(annotation)
    return out


def badges_for(annotations: Iterable[TransientAnnotation]) -> List[Dict]:
    """``[{origin, verdict, summary}]`` for annotations whose verdict earns a Summary-tab badge."""
    out = []
    for annotation in annotations:
        if annotation.verdict in BADGE_VERDICTS:
            out.append({"origin": annotation.origin, "verdict": annotation.verdict, "summary": annotation.summary})
    return out


# --- legacy shim (#317) -------------------------------------------------------------
# key -> (Transient attribute, how to read it). Kept read-only: filters on
# ``legacy.<key>`` go straight to the column (see filters/transient_search.py).
LEGACY_FIELDS = (
    ("point_source_probability", "point_source_probability"),
    ("real_bogus_score", "real_bogus_score"),
    ("mw_ebv", "mw_ebv"),
    ("antares_classification", "antares_classification"),
    ("alt_status", "alt_status"),
    ("has_hst", "has_hst"),
    ("has_jwst", "has_jwst"),
    ("has_spitzer", "has_spitzer"),
    ("has_chandra", "has_chandra"),
    ("TNS_spec_class", "TNS_spec_class"),
)
# Keys whose column is numeric or boolean (usable with the min / max filters).
LEGACY_NUMERIC_KEYS = ("point_source_probability", "real_bogus_score", "mw_ebv",
                       "has_hst", "has_jwst", "has_spitzer", "has_chandra")
LEGACY_BOOLEAN_KEYS = ("has_hst", "has_jwst", "has_spitzer", "has_chandra")
LEGACY_COLUMNS = {key: column for key, column in LEGACY_FIELDS}


def legacy_data(transient: Transient) -> Dict:
    """The non-null annotation-like columns of ``transient`` as a flat dict."""
    out = {}
    for key, column in LEGACY_FIELDS:
        value = getattr(transient, column, None)
        if value is None or value == "":
            continue
        if column == "antares_classification":
            value = getattr(value, "name", str(value))
        out[key] = value
    return out


def legacy_annotation(transient: Transient) -> Optional[Dict]:
    """A read-only ``origin='legacy'`` entry shaped like the API's annotation JSON, or None when empty."""
    data = legacy_data(transient)
    if not data:
        return None
    return {
        "id": None,
        "transient": transient.pk,
        "transient_name": transient.name,
        "origin": LEGACY_ORIGIN,
        "data": data,
        "groups": [],
        "service": None,
        "run": None,
        "created_by": None,
        "modified_by": None,
        "created_date": transient.created_date.isoformat() if transient.created_date else None,
        "modified_date": transient.modified_date.isoformat() if transient.modified_date else None,
        "read_only": True,
    }


# --- services (#318) ----------------------------------------------------------------

def annotation_services(user=None, include_disabled: bool = False):
    """``ExternalService`` rows of kind ``annotation``, optionally only those ``user`` may run."""
    qs = ExternalService.objects.filter(kind=ExternalService.KIND_ANNOTATION).prefetch_related("groups")
    if not include_disabled:
        qs = qs.filter(enabled=True)
    services = list(qs.order_by("name"))
    if user is not None:
        services = [s for s in services if s.visible_to(user)]
    return services


def can_run_service(user, service: ExternalService) -> bool:
    """Staff and superusers; other users only when the service names their group explicitly."""
    if user is None or not user.is_authenticated or not service.enabled:
        return False
    if user.is_staff or user.is_superuser:
        return True
    group_ids = {g.pk for g in service.groups.all()}
    if not group_ids:
        return False
    return bool(group_ids & set(user.groups.values_list("pk", flat=True)))


def ensure_builtin_services(user: User, enabled: bool = True) -> List[ExternalService]:
    """Create (or update the description of) the three built-in annotation services."""
    out = []
    for slug, name, description in BUILTIN_SERVICES:
        service, created = ExternalService.objects.get_or_create(
            slug=slug,
            defaults={"name": name, "kind": ExternalService.KIND_ANNOTATION, "description": description,
                      "enabled": enabled, "created_by": user, "modified_by": user},
        )
        changed = []
        if service.kind != ExternalService.KIND_ANNOTATION:
            service.kind = ExternalService.KIND_ANNOTATION
            changed.append("kind")
        if not service.description:
            service.description = description
            changed.append("description")
        if changed:
            service.modified_by = user
            service.save(update_fields=changed + ["modified_by", "modified_date"])
        out.append(service)
    return out


def start_annotation_run(service: ExternalService, transient: Transient, user: User,
                         radius_arcsec: Optional[float] = None, dispatch: bool = True) -> ExternalServiceRun:
    """Queue one run of ``service`` on ``transient`` (the runner writes the annotation)."""
    if service.kind != ExternalService.KIND_ANNOTATION:
        raise AnnotationError("%s is not an annotation service" % service.slug)
    payload = {
        "annotation": True,
        "ra": float(transient.ra), "dec": float(transient.dec),
        "radius_arcsec": float(radius_arcsec or default_radius_arcsec()),
    }
    run, _token = runs.start_run(service, user, payload, transient=transient, dispatch=dispatch)
    return run


def latest_runs(transient: Transient, services: Iterable[ExternalService]) -> Dict[int, ExternalServiceRun]:
    """``{service_id: latest run}`` for the transient, one query."""
    ids = [s.pk for s in services]
    out: Dict[int, ExternalServiceRun] = {}
    if not ids:
        return out
    qs = (ExternalServiceRun.objects.filter(transient=transient, service_id__in=ids)
          .select_related("created_by").order_by("-created_date"))
    for run in qs:
        out.setdefault(run.service_id, run)
    return out


def fail_stale_runs(max_age_minutes: Optional[int] = None) -> int:
    """Mark pending / running annotation runs older than N minutes as failed (a lost worker)."""
    minutes = max_age_minutes or int(getattr(settings, "ANNOTATION_RUN_STALE_MINUTES", 60) or 60)
    cutoff = timezone.now() - timezone.timedelta(minutes=minutes)
    qs = ExternalServiceRun.objects.filter(
        service__kind=ExternalService.KIND_ANNOTATION,
        status__in=(ExternalServiceRun.STATUS_PENDING, ExternalServiceRun.STATUS_RUNNING),
        created_date__lt=cutoff,
    )
    n = 0
    for run in qs:
        run.mark_failed("no worker completed this run within %d minutes" % minutes)
        n += 1
    return n


def autorun_slugs() -> List[str]:
    """Slugs of the services ``ANNOTATION_AUTORUN_SERVICES`` names (empty = off)."""
    raw = getattr(settings, "ANNOTATION_AUTORUN_SERVICES", "") or ""
    if isinstance(raw, str):
        raw = raw.split(",")
    return [s.strip() for s in raw if s and s.strip()]


def autorun_for_new_transient(transient: Transient) -> List[ExternalServiceRun]:
    """Queue the configured checks for a freshly created transient (post_save hook)."""
    slugs = autorun_slugs()
    if not slugs:
        return []
    out = []
    services = ExternalService.objects.filter(kind=ExternalService.KIND_ANNOTATION, enabled=True, slug__in=slugs)
    for service in services:
        try:
            out.append(start_annotation_run(service, transient, transient.created_by))
        except Exception:  # noqa: BLE001 - never break a transient save over a lookup
            log.exception("autorun of %s for %s failed to enqueue", service.slug, transient.name)
    return out


__all__ = [
    "AnnotationError", "BUILTIN_SERVICES", "BUILTIN_SLUGS", "LEGACY_COLUMNS", "LEGACY_FIELDS",
    "LEGACY_NUMERIC_KEYS", "VERDICT_KEY", "annotation_services", "annotations_for_transients",
    "autorun_for_new_transient", "autorun_slugs", "badges_for", "can_manage", "can_run_service",
    "can_write_origin", "default_radius_arcsec", "delete_annotation", "ensure_builtin_services",
    "fail_stale_runs", "latest_runs", "legacy_annotation", "legacy_data", "start_annotation_run",
    "upsert", "user_origin", "visible_annotations",
]
