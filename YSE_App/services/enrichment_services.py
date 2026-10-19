"""The built-in enrichment services (#439), registered when :mod:`enrichment` first needs them.

=============  ======  =====================================================================
name           target  what it runs
=============  ======  =====================================================================
tns            trans.  ``tns_resync.resync_object`` (#438); records its own ``tns`` runs
ned / desi     host    ``redshift_catalogs.update_host`` (#436, #437); own runs
ztf-forced     trans.  ``ztf_forced.submit_forced_phot`` (#114); needs an explicit selection
prost          trans.  ``prost_hosts.match_transient``: a host for transients without one
photoz-ml      host    ``Photo_Z.run_batch``: internal ML photo-z (cron ``Photo_Z.YSE``)
sdss-photoz    host    ``SDSS_Photo_Z.run_batch``: SDSS photo-z (cron ``SDSS_Photo_Z.YSE``)
ps1-cutouts    trans.  ``PS1_cutouts.cutout_one`` (cron ``PS1_cutouts.YSE``)
gaia-lc        trans.  ``Gaia_LC.gaia_one`` (cron ``Gaia_LC.GaiaLC`` covers the last 7 days)
blast          trans.  ``blast.blast_one``: Blast host/SED annotation; off unless BLAST_ENABLED
=============  ======  =====================================================================

The wrapped services keep their own crons, sweeps and commands unchanged; here they are
only callable per object and selectable through the shared run log.
"""

from __future__ import annotations

from django.conf import settings

from YSE_App.models.external_service_models import ExternalService
from YSE_App.services.enrichment import (
    ERROR,
    NO_MATCH,
    SKIPPED,
    TARGET_HOST,
    TARGET_TRANSIENT,
    UNCHANGED,
    UPDATED,
    EnrichmentError,
    EnrichmentService,
    Outcome,
    RateLimited,
    register,
)


def _s(name, default):
    return lambda: getattr(settings, name, default)


def _transients():
    from YSE_App.models.transient_models import Transient

    return Transient.objects.all()


def _linked_hosts():
    from YSE_App.models.host_models import Host

    return Host.objects.filter(transient__isnull=False).distinct()


# --- TNS re-sync (#438) -------------------------------------------------------------

def _tns_candidates(missing_only):
    from YSE_App.services import tns_resync

    return _transients().filter(tns_resync._tns_named())


def _tns_context():
    from YSE_App.services import tns_resync

    try:
        return {"tns_client": tns_resync.default_client(), "tns_throttle": tns_resync.Throttle()}
    except tns_resync.ResyncError as exc:
        raise EnrichmentError(str(exc)) from exc


def _tns_one(transient, ctx):
    from YSE_App.services import tns_resync
    from YSE_App.sharing import tns

    try:
        result = tns_resync.resync_object(transient, dry_run=False, client=ctx.get("tns_client"),
                                          user=ctx.get("user"), throttle=ctx.get("tns_throttle"),
                                          import_missing=False)
    except tns.TNSRateLimited as exc:
        raise RateLimited(str(exc), retry_after=exc.retry_after)
    except tns_resync.ResyncError as exc:
        raise EnrichmentError(str(exc)) from exc
    action = {"updated": UPDATED, "imported": UPDATED, "unchanged": UNCHANGED}.get(result.action, ERROR)
    return Outcome(action, result.summary(), error=result.error)


# --- NED / DESI host redshifts (#436, #437) ----------------------------------------------

def _redshift_candidates(missing_only):
    qs = _linked_hosts().exclude(redshift_override=True)
    return qs.filter(redshift__isnull=True) if missing_only else qs


def _redshift_context():
    from YSE_App.services import redshift_catalogs

    return {"clients": redshift_catalogs.default_clients()}


def _redshift_runner(slug):
    def run_one(host, ctx):
        from YSE_App.services import redshift_catalogs

        results = redshift_catalogs.update_host(host, services=[slug], dry_run=False, clients=ctx.get("clients"),
                                                user=ctx.get("user"))
        if not results:
            return Outcome(SKIPPED, "%s is not enabled in REDSHIFT_CATALOG_SERVICES" % slug)
        r = results[0]
        action = {"updated": UPDATED, "unchanged": UNCHANGED, "kept": UNCHANGED, "no_match": NO_MATCH,
                  "skipped": SKIPPED}.get(r.action, ERROR)
        return Outcome(action, r.summary(), error=r.error)
    return run_one


# --- ZTF forced photometry (#114) ------------------------------------------------------

def _ztf_one(transient, ctx):
    from YSE_App.services import ztf_forced

    outcome = ztf_forced.submit_forced_phot(transient, ctx["user"])
    action = {"submitted": UPDATED, "refused": SKIPPED}.get(outcome.kind, ERROR)
    return Outcome(action, "%s (%s): %s" % (outcome.kind, outcome.via, outcome.message),
                   error=outcome.message if action == ERROR else "")


# --- Prost --------------------------------------------------------------------------------

def _prost_candidates(missing_only):
    return _transients().filter(host__isnull=True, ra__isnull=False, dec__isnull=False)


def _prost_one(transient, ctx):
    from YSE_App.services import prost_hosts

    return prost_hosts.match_transient(transient, user=ctx.get("user"))


# --- the ported crons ------------------------------------------------------------------------

def _photoz_ml_candidates(missing_only):
    from YSE_App.data_ingest import Photo_Z

    return Photo_Z.candidate_hosts() if missing_only else _linked_hosts()


def _photoz_ml_batch(hosts, ctx):
    from YSE_App.data_ingest import Photo_Z

    return Photo_Z.run_batch(hosts, ctx)


def _sdss_candidates(missing_only):
    from YSE_App.data_ingest import SDSS_Photo_Z

    return SDSS_Photo_Z.candidate_hosts() if missing_only else _linked_hosts()


def _sdss_batch(hosts, ctx):
    from YSE_App.data_ingest import SDSS_Photo_Z

    return SDSS_Photo_Z.run_batch(hosts, ctx)


def _ps1_candidates(missing_only):
    from YSE_App.data_ingest import PS1_cutouts

    return PS1_cutouts.candidate_transients(missing_only)


def _ps1_context():
    from django.contrib.auth.models import User

    from YSE_App.data_ingest import PS1_cutouts

    try:
        return {"user": PS1_cutouts.cutout_user()}  # the cron's account
    except User.DoesNotExist:
        return {}


def _ps1_one(transient, ctx):
    from YSE_App.data_ingest import PS1_cutouts

    return PS1_cutouts.cutout_one(transient, ctx)


def _gaia_candidates(missing_only):
    from YSE_App.data_ingest import Gaia_LC

    return Gaia_LC.candidate_transients(missing_only)


def _gaia_one(transient, ctx):
    from YSE_App.data_ingest import Gaia_LC

    return Gaia_LC.gaia_one(transient, ctx)


# --- Blast --------------------------------------------------------------------------------------

def _blast_candidates(missing_only):
    from YSE_App.models.annotation_models import TransientAnnotation
    from YSE_App.services import blast, tns_resync

    qs = _transients().filter(tns_resync._tns_named())
    if missing_only:
        qs = qs.exclude(pk__in=TransientAnnotation.objects.filter(origin=blast.ORIGIN).values("transient_id"))
    return qs


def _blast_context():
    from YSE_App.services import blast

    return blast.make_context()


def _blast_one(transient, ctx):
    from YSE_App.services import blast

    return blast.blast_one(transient, ctx)


# --- registry -------------------------------------------------------------------------------------

register(EnrichmentService(
    name="tns", slug="tns", title="TNS re-sync", kind=ExternalService.KIND_SHARING,
    description="Re-sync of TNS classifications, photometry and spectra (#438).",
    target=TARGET_TRANSIENT, candidates=_tns_candidates, run_one=_tns_one, make_context=_tns_context,
    records_itself=True, recheck_days=_s("TNS_RESYNC_OTHER_DAYS", 30.0), retry_hours=24.0,
))
for _slug, _title in (("ned", "NED host redshifts"), ("desi", "DESI host redshifts")):
    register(EnrichmentService(
        name=_slug, slug="%s-redshift" % _slug, title=_title,
        description="Spectroscopic host redshifts (#436, #437), under the redshift precedence list.",
        target=TARGET_HOST, candidates=_redshift_candidates, run_one=_redshift_runner(_slug),
        make_context=_redshift_context, records_itself=True,
        recheck_days=_s("REDSHIFT_CATALOG_RECHECK_DAYS", 90.0), retry_hours=24.0,
    ))
register(EnrichmentService(
    name="ztf-forced", slug="ztf_forced", title="ZTF forced photometry", kind=ExternalService.KIND_FACILITY,
    description="ZTF forced-photometry submissions (#114).",
    target=TARGET_TRANSIENT, candidates=lambda missing_only: _transients(), run_one=_ztf_one,
    records_itself=True, recheck_days=0.5, retry_hours=12.0, needs_selection=True,
))
register(EnrichmentService(
    name="prost", slug="prost-host", title="Prost host association",
    description="Prost host match for transients without a host (#439).",
    target=TARGET_TRANSIENT, candidates=_prost_candidates, run_one=_prost_one,
    recheck_days=30.0, retry_hours=24.0 * 7, max_attempts=3,
))
register(EnrichmentService(
    name="photoz-ml", slug="photoz-ml", title="Host ML photo-z (SDSS magnitudes)",
    description="Internal photo-z of hosts from SDSS DR16 magnitudes (cron Photo_Z.YSE).",
    target=TARGET_HOST, candidates=_photoz_ml_candidates, run_batch=_photoz_ml_batch,
    recheck_days=30.0, retry_hours=24.0,
))
register(EnrichmentService(
    name="sdss-photoz", slug="sdss-photoz", title="Host SDSS photo-z",
    description="SDSS DR16 Photoz of hosts (cron SDSS_Photo_Z.YSE).",
    target=TARGET_HOST, candidates=_sdss_candidates, run_batch=_sdss_batch,
    recheck_days=30.0, retry_hours=24.0,
))
register(EnrichmentService(
    name="ps1-cutouts", slug="ps1-cutouts", title="PS1 host cutouts",
    description="PS1 g,r,i,z,y stack cutouts of hosts (cron PS1_cutouts.YSE).",
    target=TARGET_TRANSIENT, candidates=_ps1_candidates, run_one=_ps1_one, make_context=_ps1_context,
    recheck_days=None, retry_hours=lambda: 24.0 * float(getattr(settings, "PS1_CUTOUT_RETRY_DAYS", 30.0)),
    max_attempts=_s("PS1_CUTOUT_MAX_ATTEMPTS", 3),
))
register(EnrichmentService(
    name="gaia-lc", slug="gaia-lc", title="Gaia alert light curves",
    description="Gaia alert photometry of transients with a Gaia name (cron Gaia_LC.GaiaLC).",
    target=TARGET_TRANSIENT, candidates=_gaia_candidates, run_one=_gaia_one,
    recheck_days=None, retry_hours=24.0, rate_limit_seconds=1.0,
))
register(EnrichmentService(
    name="blast", slug="blast", title="Blast host properties", kind=ExternalService.KIND_GENERIC,
    description="Blast host association and SED properties as the 'blast' annotation (#439).",
    target=TARGET_TRANSIENT, candidates=_blast_candidates, run_one=_blast_one, make_context=_blast_context,
    recheck_days=_s("BLAST_RECHECK_DAYS", 30.0), retry_hours=24.0, enabled_setting="BLAST_ENABLED",
))
