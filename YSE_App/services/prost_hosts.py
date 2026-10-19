"""Prost host association, as functions (#439).

This is the host matching the TNS crons ran inline (``processTNS.getProstData`` /
``getProstHosts`` in ``YSE_App.data_ingest.TNS_uploads``); those methods now call
:func:`associate` and :func:`host_dict`, with the same priors, catalogues and scratch
handling, so the crons behave as before. :func:`match_transient` is the per-object entry
point used by the enrichment registry (``manage.py enrich --service prost``), the TNS gap
import (#438) and the follow-up enrichment (#114) for objects without a host.

``match_transient`` links the host through the upload path's
``data_utils.add_transient_host_util`` (the same Host rows, the same NED/DESI lookup queue),
but the spectroscopic redshift is written afterwards with
``redshift_precedence.apply_redshift``, so it never replaces an override or a higher-ranked value.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from shutil import rmtree
from typing import Optional

from django.conf import settings

log = logging.getLogger(__name__)

PROST_RUN_PREFIX = "prost-"
PROST_STALE_SECONDS = 24 * 3600
CATALOGS = ("glade", "decals", "panstarrs", "skymapper")


def make_prost_run_dir(base, stale_seconds=PROST_STALE_SECONDS):
    """Create and return a unique per-run scratch directory under ``base``.

    The base directory is created if missing but is never removed. Stale
    ``prost-*`` subdirectories older than ``stale_seconds`` (left by aborted
    runs) are removed; nothing else under ``base`` is touched.
    """
    os.makedirs(base, exist_ok=True)
    cutoff = time.time() - stale_seconds
    try:
        for name in os.listdir(base):
            path = os.path.join(base, name)
            if (name.startswith(PROST_RUN_PREFIX) and os.path.isdir(path)
                    and not os.path.islink(path)
                    and os.path.getmtime(path) < cutoff):
                rmtree(path, ignore_errors=True)
    except OSError:
        pass
    return tempfile.mkdtemp(dir=base, prefix=PROST_RUN_PREFIX)


def _prost_library():
    """``(associate_sample, SnRateAbsmag)`` from astro_prost, or ``(None, None)`` when not installed."""
    try:
        from astro_prost.associate import associate_sample
        from astro_prost.helpers import SnRateAbsmag
    except ImportError:
        return None, None
    return associate_sample, SnRateAbsmag


def scratch_base() -> str:
    return getattr(settings, "PROST_PATH", "") or getattr(settings, "ghost_path", "") or tempfile.gettempdir()


def associate(objs, ras_deg, decs_deg, scratch, *, transient_redshift=None, associate_fn=None, absmag_cls=None):
    """Run Prost on the named positions; returns its host table (a DataFrame), or None without astro_prost.

    The priors, likelihoods and catalogues are the TNS cron's. Each call uses its own scratch
    directory under ``scratch`` and removes only it (#617).
    """
    import pandas as pd
    from scipy.stats import gamma, halfnorm, uniform

    if associate_fn is None or absmag_cls is None:
        lib_associate, lib_absmag = _prost_library()
        associate_fn = associate_fn or lib_associate
        absmag_cls = absmag_cls or lib_absmag
    if associate_fn is None or absmag_cls is None:
        log.info("astro_prost not installed; skipping host association")
        return None

    output_dir = make_prost_run_dir(scratch)
    priors = {"offset": uniform(loc=0, scale=5), "absmag": uniform(loc=-30, scale=20)}
    likes = {"offset": gamma(a=0.75), "absmag": absmag_cls(a=-25, b=20)}
    transient_catalog = pd.DataFrame({"IAUID": list(objs), "RA": list(ras_deg), "Dec": list(decs_deg)})
    if transient_redshift is not None:
        priors["redshift"] = halfnorm(loc=0.0001, scale=0.5)
        transient_catalog["redshift"] = transient_redshift
    try:
        return associate_fn(
            transient_catalog,
            coord_cols=("RA", "Dec"),
            priors=priors,
            likes=likes,
            catalogs=list(CATALOGS),
            parallel=False,
            save=True,
            save_path=output_dir,
            progress_bar=False,
            cat_cols=False,
            verbose=0,
            best_redshift=True,
        )
    finally:
        rmtree(output_dir, ignore_errors=True)


def host_dict(prost_host) -> dict:
    """The upload ``host`` document for one Prost row (the cron's ``getProstHosts``)."""
    if prost_host["host_name"].to_numpy()[0]:
        host_name = prost_host["host_name"].to_numpy()[0]
    else:
        host_name = str(prost_host["host_objID"].to_numpy()[0])
    hostdict = {"name": host_name,
                "ra": prost_host["host_ra"].to_numpy()[0],
                "dec": prost_host["host_dec"].to_numpy()[0]}
    # a weird issue with photo-z that really seem like spec-z.  Let's trust accurate zphot.
    if "host_redshift_info" in prost_host.keys() and prost_host["host_redshift_info"].to_numpy()[0] == "SPEC":
        hostdict["redshift"] = prost_host["host_redshift_mean"].to_numpy()[0]
        hostdict["redshift_err"] = prost_host["host_redshift_std"].to_numpy()[0]
        hostdict["redshift_source"] = "Prost"  # provenance (#436, #437)
    else:
        hostdict["photo_z_internal"] = prost_host["host_redshift_mean"].to_numpy()[0]
        hostdict["photo_z_err_internal"] = prost_host["host_redshift_std"].to_numpy()[0]
    return hostdict


def enabled() -> bool:
    """Whether callers outside the registry (TNS gap import, follow-up enrichment) run Prost."""
    return bool(getattr(settings, "PROST_HOST_MATCH_ENABLED", False))


def _plain(value):
    try:
        return value.item()  # numpy scalar -> Python
    except AttributeError:
        return value


def match_transient(transient, *, dry_run: bool = False, user=None, associate_fn=None, absmag_cls=None,
                    scratch: Optional[str] = None):
    """Find and link a host for one transient without one; returns an enrichment ``Outcome``."""
    from YSE_App.services import redshift_precedence as rp
    from YSE_App.services.enrichment import ERROR, NO_MATCH, SKIPPED, UPDATED, Outcome

    if transient.host_id:
        return Outcome(SKIPPED, "has a host")
    if transient.ra is None or transient.dec is None:
        return Outcome(SKIPPED, "no position")
    hosts = associate([transient.name], [float(transient.ra)], [float(transient.dec)], scratch or scratch_base(),
                      associate_fn=associate_fn, absmag_cls=absmag_cls)
    if hosts is None:
        return Outcome(ERROR, error="astro_prost is not installed")
    row = hosts[hosts["IAUID"] == transient.name]
    if not len(row):
        return Outcome(NO_MATCH, "no Prost host")
    try:
        doc = {k: _plain(v) for k, v in host_dict(row).items()}
    except (KeyError, IndexError, TypeError) as exc:
        return Outcome(NO_MATCH, "no usable Prost host (%s)" % type(exc).__name__)
    if not doc.get("name") or doc.get("name") in ("nan", "None"):
        return Outcome(NO_MATCH, "no Prost host")
    summary = "host %s (%.5f, %.5f)" % (doc["name"], float(doc["ra"]), float(doc["dec"]))
    if dry_run:
        return Outcome(UPDATED, "would link " + summary, data=doc)

    from YSE_App import data_utils
    from YSE_App.services.enrichment import service_user

    user = user or service_user()
    redshift = {k: doc.pop(k) for k in ("redshift", "redshift_err", "redshift_source") if k in doc}
    data_utils.add_transient_host_util(doc, transient, user)
    transient.refresh_from_db(fields=["host"])
    host = transient.host
    if host is not None and redshift.get("redshift") is not None:
        decision = rp.apply_redshift(host, float(redshift["redshift"]),
                                     _float_or_none(redshift.get("redshift_err")), rp.SOURCE_PROST, user=user)
        summary += "; z %s (%s)" % (redshift["redshift"], decision.reason)
    doc.update(redshift)
    doc["host_id"] = getattr(host, "pk", None)
    return Outcome(UPDATED, "linked " + summary, data=doc)


def _float_or_none(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


def queue_match(transient, *, created_by=None):
    """Queue a Prost match for a transient without a host, when ``PROST_HOST_MATCH_ENABLED`` is on."""
    if transient is None or getattr(transient, "host_id", None) or not enabled():
        return None
    from YSE_App.services.enrichment import queue_objects

    return queue_objects("prost", [transient.pk], created_by=created_by, transient=transient)
