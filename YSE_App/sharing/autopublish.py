"""``AutoPublisher`` evaluation: on transient saves and in a periodic sweep (#325, #326).

A rule fires once per (service, transient, kind): any existing submission,
whatever its status, blocks another; a failed or rejected one is retried from
the submissions page (same payload), not re-created by the rule. Payload problems (no
detection from an allowed instrument, unmapped filter) are logged and skipped
so one transient cannot stop the sweep.
"""

from __future__ import annotations

import logging
import time

from django.conf import settings
from django.utils import timezone

from YSE_App.jobs import job
from YSE_App.models.sharing_models import AutoPublisher, SharingSubmission
from YSE_App.sharing import tns

log = logging.getLogger(__name__)

SWEEP_KIND = "sharing.autopublish_sweep"

_cache = {"checked": 0.0, "active": False}
CACHE_SECONDS = 60.0


def active_rules():
    return (AutoPublisher.objects.filter(enabled=True, tns_enabled=True, service__enabled=True,
                                         service__kind="tns")
            .select_related("service", "group").order_by("pk"))


def rules_active(force: bool = False) -> bool:
    """Cheap gate for the post_save hook: any enabled rule at all (cached for a minute)."""
    now = time.monotonic()
    if force or now - _cache["checked"] > CACHE_SECONDS:
        _cache["active"] = active_rules().exists()
        _cache["checked"] = now
    return _cache["active"]


def reset_cache():
    _cache["checked"] = 0.0
    _cache["active"] = False


def already_submitted(rule: AutoPublisher, transient) -> bool:
    return SharingSubmission.objects.filter(service=rule.service, transient=transient, kind=rule.kind).exists()


def apply_rule(rule: AutoPublisher, transient, *, dispatch: bool = True):
    """Queue the rule's report for ``transient`` when it qualifies; returns the submission or None."""
    if not rule.matches(transient) or already_submitted(rule, transient):
        return None
    options = {"coauthors": rule.coauthors, "remarks": rule.remarks}
    if rule.kind == SharingSubmission.KIND_CLASSIFICATION:
        spectrum = transient.best_spectrum or transient.transientspectrum_set.order_by("-obs_date").first()
        options["spectrum"] = spectrum
    try:
        return tns.create_submission(rule.service, transient, rule.kind, None, auto_publisher=rule,
                                     dispatch=dispatch, **options)
    except tns.PayloadError as exc:
        log.info("auto-publisher %s skipped %s: %s", rule.name, transient.name, exc)
    except tns.SharingError as exc:
        log.warning("auto-publisher %s could not queue %s: %s", rule.name, transient.name, exc)
    return None


def evaluate_transient(transient, *, dispatch: bool = True):
    """Run every active rule against one transient (the post_save hook)."""
    created = []
    for rule in active_rules():
        submission = apply_rule(rule, transient, dispatch=dispatch)
        if submission is not None:
            created.append(submission)
    return created


def on_transient_saved(sender, instance, created=False, raw=False, **kwargs):
    """post_save receiver for ``Transient`` (connected in ``YSE_App.signals``)."""
    if raw or not getattr(settings, "SHARING_AUTOPUBLISH_ON_SAVE", True):
        return
    if not rules_active():
        return
    try:
        evaluate_transient(instance)
    except Exception:  # noqa: BLE001 - never let a rule break a save
        log.exception("auto-publisher evaluation failed for %s", getattr(instance, "name", instance))


def sweep(limit_per_rule: int = 200, *, dispatch: bool = True) -> dict:
    """Every active rule against its qualifying transients; returns counts per rule."""
    summary = {"rules": 0, "queued": 0, "per_rule": {}}
    for rule in active_rules():
        queued = 0
        for transient in rule.qualifying_transients(limit=limit_per_rule):
            if apply_rule(rule, transient, dispatch=dispatch) is not None:
                queued += 1
        rule.last_run_at = timezone.now()
        rule.save(update_fields=["last_run_at", "modified_date"])
        summary["rules"] += 1
        summary["queued"] += queued
        summary["per_rule"][rule.name] = queued
    return summary


@job(SWEEP_KIND, max_attempts=1)
def sweep_job(payload, job=None):
    payload = payload or {}
    return sweep(int(payload.get("limit_per_rule", 200)))
