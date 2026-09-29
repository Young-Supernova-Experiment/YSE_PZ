"""Built-in annotation checks (#318) and their runner registration.

Importing this package registers one runner per built-in slug with
:func:`YSE_App.services.external_services.register_runner`, so a queued
``external_service.run`` job for a service whose slug is ``gaia_dr3``,
``wise`` or ``quasar`` executes the matching check and writes the
annotation. A service of kind ``annotation`` with another slug is handled by
``kind:annotation`` only when a ``check`` is registered for it through
:func:`register_check`.
"""

from __future__ import annotations

from typing import Callable, Dict

from YSE_App.annotation_services import base, gaia, quasar, wise
from YSE_App.annotation_services.base import AnnotationCheckError, CheckResult, execute_check
from YSE_App.services import external_services as runs

# slug -> check(ra, dec, radius_arcsec) -> (data, verdict, summary)
CHECKS: Dict[str, Callable[[float, float, float], CheckResult]] = {
    gaia.SLUG: gaia.check,
    wise.SLUG: wise.check,
    quasar.SLUG: quasar.check,
}


def register_check(slug: str, check: Callable[[float, float, float], CheckResult]) -> None:
    """Make ``check`` run for annotation services whose slug is ``slug``."""
    CHECKS[slug] = check
    runs.register_runner(slug, _runner_for(slug))


def _runner_for(slug: str):
    def _run(run):
        return execute_check(run, CHECKS[slug], origin=slug)

    _run.__name__ = "run_%s" % slug.replace("-", "_")
    return _run


def run_annotation_service(run):
    """``kind:annotation`` runner: dispatch on the slug; unknown slugs fail the run with a clear message."""
    check = CHECKS.get(run.service.slug)
    if check is None:
        from YSE_App.models.external_service_models import ExternalServiceRun

        run.mark_running()
        error = "no annotation check is registered for service %r" % run.service.slug
        runs.record_completion(run, ExternalServiceRun.STATUS_FAILED, error=error)
        return {"status": run.status, "error": error}
    return execute_check(run, check, origin=run.service.slug)


for _slug in list(CHECKS):
    runs.register_runner(_slug, _runner_for(_slug))
runs.register_runner("kind:annotation", run_annotation_service)

__all__ = ["AnnotationCheckError", "CHECKS", "base", "execute_check", "gaia", "quasar", "register_check",
           "run_annotation_service", "wise"]
