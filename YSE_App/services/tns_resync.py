"""TNS re-sync: bring classifications, photometry and spectra up to date from TNS (#438).

The legacy crons in ``YSE_App.data_ingest.TNS_uploads`` import *new* TNS objects
(``TNS_emails``, ``TNS_recent``), but nothing re-reads TNS for objects we already
have (``TNS_updates`` runs with ``doTNS=False``), and objects reported while the
crons were down are never imported. This module does both, one object at a time:

* :func:`resync_object` is the plain entry point (no argparse, no ``settings.ini``
  parsing): fetch one TNS object with its photometry and spectra and apply only the
  differences. #114 (query TNS on a follow-up request) calls it.
* :func:`import_missing` lists the TNS objects made public since a date (paged) and
  imports the ones we lack.
* :func:`sweep` is the regular job (``tns.resync_sweep``, queued by the
  ``TNS_Resync.TNSResync`` cron): a short gap import, then the transients whose last
  re-sync is oldest, within a request budget.
* ``manage.py tns_resync`` is the backfill (dry run by default).

Rules applied to an existing transient:

* ``TNS_spec_class`` follows TNS. ``best_spec_class`` is set from TNS only when we have
  none, or when ours is the previous TNS classification (no better one of ours).
  Differences are recorded by :func:`record_classification_difference`.
* ``redshift`` is written only when :mod:`YSE_App.services.redshift_precedence` allows it:
  when empty or when it came from TNS, never over an override (#436, #437).
* New TNS photometry points and spectra go through the normal upload path
  (``data_utils._add_transient_data_stage`` inside ``photstat.deferred_updates()``),
  as the TNS cron's service account, so they get the TNS header, the Public group and
  the ``TNS <prefix> <name>`` reference (#645), and PhotStat is refreshed once per
  object (#269). A point is already present when the transient has a point of the
  same instrument and band within ``MJD_MATCH_DAYS``; a spectrum when it has a
  spectrum within ``SPECTRUM_MATCH_DAYS`` of the TNS observation date.

Each applied re-sync is recorded as an ``ExternalServiceRun`` of the ``tns`` service
(``transient``, ``status``, ``finished_at``, ``result``): that is the per-transient
"last TNS sync" the sweep and the backfill resume from. A re-sync that changes nothing
updates the transient's previous no-change run instead of adding a row, so the table
grows with changes, not with checks. Dry runs write nothing.
"""

from __future__ import annotations

import datetime
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Iterable, List, Optional, Sequence, Union

from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import Case, IntegerField, OuterRef, Q, Subquery, Value, When
from django.utils import timezone

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.common.data_references import (
    TNS_SPECTRUM_REPORT_KEYS,
    tns_reference_with_reports,
    tns_report_id,
)
from YSE_App.jobs import JobRetry, job
from YSE_App.models.external_service_models import ExternalService, ExternalServiceRun
from YSE_App.models.phot_models import TransientPhotData
from YSE_App.models.sharing_models import SharingService, is_tns_name
from YSE_App.models.spectra_models import TransientSpectrum
from YSE_App.models.enum_models import TransientClass
from YSE_App.models.transient_models import AlternateTransientNames, Transient
from YSE_App.services import redshift_precedence
from YSE_App.sharing import tns

log = logging.getLogger(__name__)

SERVICE_SLUG = "tns"
SWEEP_KIND = "tns.resync_sweep"
REDSHIFT_SOURCE = "TNS"
TNS_NAME_REGEX = r"^20[0-9]{2}[a-zA-Z]{1,5}$"
ACTIVE_STATUSES = ("New", "Following", "Watch", "FollowupRequested", "Interesting")
MJD_MATCH_DAYS = 0.01  # the upload path's own ``mjdmatchmin`` for TNS photometry
SPECTRUM_MATCH_DAYS = 0.01
SEARCH_PAGE_SIZE = 50  # TNS returns at most 50 rows per search page

TransientOrName = Union[Transient, str]


class ResyncError(Exception):
    """The re-sync cannot run (no TNS credentials, no service account, upload refused)."""


def _setting(name, default):
    value = getattr(settings, name, default)
    return default if value is None else value


# --- who and with what ---------------------------------------------------------

def service_user() -> User:
    """The account the TNS cron uploads as: the ``[main] dblogin`` account, else the sharing system user."""
    for username in _setting("API_SERVICE_USERNAMES", ()) or ():
        user = User.objects.filter(username=username, is_active=True).first()
        if user is not None:
            return user
    user = tns.system_user()
    if user is None:
        raise ResyncError("No service account: set [main] dblogin (or [sharing] SYSTEM_USERNAME).")
    return user


def _usable(value) -> bool:
    value = str(value or "").strip()
    return bool(value) and not value.startswith("<")


def default_client() -> tns.TNSClient:
    """A production TNS client (never the sandbox).

    ``[sharing] TNS_RESYNC_SERVICE`` names a TNS sharing service to use; otherwise the
    ``[main]`` bot the TNS crons use, otherwise any enabled production TNS service with a credential.
    """
    services = SharingService.objects.filter(kind=SharingService.KIND_TNS, enabled=True, testing=False)
    slug = _setting("TNS_RESYNC_SERVICE", "")
    if slug:
        service = services.filter(slug=slug).first()
        if service is None or not service.has_credential:
            raise ResyncError("TNS_RESYNC_SERVICE %r is not an enabled production TNS service with a credential."
                              % slug)
        return tns.TNSClient.for_service(service)
    key, bot_id, bot_name = (_setting("TNSAPIKEY", ""), _setting("TNSID", ""), _setting("TNSUSER", ""))
    if _usable(key) and _usable(bot_id) and _usable(bot_name):
        return tns.TNSClient(_setting("TNS_API_URL", "https://www.wis-tns.org/api"), key, bot_id, bot_name)
    for service in services.select_related("credential").order_by("slug"):
        if service.has_credential:
            return tns.TNSClient.for_service(service)
    raise ResyncError("No TNS credentials: set the [main] TNS bot keys or enable a production TNS sharing service.")


class Throttle:
    """At least ``interval`` seconds between TNS requests (``TNS_REQUEST_INTERVAL_SECONDS``)."""

    def __init__(self, interval: Optional[float] = None):
        if interval is None:
            interval = _setting("SHARING_TNS_REQUEST_INTERVAL_SECONDS", 1.0)
        self.interval = max(0.0, float(interval))
        self.requests = 0
        self._last = None

    def wait(self):
        if self._last is not None and self.interval:
            delay = self.interval - (time.monotonic() - self._last)
            if delay > 0:
                time.sleep(delay)
        self._last = time.monotonic()
        self.requests += 1


def tns_service(user: Optional[User] = None, *, create: bool = True) -> Optional[ExternalService]:
    """The ``tns`` ExternalService whose runs record each transient's last re-sync."""
    service = ExternalService.objects.filter(slug=SERVICE_SLUG).first()
    if service is None and create:
        user = user or service_user()
        service, _ = ExternalService.objects.get_or_create(
            slug=SERVICE_SLUG,
            defaults={
                "name": "TNS re-sync", "kind": ExternalService.KIND_SHARING,
                "description": "Re-sync of TNS classifications, photometry and spectra (#438). "
                               "One run per transient re-sync; no runner, nothing to start by hand.",
                "created_by": user, "modified_by": user,
            },
        )
    return service


# --- result --------------------------------------------------------------------

@dataclass
class ResyncResult:
    name: str
    tns_name: str = ""
    transient_id: Optional[int] = None
    action: str = "unchanged"  # unchanged | updated | imported | not_found | error
    dry_run: bool = True
    fields: dict = field(default_factory=dict)
    classification: Optional[dict] = None
    new_photometry: int = 0
    new_spectra: int = 0
    spectra_failed: int = 0
    error: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.fields or self.new_photometry or self.new_spectra or self.action == "imported")

    def as_dict(self) -> dict:
        data = asdict(self)
        data["changed"] = self.changed
        return data

    def summary(self) -> str:
        if self.action in ("error", "not_found"):
            return "%s: %s%s" % (self.name, self.action, (" (%s)" % self.error) if self.error else "")
        verb = {"imported": ("import", "imported"), "updated": ("update", "updated")}.get(self.action)
        parts = []
        if self.fields:
            parts.append(", ".join("%s %r -> %r" % (k, v[0], v[1]) for k, v in sorted(self.fields.items())))
        if self.new_photometry:
            parts.append("%d new photometry points" % self.new_photometry)
        if self.new_spectra:
            parts.append("%d new spectra" % self.new_spectra)
        if self.spectra_failed:
            parts.append("%d spectra unreadable" % self.spectra_failed)
        if not verb:
            return "%s: unchanged%s" % (self.name, ("; " + "; ".join(parts)) if parts else "")
        prefix = ("would %s" % verb[0]) if self.dry_run else verb[1]
        return "%s: %s%s" % (self.name, prefix, (": " + "; ".join(parts)) if parts else "")


def record_classification_difference(transient: Optional[Transient], *, tns_class: str, previous_tns_class: str,
                                     our_class: str, best_class_set: bool) -> Optional[dict]:
    """The decision-trail record of a TNS classification that differs from ours, or None.

    This is the one place a classification difference is recorded. Today it returns the
    entry stored in the run's ``result["classification"]``; once the decision-trail table
    (``ClassificationEvent``, Stage 5) lands, it also writes that row here with source ``TNS``.
    """
    tns_class = (tns_class or "").strip()
    previous_tns_class = (previous_tns_class or "").strip()
    our_class = (our_class or "").strip()
    if not tns_class or (tns_class == previous_tns_class and not best_class_set):
        return None  # nothing new from TNS (a standing difference was recorded when TNS changed)
    return {
        "source": "TNS",
        "tns_class": tns_class,
        "previous_tns_class": previous_tns_class,
        "our_class": our_class,
        "differs_from_ours": bool(our_class) and our_class != tns_class,
        "best_class_set": bool(best_class_set),
        "transient": getattr(transient, "pk", None),
    }


# --- TNS object -> upload documents ----------------------------------------------

def _aware(value) -> Optional[datetime.datetime]:
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        text = str(value).strip().replace("T", " ")
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.datetime.strptime(text[:26], fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, datetime.timezone.utc)
    return dt


def _legacy_proc():
    """The cron's ``processTNS`` parser, configured only for parsing (imported lazily: it is heavy)."""
    from YSE_App.data_ingest.TNS_uploads import processTNS

    proc = processTNS()
    proc.clobber = False
    proc.status = "New"
    proc.fixture_skip_archival = True  # HST/Chandra/Spitzer flags are deferred to get_archival / the page
    return proc


def _photometry_upload(jd: dict) -> Optional[dict]:
    """The TNS cron's photometry upload document for ``jd`` (TNS header, Public, reference), or None."""
    if not jd.get("photometry"):
        return None
    try:
        phot, *_rest = _legacy_proc().getTNSPhotometry(jd)
    except (ValueError, KeyError, TypeError, IndexError) as exc:  # e.g. no magnitude at all
        log.info("TNS re-sync: %s photometry not usable: %s", jd.get("objname"), exc)
        return None
    return phot


def _point_key(instrument: str, band: str):
    from YSE_App.common.tns_photometry_map import resolve_tns_photometry

    resolved = resolve_tns_photometry(instrument, band)
    return resolved.instrument, resolved.band


def _existing_points(transient: Transient) -> dict:
    """``{(instrument, band): [mjd, ...]}`` of every photometry point the transient already has."""
    from astropy.time import Time

    rows = [r for r in TransientPhotData.objects.filter(photometry__transient=transient).values_list(
        "photometry__instrument__name", "band__name", "obs_date") if r[2] is not None]
    out = {}
    if not rows:
        return out
    mjds = Time([_aware(r[2]).astimezone(datetime.timezone.utc).replace(tzinfo=None) for r in rows]).mjd
    for (inst, band, _date), mjd in zip(rows, [float(m) for m in mjds]):
        out.setdefault((inst, band), []).append(mjd)
    return out


def _new_points_only(phot: Optional[dict], existing: dict) -> (Optional[dict], int):
    """``phot`` with only the points ``existing`` lacks (and no empty headers), and their count."""
    from astropy.time import Time

    if not phot:
        return None, 0
    out = {k: v for k, v in phot.items() if not isinstance(v, dict)}
    count = 0
    for key, block in phot.items():
        if not isinstance(block, dict) or "photdata" not in block:
            continue
        kept = {}
        for pkey, point in block["photdata"].items():
            mjd = float(Time(_aware(point["obs_date"]).replace(tzinfo=None)).mjd)
            have = existing.get(_point_key(block["instrument"], point["band"]), [])
            if any(abs(mjd - m) < MJD_MATCH_DAYS for m in have):
                continue
            kept[pkey] = point
        if kept:
            out[key] = dict(block, photdata=kept)
            count += len(kept)
    if not count:
        return None, 0
    out.setdefault("mjdmatchmin", MJD_MATCH_DAYS)
    out["clobber"] = False
    return out, count


def _spectrum_present(transient: Optional[Transient], obs_date) -> bool:
    when = _aware(obs_date)
    if transient is None or when is None:
        return False
    delta = datetime.timedelta(days=SPECTRUM_MATCH_DAYS)
    return TransientSpectrum.objects.filter(transient=transient, obs_date__gte=when - delta,
                                            obs_date__lte=when + delta).exists()


def parse_spectrum_text(text: str) -> dict:
    """``{index: {wavelength, flux, flux_err}}`` from a TNS ascii spectrum (the cron's line filter)."""
    specdata = {}
    for line in (text or "").splitlines():
        if (":" in line or "=" in line or not line.strip() or "END" in line or "GROUPS" in line
                or "SNID " in line or line.lstrip().startswith("#")):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            wavelength, flux = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        if flux != flux:  # NaN
            continue
        specdata[str(len(specdata))] = {"wavelength": wavelength, "flux": flux, "flux_err": None}
    return specdata


def _spectrum_upload(jd: dict, spec: dict, text: str, ra: float, dec: float) -> Optional[dict]:
    """One spectrum in the TNS cron's upload shape (Public group, TNS reference with the report id)."""
    import re

    specdata = parse_spectrum_text(text)
    if not specdata:
        return None
    group = spec.get("source_group_name")
    if not group and isinstance(spec.get("source_group"), dict):
        group = spec["source_group"].get("name")
    when = _aware(spec.get("obsdate"))
    return {
        "ra": ra, "dec": dec,
        "instrument": ((spec.get("instrument") or {}).get("name") or "Unknown"),
        "obs_date": when.isoformat() if when else spec.get("obsdate"),
        "obs_group": re.sub(r"[^\x00-\x7f]", "", group or "Unknown") or "Unknown",
        "groups": PUBLIC_COLLABORATION_GROUP_NAME,
        "reference": tns_reference_with_reports(jd, [tns_report_id(spec, TNS_SPECTRUM_REPORT_KEYS)]),
        "specdata": specdata,
    }


def _new_spectra(jd: dict, transient: Optional[Transient], client, throttle: Throttle, *, download: bool,
                 ra: float, dec: float, result: ResyncResult) -> Optional[dict]:
    """Upload documents for the TNS spectra ``transient`` lacks (downloaded only when ``download``)."""
    specs = {}
    for spec in jd.get("spectra") or []:
        if not isinstance(spec, dict) or not spec.get("asciifile"):
            continue
        if _spectrum_present(transient, spec.get("obsdate")):
            continue
        if not download:
            result.new_spectra += 1
            continue
        throttle.wait()
        try:
            text = client.get_file_text(spec["asciifile"])
        except tns.TNSRateLimited:
            raise
        except tns.TNSError as exc:
            log.warning("TNS re-sync: %s spectrum %s: %s", jd.get("objname"), spec.get("asciifile"), exc)
            result.spectra_failed += 1
            continue
        upload = _spectrum_upload(jd, spec, text, ra, dec)
        if upload is None:
            result.spectra_failed += 1
            continue
        specs[spec["asciifile"]] = upload
        result.new_spectra += 1
    if not specs:
        return None
    specs["clobber"] = False
    return specs


# --- one object ----------------------------------------------------------------

def resolve_transient(transient_or_name: TransientOrName) -> (Optional[Transient], str):
    """``(transient or None, TNS name to query)`` for a Transient, a name or an alternate name."""
    if isinstance(transient_or_name, Transient):
        # read the current row: differences are computed against the database, not a stale instance
        transient = (Transient.objects.select_related("best_spec_class").filter(pk=transient_or_name.pk).first()
                     or transient_or_name)
    else:
        name = str(transient_or_name or "").strip()
        transient = Transient.objects.filter(name=name).first()
        if transient is None:
            alt = AlternateTransientNames.objects.filter(name=name).select_related("transient").first()
            transient = alt.transient if alt is not None else None
        if transient is None:
            return None, name
    return transient, tns.tns_name_for(transient)


def _class_name(cls) -> str:
    return getattr(cls, "name", "") or ""


def _field_changes(transient: Transient, jd: dict, result: ResyncResult) -> dict:
    """The Transient attributes to change (``{attr: new}``); fills ``result.fields`` / ``classification``."""
    updates = {}
    object_type = jd.get("object_type") or {}
    tns_class = (object_type.get("name") if isinstance(object_type, dict) else "") or ""
    tns_class = str(tns_class).strip()
    previous = (transient.TNS_spec_class or "").strip()
    ours = _class_name(transient.best_spec_class)
    best_set = False
    if tns_class:
        if tns_class != previous:
            updates["TNS_spec_class"] = tns_class
            result.fields["TNS_spec_class"] = (previous or None, tns_class)
        if ours != tns_class and (not ours or ours == previous):
            new_best = TransientClass.objects.filter(name=tns_class).first()
            if new_best is not None:
                updates["best_spec_class"] = new_best
                result.fields["best_spec_class"] = (ours or None, tns_class)
                best_set = True
        result.classification = record_classification_difference(
            transient, tns_class=tns_class, previous_tns_class=previous, our_class=ours, best_class_set=best_set)

    try:
        z = float(jd.get("redshift")) if jd.get("redshift") not in (None, "") else None
    except (TypeError, ValueError):
        z = None
    if z is not None:
        # one precedence list for every writer (#436, #437): TNS fills an empty redshift or
        # refreshes its own, never an override or a value from a higher-ranked source
        if redshift_precedence.decide(transient, z, REDSHIFT_SOURCE).write:
            updates["redshift"] = z
            updates["redshift_source"] = REDSHIFT_SOURCE
            result.fields["redshift"] = (transient.redshift, z)
    return updates


def _check_upload_allowed(user: User):
    from YSE_App.common.authz import bulk_ingest_denial

    denial = bulk_ingest_denial(user)
    if denial:
        raise ResyncError("%s: %s Add it to the api_writers group." % (user.username, denial))


def _upload_data(transient: Transient, phot: Optional[dict], specs: Optional[dict], user: User):
    """New photometry and spectra through the ``add_transient`` data stage; PhotStat refreshed once."""
    from YSE_App import data_utils
    from YSE_App.services import photstat

    entry = {"name": transient.name}
    if phot:
        entry["transientphotometry"] = phot
    if specs:
        entry["transientspectra"] = specs
    with photstat.deferred_updates():
        data_utils._add_transient_data_stage({transient.name: entry}, user)


def _import_new(name: str, jd: dict, user: User, client, throttle: Throttle, *, spectra: bool,
                result: ResyncResult):
    """Import an object we lack exactly as the TNS cron does (``add_transient_payload``)."""
    import json

    from astropy import units as u
    from astropy.coordinates import SkyCoord

    from YSE_App import data_utils
    from YSE_App.services import photstat

    ra, dec = float(jd["radeg"]), float(jd["decdeg"])
    sc = SkyCoord(ra, dec, unit=u.deg)
    ebv = None
    try:
        from YSE_App.data_ingest import TNS_uploads

        ebv = "%.3f" % (float(TNS_uploads.sfd(sc)) * 0.86)
    except Exception:  # noqa: BLE001 - the dust map is optional, as in the cron
        ebv = None
    proc = _legacy_proc()
    clean = dict(jd)  # getTNSData indexes these directly
    if not isinstance(clean.get("reporting_group"), dict):
        clean["reporting_group"] = {"group_name": None}
    if not isinstance(clean.get("object_type"), dict):
        clean["object_type"] = {"name": None}
    clean.setdefault("redshift", None)
    clean.setdefault("internal_names", "")
    clean.setdefault("discoverydate", None)
    entry = proc.getTNSData(clean, name, sc, ebv)
    phot = _photometry_upload(jd)
    if phot:
        entry["transientphotometry"] = phot
        result.new_photometry = sum(len(b.get("photdata", {})) for b in phot.values() if isinstance(b, dict))
    specs = _new_spectra(jd, None, client, throttle, download=spectra, ra=ra, dec=dec, result=result)
    if specs:
        entry["transientspectra"] = specs
    doc = {name: entry, "noupdatestatus": True, "TNS": True}
    with photstat.deferred_updates():
        response = data_utils.add_transient_payload(doc, user)
    if response.status_code >= 400:
        try:
            message = json.loads(response.content).get("message", "")
        except ValueError:
            message = ""
        raise ResyncError("upload of %s refused: HTTP %d %s" % (name, response.status_code, message))
    transient, _ = resolve_transient(name)
    return transient


def _record(transient: Optional[Transient], result: ResyncResult, user: User, *, run=None, started=None):
    """Store the outcome as the transient's latest ``tns`` run (re-using a previous no-change run)."""
    service = tns_service(user)
    now = timezone.now()
    status = (ExternalServiceRun.STATUS_FAILED if result.action in ("error", "not_found")
              else ExternalServiceRun.STATUS_SUCCEEDED)
    payload = result.as_dict()
    if run is None and transient is not None and not result.changed and status == ExternalServiceRun.STATUS_SUCCEEDED:
        previous = (ExternalServiceRun.objects.filter(service=service, transient=transient)
                    .order_by("-finished_at", "-pk").first())
        if previous is not None and previous.status == status and not (previous.result or {}).get("changed"):
            run = previous
            payload["checks"] = int((previous.result or {}).get("checks") or 1) + 1
    if run is None:
        run = ExternalServiceRun(service=service, transient=transient, target_ref=result.tns_name or result.name,
                                 request_payload={"name": result.name}, created_by=user, modified_by=user)
    run.transient = transient or run.transient
    run.status = status
    run.started_at = started or now
    run.finished_at = now
    run.result = payload
    run.error = result.error[:10000] if status == ExternalServiceRun.STATUS_FAILED else ""
    run.attempts = (run.attempts or 0) + 1
    run.modified_by = user
    run.save()
    return run


def resync_object(transient_or_name: TransientOrName, *, dry_run: bool = True, client=None,
                  user: Optional[User] = None, spectra: bool = True, throttle: Optional[Throttle] = None,
                  import_missing: bool = True) -> ResyncResult:
    """Re-sync one object from TNS and return what changed (or would change, with ``dry_run``).

    ``transient_or_name`` is a Transient, its name, an alternate name or a TNS name we
    do not have yet (imported unless ``import_missing`` is False). ``spectra=False``
    skips spectrum downloads. Raises :class:`tns.TNSRateLimited` when TNS throttles us
    (nothing is recorded for this object then) and :class:`ResyncError` when it cannot
    run at all; any other TNS error is returned (and recorded) as ``action="error"``.
    """
    started = timezone.now()
    transient, tns_name = resolve_transient(transient_or_name)
    name = transient.name if transient is not None else tns_name
    result = ResyncResult(name=name, tns_name=tns_name, transient_id=getattr(transient, "pk", None),
                          dry_run=dry_run)
    if not is_tns_name(tns_name):
        result.action = "error"
        result.error = "no TNS designation"
        return result
    if user is None and not dry_run:
        user = service_user()
    if not dry_run:
        _check_upload_allowed(user)
    client = client or default_client()
    throttle = throttle or Throttle()

    throttle.wait()
    try:
        jd = client.get_object(tns_name, photometry=True, spectra=True)
    except tns.TNSRateLimited:
        raise
    except tns.TNSError as exc:
        result.action = "error"
        result.error = str(exc)[:500]
        if not dry_run and transient is not None:
            _record(transient, result, user, started=started)
        return result
    if not isinstance(jd, dict) or not str(jd.get("objname") or "").strip():
        result.action = "not_found"
        result.error = "TNS has no object %s" % tns_name
        if not dry_run and transient is not None:
            _record(transient, result, user, started=started)
        return result

    if transient is None:
        if not import_missing:
            result.action = "not_found"
            result.error = "not in YSE-PZ"
            return result
        result.action = "imported"
        if dry_run:
            phot = _photometry_upload(jd)
            result.new_photometry = sum(len(b.get("photdata", {})) for b in (phot or {}).values()
                                        if isinstance(b, dict))
            _new_spectra(jd, None, client, throttle, download=False, ra=0.0, dec=0.0, result=result)
            return result
        transient = _import_new(tns_name, jd, user, client, throttle, spectra=spectra, result=result)
        result.transient_id = getattr(transient, "pk", None)
        result.name = getattr(transient, "name", tns_name)
        _record(transient, result, user, started=started)
        return result

    updates = _field_changes(transient, jd, result)
    phot, result.new_photometry = _new_points_only(_photometry_upload(jd), _existing_points(transient))
    specs = _new_spectra(jd, transient, client, throttle, download=spectra and not dry_run,
                         ra=transient.ra, dec=transient.dec, result=result)
    if result.changed:
        result.action = "updated"
    if dry_run:
        return result

    if updates:
        for attr, value in updates.items():
            setattr(transient, attr, value)
        transient.modified_by = user
        transient.save(update_fields=list(updates) + ["modified_by", "modified_date"])
    if phot or specs:
        _upload_data(transient, phot, specs, user)
    _record(transient, result, user, started=started)
    return result


# --- selection -----------------------------------------------------------------

def _tns_named() -> Q:
    alt = AlternateTransientNames.objects.filter(name__regex=TNS_NAME_REGEX).values("transient_id")
    return Q(name__regex=TNS_NAME_REGEX) | Q(pk__in=alt)


def with_last_sync(qs):
    """Annotate ``last_tns_sync``: when the transient's latest finished ``tns`` run ended (or None)."""
    runs = (ExternalServiceRun.objects.filter(transient=OuterRef("pk"), service__slug=SERVICE_SLUG,
                                              status__in=ExternalServiceRun.FINAL_STATUSES)
            .order_by("-finished_at").values("finished_at")[:1])
    return qs.annotate(last_tns_sync=Subquery(runs))


def select_transients(*, names: Sequence[str] = (), statuses: Sequence[str] = (), since=None, until=None,
                      never_synced: bool = False, skip_synced_days: float = 0, limit: Optional[int] = None):
    """Transients with a TNS designation to re-sync, never-synced first, then oldest sync, newest discovery."""
    qs = Transient.objects.filter(_tns_named())
    if names:
        names = [n.strip() for n in names if n and n.strip()]
        alt = AlternateTransientNames.objects.filter(name__in=names).values("transient_id")
        qs = qs.filter(Q(name__in=names) | Q(pk__in=alt))
    if statuses:
        qs = qs.filter(status__name__in=list(statuses))
    if since is not None:
        qs = qs.filter(disc_date__gte=since)
    if until is not None:
        qs = qs.filter(disc_date__lt=until)
    qs = with_last_sync(qs)
    if never_synced:
        qs = qs.filter(last_tns_sync__isnull=True)
    elif skip_synced_days and skip_synced_days > 0:
        cutoff = timezone.now() - datetime.timedelta(days=float(skip_synced_days))
        qs = qs.filter(Q(last_tns_sync__isnull=True) | Q(last_tns_sync__lt=cutoff))
    qs = qs.select_related("status", "best_spec_class").order_by(
        Case(When(last_tns_sync__isnull=True, then=Value(0)), default=Value(1), output_field=IntegerField()),
        "last_tns_sync", "-disc_date", "-pk")
    return qs[: int(limit)] if limit else qs


def due_transients(*, limit: int, active_days: float, other_days: float, now=None) -> List[Transient]:
    """The sweep's batch: TNS-named transients never re-synced, or re-synced longer ago than
    ``active_days`` (active statuses) / ``other_days`` (every other status), active ones first."""
    now = now or timezone.now()
    qs = with_last_sync(Transient.objects.filter(_tns_named()))
    active = Q(status__name__in=ACTIVE_STATUSES)
    due = (Q(last_tns_sync__isnull=True)
           | (active & Q(last_tns_sync__lt=now - datetime.timedelta(days=float(active_days))))
           | (~active & Q(last_tns_sync__lt=now - datetime.timedelta(days=float(other_days)))))
    qs = qs.filter(due).select_related("status", "best_spec_class").order_by(
        Case(When(active, then=Value(0)), default=Value(1), output_field=IntegerField()),
        Case(When(last_tns_sync__isnull=True, then=Value(0)), default=Value(1), output_field=IntegerField()),
        "last_tns_sync", "-disc_date", "-pk")
    return list(qs[: int(limit)])


def tns_names_since(client, since: datetime.datetime, *, throttle: Throttle, max_pages: int = 100) -> List[str]:
    """Names of every TNS object made public since ``since``, paged (``SEARCH_PAGE_SIZE`` per page)."""
    stamp = _aware(since).astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    names, seen = [], set()
    for page in range(int(max_pages)):
        throttle.wait()
        rows = client.search_query({"ra": "", "dec": "", "radius": "", "units": "", "objname": "",
                                    "internal_name": "", "public_timestamp": stamp,
                                    "num_page": str(SEARCH_PAGE_SIZE), "page": str(page)})
        fresh = []
        for row in rows:
            objname = str(row.get("objname") or "").strip()
            if objname and objname not in seen:
                seen.add(objname)
                fresh.append(objname)
        names.extend(fresh)
        if len(rows) < SEARCH_PAGE_SIZE or not fresh:  # last page, or TNS ignored the page number
            break
    return names


def missing_names(names: Iterable[str]) -> List[str]:
    """The ``names`` that are neither a transient's name nor an alternate name."""
    names = [n for n in names if n]
    have = set(Transient.objects.filter(name__in=names).values_list("name", flat=True))
    have |= set(AlternateTransientNames.objects.filter(name__in=names).values_list("name", flat=True))
    return [n for n in names if n not in have]


def import_missing(since: datetime.datetime, *, dry_run: bool = True, client=None, user=None, spectra: bool = True,
                   throttle: Optional[Throttle] = None, limit: Optional[int] = None, until=None,
                   deadline: Optional[float] = None) -> List[ResyncResult]:
    """Import the TNS objects made public since ``since`` that we lack (gap import).

    ``until`` drops objects TNS says were discovered on or after it.
    """
    client = client or default_client()
    throttle = throttle or Throttle()
    until = _aware(until) if until is not None else None
    results = []
    for name in missing_names(tns_names_since(client, since, throttle=throttle)):
        if limit is not None and len(results) >= limit:
            break
        if deadline is not None and time.monotonic() > deadline:
            break
        if until is not None:
            # the discovery date is only known from the object itself: skip without importing
            throttle.wait()
            disc = _aware((client.get_object(name) or {}).get("discoverydate"))
            if disc is not None and disc >= until:
                continue
        results.append(resync_object(name, dry_run=dry_run, client=client, user=user, spectra=spectra,
                                     throttle=throttle))
    return results


# --- the regular job -------------------------------------------------------------

def sweep(*, limit: Optional[int] = None, active_days=None, other_days=None, gap_days=None, budget_seconds=None,
          client=None, throttle: Optional[Throttle] = None, spectra: bool = True) -> dict:
    """One regular pass: import objects made public in the last ``gap_days``, then re-sync the
    ``limit`` most overdue transients, stopping after ``budget_seconds``."""
    limit = int(limit if limit is not None else _setting("TNS_RESYNC_MAX_PER_RUN", 50))
    active_days = float(active_days if active_days is not None else _setting("TNS_RESYNC_ACTIVE_DAYS", 2))
    other_days = float(other_days if other_days is not None else _setting("TNS_RESYNC_OTHER_DAYS", 30))
    gap_days = float(gap_days if gap_days is not None else _setting("TNS_RESYNC_GAP_DAYS", 3))
    budget = float(budget_seconds if budget_seconds is not None else _setting("TNS_RESYNC_BUDGET_SECONDS", 900))
    deadline = time.monotonic() + budget
    user = service_user()
    _check_upload_allowed(user)
    client = client or default_client()
    throttle = throttle or Throttle()
    summary = {"imported": 0, "checked": 0, "updated": 0, "unchanged": 0, "failed": 0, "changes": []}

    def tally(result: ResyncResult):
        if result.action == "imported":
            summary["imported"] += 1
        elif result.action == "updated":
            summary["updated"] += 1
        elif result.action == "unchanged":
            summary["unchanged"] += 1
        else:
            summary["failed"] += 1
        if result.changed and len(summary["changes"]) < 50:
            summary["changes"].append(result.summary())

    if gap_days > 0:
        since = timezone.now() - datetime.timedelta(days=gap_days)
        for result in import_missing(since, dry_run=False, client=client, user=user, spectra=spectra,
                                     throttle=throttle, limit=limit, deadline=deadline):
            tally(result)
    for transient in due_transients(limit=limit, active_days=active_days, other_days=other_days):
        if time.monotonic() > deadline:
            summary["stopped"] = "budget"
            break
        result = resync_object(transient, dry_run=False, client=client, user=user, spectra=spectra,
                               throttle=throttle, import_missing=False)
        summary["checked"] += 1
        tally(result)
    summary["requests"] = throttle.requests
    return summary


@job(SWEEP_KIND, max_attempts=3, backoff_seconds=900)
def resync_sweep(payload=None, job=None):
    payload = payload or {}
    try:
        return sweep(limit=payload.get("limit"), active_days=payload.get("active_days"),
                     other_days=payload.get("other_days"), gap_days=payload.get("gap_days"),
                     budget_seconds=payload.get("budget_seconds"))
    except tns.TNSRateLimited as exc:
        # every object finished so far is recorded; the next attempt resumes after them
        raise JobRetry(str(exc), delay=exc.retry_after)
    except tns.TNSTransportError as exc:
        raise JobRetry(str(exc))
