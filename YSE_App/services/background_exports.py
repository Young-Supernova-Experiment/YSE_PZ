"""Background-job exports with no object cap (#855).

A photometry or spectra download over the object cap (``SEARCH_BULK_PHOTOMETRY_MAX_ROWS``) of a search
result or a Selection runs as an ``exports.build`` job instead of being refused, when
``BACKGROUND_EXPORTS_ENABLED`` is on (default off: the job runner must run on the stack).

* **What is stored in the job:** where the transients come from (the search's query string, the
  Selection's slug, or the ticked ids), the data (``photometry`` / ``spectra``), the format and the
  download options as a query string. No transient data.
* **Access:** decided when the job runs, as the requesting user (``Job.created_by``): the transients
  they may list, then per point and per spectrum through the same calls as the interactive
  download (``download_views``), so the file equals that download for the same request.
* **Formats:** photometry ``csv`` (the bulk photometry zip), ``snana`` (a zip of one SNANA file per
  transient, as ``download_photometry``) and ``ecsv`` (one combined table); spectra ``csv`` (the
  spectra zip with manifest). Parquet is not offered: pyarrow is not a dependency.
* **Files:** ``default_storage`` (``MEDIA_ROOT``) under ``exports/<job id>-<random>/``. Only the requesting
  user can download (``export_views``). They are deleted ``EXPORT_RETENTION_DAYS`` (7) after they were
  written, by ``prune_expired`` (``manage.py prune_exports``; every export job also runs it first).
* **Notification:** ``services.notify.notify`` (kind ``job_result``) with a link to the user's
  exports page, when the file is ready or when the export could not be built.
"""

from __future__ import annotations

import datetime
import logging
import secrets
import shutil
import tempfile
import zipfile
from types import SimpleNamespace

from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from django.http import QueryDict
from django.urls import reverse
from django.utils import timezone

from YSE_App.jobs import JobFailed, enqueue, job

logger = logging.getLogger(__name__)

KIND = "exports.build"
EXPORT_DIR = "exports"

PHOTOMETRY = "photometry"
SPECTRA = "spectra"
FORMATS = {
    PHOTOMETRY: (("csv", "CSV (zip, one file per transient)"),
                 ("snana", "SNANA (zip, one file per transient)"),
                 ("ecsv", "One combined ECSV table")),
    SPECTRA: (("csv", "CSV (zip, one file per spectrum, with manifest)"),),
}
SOURCES = ("search", "selection", "ids")
# Orderings a payload may ask for (the search page's bulk action orders by discovery date).
ORDERINGS = {"-disc_date": ("-disc_date",)}


class ExportError(ValueError):
    """The export cannot be built; ``str(exc)`` is shown to the user."""


def enabled() -> bool:
    return bool(getattr(settings, "BACKGROUND_EXPORTS_ENABLED", False))


def retention_days() -> int:
    return max(1, int(getattr(settings, "EXPORT_RETENTION_DAYS", 7)))


def format_choices(data):
    return FORMATS[data]


def check_format(data, fmt):
    """``fmt`` when it is a format of ``data`` (empty: csv), else ``ExportError``."""
    fmt = (fmt or "csv").strip().lower()
    if fmt not in dict(FORMATS[data]):
        raise ExportError("Unknown download format %r." % fmt)
    return fmt


def option_query(params):
    """The download options of a GET request as a query string (``params`` and ``format`` left out)."""
    options = params.copy() if hasattr(params, "copy") else QueryDict("", mutable=True)
    for key in ("params", "format"):
        options.pop(key, None)
    return options.urlencode()


# ------------------------------------------------------------------ queueing


def request_export(user, *, data, fmt, source, params="", slug="", ids=None, options="", order_by=""):
    """Queue an export for ``user`` and return the ``Job``. Nothing is read until the job runs."""
    if data not in FORMATS:
        raise ExportError("Unknown export %r." % data)
    fmt = check_format(data, fmt)
    if source not in SOURCES:
        raise ExportError("Unknown export source %r." % source)
    if order_by and order_by not in ORDERINGS:
        raise ExportError("Unknown ordering %r." % order_by)
    payload = {"data": data, "format": fmt, "source": source, "params": params or "", "slug": slug or "",
               "ids": [int(i) for i in (ids or ())], "options": options or "", "order_by": order_by or ""}
    return enqueue(KIND, payload, created_by=user)


def queued_message(job):
    return ("That is more than %d transients, so the export runs in the background (job %d). You will get a "
            "notification with the download link when it is ready; finished files are kept for %d days."
            % (_cap(), job.pk, retention_days()))


def _cap():
    from YSE_App.views import SEARCH_BULK_PHOTOMETRY_MAX_ROWS

    return SEARCH_BULK_PHOTOMETRY_MAX_ROWS


# ------------------------------------------------------------------ running


def resolve_transients(user, payload):
    """The transients of the export as ``user`` may list them now, in the download's order."""
    from YSE_App.filters.transient_search import TransientSearchFilterSet, quick_search_params
    from YSE_App.models import Selection, Transient
    from YSE_App.services import selections as selection_svc
    from YSE_App.services.visibility import filter_listed_transients

    source = payload.get("source")
    if source == "search":
        params = QueryDict(payload.get("params") or "")
        filterset = TransientSearchFilterSet(quick_search_params(params), queryset=Transient.objects.all(),
                                             request=SimpleNamespace(user=user, GET=params))
        if not filterset.is_valid():
            raise ExportError("That search is no longer valid, so nothing was exported.")
        qs = filter_listed_transients(user, filterset.qs)
    elif source == "selection":
        selection = Selection.objects.filter(slug=payload.get("slug") or "").first()
        if selection is None:
            raise ExportError("That Selection no longer exists.")
        try:
            qs = selection_svc.selection_queryset(selection, user)
        except selection_svc.SelectionError as exc:
            raise ExportError(str(exc))
    elif source == "ids":
        qs = filter_listed_transients(user, Transient.objects.filter(pk__in=payload.get("ids") or []))
    else:
        raise ExportError("Unknown export source.")
    order = ORDERINGS.get(payload.get("order_by") or "")
    if order:
        qs = qs.order_by(*order)
    return list(qs)


def photometry_keep(options, transient):
    """``keep(fields)`` for the SNANA file: the same option test as the CSV rows."""
    from YSE_App.common.utilities import date_to_mjd
    from YSE_App.services.download_options import transient_reference
    from YSE_App.services.spectral_phase import phase_days

    if options is None or not options.active:
        return None
    epoch = transient_reference(transient, options.reference) if options.phase_range else None

    def keep(fields):
        mjd = date_to_mjd(fields["obs_date"])
        data_quality = ";".join(str(q) for q in (fields.get("data_quality") or []))
        phase = phase_days(mjd, epoch[0], epoch[1]) if epoch is not None else None
        return options.keeps(mjd, fields["band"].split(" - ")[-1], data_quality, phase)

    return keep


def filename_for(payload, job_id):
    ext = "ecsv" if payload.get("format") == "ecsv" else "zip"
    return "YSEPZ_export_%d_%s.%s" % (job_id, payload.get("data"), ext)


def write_export(user, transients, payload, fh):
    """Write the export file to the binary file ``fh``, with the interactive download's builders."""
    from YSE_App import download_views

    data, fmt = payload["data"], payload["format"]
    params = QueryDict(payload.get("options") or "")
    if data == PHOTOMETRY:
        from YSE_App.services.download_options import PhotometryOptions

        options = PhotometryOptions.from_params(params)
        if fmt == "ecsv":
            for chunk in download_views.photometry_ecsv_chunks(user, transients, options):
                fh.write(chunk.encode("utf-8"))
            return
        entries = download_views.photometry_zip_entries(user, transients, options, fmt)
    else:
        from YSE_App.services.download_options import SpectraOptions

        entries = download_views.spectra_zip_entries(user, transients, SpectraOptions.from_params(params))
    with zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)


def _notify(user, text, subject):
    from YSE_App.services import notify as notify_service

    notify_service.notify([user], text, url=reverse("export_list"), kind="job_result", subject=subject)


@job(KIND, max_attempts=2, backoff_seconds=300)
def build_export(payload, job=None):
    """Handler of ``exports.build``: build the file as the requesting user and notify them."""
    from YSE_App.services.download_options import OptionError

    user = getattr(job, "created_by", None) if job is not None else None
    if user is None or not user.is_active:
        raise JobFailed("export without an active requesting user")
    prune_expired()
    try:
        transients = resolve_transients(user, payload)
        if not transients:
            raise ExportError("No transients to export.")
        name = filename_for(payload, job.pk)
        with tempfile.TemporaryFile() as fh:
            write_export(user, transients, payload, fh)
            size = fh.tell()
            fh.seek(0)
            # An unguessable directory too, in case MEDIA_ROOT were ever served without a check.
            folder = "%s/%d-%s" % (EXPORT_DIR, job.pk, secrets.token_hex(8))
            stored = default_storage.save("%s/%s" % (folder, name), File(fh, name=name))
    except (ExportError, OptionError) as exc:
        _notify(user, "Your %s export (job %d) could not be built: %s" % (payload.get("data"), job.pk, exc),
                "Export failed")
        raise JobFailed(str(exc))
    now = timezone.now()
    result = {"file": stored, "filename": name, "bytes": size, "transients": len(transients),
              "data": payload.get("data"), "format": payload.get("format"), "written_at": now.isoformat(),
              "expires_at": (now + datetime.timedelta(days=retention_days())).isoformat()}
    _notify(user, "Your %s export of %d transients is ready (job %d)." % (payload.get("data"), len(transients), job.pk),
            "Export ready")
    return result


# ------------------------------------------------------------------ files and retention


def export_file(job_row):
    """The stored file name of a finished, unexpired export, or None."""
    from YSE_App.models.job_models import Job

    if job_row.kind != KIND or job_row.status != Job.DONE or not isinstance(job_row.result, dict):
        return None
    name = job_row.result.get("file") or ""
    if not name.startswith(EXPORT_DIR + "/") or ".." in name:
        return None
    if expired(job_row):
        return None
    return name if default_storage.exists(name) else None


def expired(job_row, now=None):
    now = now or timezone.now()
    finished = job_row.finished_at or job_row.updated_at
    return finished is None or finished < now - datetime.timedelta(days=retention_days())


def prune_expired(now=None, dry_run=False):
    """Delete export files older than ``EXPORT_RETENTION_DAYS``; returns how many directories went.

    Each job writes ``exports/<job id>-<random>/``; a directory goes when its job finished before the cutoff, or,
    when the job row is gone (``JOB_RETENTION_DAYS``) or never finished, when its files are older than
    the cutoff.
    """
    from YSE_App.models.job_models import Job

    now = now or timezone.now()
    cutoff = now - datetime.timedelta(days=retention_days())
    try:
        dirs, _files = default_storage.listdir(EXPORT_DIR)
    except (FileNotFoundError, NotADirectoryError):
        return 0
    removed = 0
    for name in dirs:
        job_id = name.split("-", 1)[0]
        if not job_id.isdigit():
            continue
        row = Job.objects.filter(pk=int(job_id), kind=KIND).first()
        path = "%s/%s" % (EXPORT_DIR, name)
        if row is not None and row.status == Job.DONE and row.finished_at is not None:
            old = row.finished_at < cutoff
        elif row is not None and row.status in Job.ACTIVE_STATUSES:
            old = False
        else:
            _subdirs, files = default_storage.listdir(path)
            times = [default_storage.get_modified_time("%s/%s" % (path, f)) for f in files]
            old = all(t < cutoff for t in times)
        if not old:
            continue
        removed += 1
        if dry_run:
            continue
        _remove_dir(path)
    return removed


def _remove_dir(path):
    try:
        local = default_storage.path(path)
    except NotImplementedError:
        local = None
    if local is not None:
        shutil.rmtree(local, ignore_errors=True)
        return
    _subdirs, files = default_storage.listdir(path)
    for f in files:
        default_storage.delete("%s/%s" % (path, f))


def user_exports(user, limit=50):
    """The user's export jobs, newest first, each with ``download_name`` (None: not downloadable)."""
    from YSE_App.models.job_models import Job

    rows = list(Job.objects.filter(kind=KIND, created_by=user).order_by("-id")[:limit])
    for row in rows:
        row.download_name = export_file(row)
        row.export_payload = row.payload if isinstance(row.payload, dict) else {}
    return rows
