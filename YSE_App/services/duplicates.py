"""Duplicate transients: detect and report; merge only when a person confirms (#752, #753, #754).

Two kinds of candidate pair, both found by the ``CheckDuplicates`` cron
(``YSE_App.data_ingest.QUB_data``):

* **position**: a recently modified transient whose name starts with ``1`` (an
  internal survey name) lies within about 1.5 arcsec and a year of another
  transient. The other one is kept.
* **same name**: a transient whose name differs from its slug because another
  transient already holds that name as its slug (``AutoSlugField`` appended a
  suffix). The one whose slug is the name is kept.

By default nothing is changed: :func:`report` sends each new pair once to the
review recipients through the notification queue (``services.notify``) and the
cron log lists every pair. A person then merges a pair with
``manage.py transient_duplicates merge <duplicate> <keep> --yes`` (dry run
without ``--yes``). ``DUPLICATE_AUTO_MERGE = True`` restores the old automatic
merge, now done by :func:`merge`.

:func:`merge` is one ``transaction.atomic`` block: photometry, spectra,
alternate names and tags move to the kept transient, the duplicate's name is
kept as an alternate name, and only then is the duplicate deleted. Rows of
other kinds that point at the duplicate (comments, follow-ups, ...) are deleted
or unlinked with it; the dry run lists their counts so the reviewer can move them first.

The upload path (``data_utils.add_transient_payload``) never deletes: when a
name matches several transients it updates the oldest and reports the conflict
here (:func:`report_same_name_conflict`).
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Dict, Iterable, List

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from YSE_App.common.utilities import getRADecBox

logger = logging.getLogger(__name__)

SUBJECT = "Possible duplicate transient"
FAILURE_SUBJECT = "Duplicate check failed"
KIND = "system"
POSITION = "position"
SAME_NAME = "same name"
BOX_SIZE_DEG = 0.00042  # about 1.5 arcsec, as before
DISC_WINDOW = datetime.timedelta(days=365)
RECENT = datetime.timedelta(days=1)
# Relations merge() moves itself, and rows that are rebuilt or are only bookkeeping.
MOVED_RELATIONS = frozenset({"transientphotometry", "transientspectrum", "alternatetransientnames"})


@dataclass(frozen=True)
class DuplicatePair:
    duplicate: object  # the Transient that a merge would remove
    keep: object  # the Transient that a merge would keep
    reason: str

    def describe(self) -> str:
        return "%s (id %s) looks like a duplicate of %s (id %s) [%s]" % (
            self.duplicate.name, self.duplicate.pk, self.keep.name, self.keep.pk, self.reason)


def auto_merge_enabled() -> bool:
    return bool(getattr(settings, "DUPLICATE_AUTO_MERGE", False))


def position_candidates(now=None) -> List[DuplicatePair]:
    """Recently modified ``1...`` transients with another transient at the same place and time."""
    from YSE_App.models import Transient

    now = now or timezone.now()
    recent = (Transient.objects.filter(modified_date__gt=now - RECENT, name__startswith="1")
              .exclude(disc_date=None).order_by("pk"))
    pairs, seen = [], set()
    for t in recent:
        ramin, ramax, decmin, decmax = getRADecBox(t.ra, t.dec, size=BOX_SIZE_DEG)
        others = list(
            Transient.objects.filter(
                Q(ra__gt=ramin) & Q(ra__lt=ramax) & Q(dec__gt=decmin) & Q(dec__lt=decmax)
                & Q(disc_date__gte=t.disc_date - DISC_WINDOW) & Q(disc_date__lte=t.disc_date + DISC_WINDOW))
            .exclude(pk=t.pk).order_by("pk"))
        if not others:
            continue
        named = [o for o in others if not o.name.startswith("1")]
        if named:
            keep, duplicate = named[0], t
        else:  # only other internal names: keep the oldest
            keep, duplicate = (t, others[0]) if t.pk < others[0].pk else (others[0], t)
        key = (duplicate.pk, keep.pk)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(DuplicatePair(duplicate, keep, POSITION))
    return pairs


def same_name_candidates() -> List[DuplicatePair]:
    """Transients whose name is another transient's slug."""
    from YSE_App.models import Transient

    pairs = []
    for t in Transient.objects.filter(~Q(name=F("slug"))).exclude(slug=None).order_by("pk"):
        keep = Transient.objects.filter(slug=t.name).exclude(pk=t.pk).first()
        if keep is not None:
            pairs.append(DuplicatePair(t, keep, SAME_NAME))
    return pairs


def find_candidates(now=None) -> List[DuplicatePair]:
    pairs = position_candidates(now)
    taken = {p.duplicate.pk for p in pairs}
    return pairs + [p for p in same_name_candidates() if p.duplicate.pk not in taken]


def review_recipients() -> List[User]:
    """``DUPLICATE_REPORT_USERNAMES`` (list or comma-separated), else the active superusers."""
    names = getattr(settings, "DUPLICATE_REPORT_USERNAMES", None) or []
    if isinstance(names, str):
        names = [n.strip() for n in names.split(",") if n.strip()]
    qs = User.objects.filter(is_active=True)
    qs = qs.filter(username__in=list(names)) if names else qs.filter(is_superuser=True)
    return list(qs.order_by("pk"))


def _detail_url(transient) -> str:
    return "/transient_detail/%s/" % transient.slug if transient.slug else ""


def _send_once(text: str, subject: str, url: str = "", transient=None, recipients=None) -> int:
    """Notify each recipient who has not had this exact text yet; returns rows created."""
    from YSE_App.models.notification_models import Notification
    from YSE_App.services.notify import notify

    users = [u for u in (review_recipients() if recipients is None else recipients)
             if not Notification.objects.filter(recipient=u, kind=KIND, subject=subject, text=text).exists()]
    if not users:
        return 0
    try:
        return len(notify(users, text, url, KIND, subject=subject, transient=transient))
    except Exception:  # a report must never break the cron or an upload
        logger.exception("duplicate report could not be queued")
        return 0


def report(pairs: Iterable[DuplicatePair], *, recipients=None) -> int:
    """Tell the reviewers about each pair once; returns notifications created."""
    created = 0
    for pair in pairs:
        logger.warning("duplicate candidate: %s", pair.describe())
        text = ("%s. Nothing was changed. To merge after checking both pages: "
                "manage.py transient_duplicates merge %s %s --yes" % (
                    pair.describe(), pair.duplicate.pk, pair.keep.pk))
        created += _send_once(text, SUBJECT, _detail_url(pair.keep), pair.keep, recipients)
    return created


def report_same_name_conflict(name: str, kept, others: Iterable) -> int:
    """Upload path: several transients share ``name``; ``kept`` was updated, ``others`` left alone."""
    created = 0
    for other in others:
        created += report([DuplicatePair(other, kept, SAME_NAME + " (upload)")])
    return created


def report_failure(exc: BaseException) -> int:
    logger.error("duplicate check failed: %s", exc, exc_info=exc)
    day = timezone.now().date().isoformat()
    text = "The CheckDuplicates cron failed on %s: %s: %s" % (day, type(exc).__name__, str(exc)[:500])
    return _send_once(text, FAILURE_SUBJECT)


def unmoved_related_counts(transient) -> Dict[str, int]:
    """Rows of kinds :func:`merge` does not move that point at ``transient`` (deleted or unlinked with it)."""
    counts = {}
    for rel in transient._meta.related_objects:
        if rel.related_model._meta.model_name in MOVED_RELATIONS:
            continue
        n = rel.related_model._base_manager.filter(**{rel.field.name: transient}).count()
        if n:
            counts[rel.related_model._meta.label] = n
    return counts


def merge(duplicate, keep, *, actor=None) -> Dict[str, int]:
    """Move ``duplicate``'s data to ``keep`` and delete ``duplicate``, all or nothing."""
    from YSE_App.models import AlternateTransientNames, TransientPhotometry, TransientSpectrum
    from YSE_App.services import photstat

    if duplicate.pk == keep.pk:
        raise ValueError("cannot merge a transient into itself")
    actor_id = getattr(actor, "pk", None) or keep.modified_by_id or keep.created_by_id
    counts = {}
    with photstat.deferred_updates():  # both transients' stat rows refresh once, after the block
        with transaction.atomic():
            phot = list(TransientPhotometry.objects.select_for_update().filter(transient=duplicate))
            for tp in phot:  # save() per row so the photstat move signals run (#269)
                tp.transient = keep
                tp.save()
            counts["photometry"] = len(phot)
            spectra = list(TransientSpectrum.objects.filter(transient=duplicate))
            for ts in spectra:
                ts.transient = keep
                ts.save()
            counts["spectra"] = len(spectra)
            counts["alternate_names"] = AlternateTransientNames.objects.filter(
                transient=duplicate).update(transient=keep)
            tags = list(duplicate.tags.all())
            if tags:
                keep.tags.add(*tags)
            counts["tags"] = len(tags)
            if duplicate.name != keep.name and not AlternateTransientNames.objects.filter(
                    transient=keep, name=duplicate.name).exists():
                AlternateTransientNames.objects.create(
                    transient=keep, obs_group=duplicate.obs_group, name=duplicate.name,
                    created_by_id=actor_id, modified_by_id=actor_id)
            name, pk = duplicate.name, duplicate.pk
            duplicate.delete()
    logger.warning("merged transient %s (id %s) into %s (id %s): %s", name, pk, keep.name, keep.pk, counts)
    return counts


def run_check(now=None) -> Dict[str, int]:
    """The cron body: find pairs, report them, merge only when ``DUPLICATE_AUTO_MERGE`` is on."""
    pairs = find_candidates(now)
    result = {"candidates": len(pairs), "reported": 0, "merged": 0, "failed": 0}
    if not auto_merge_enabled():
        result["reported"] = report(pairs)
        return result
    from YSE_App.models import Transient

    for pair in pairs:
        if Transient.objects.filter(pk__in=(pair.duplicate.pk, pair.keep.pk)).count() != 2:
            continue  # one side went in an earlier merge of this run
        try:
            merge(pair.duplicate, pair.keep)
            result["merged"] += 1
        except Exception as exc:  # one bad pair is rolled back; the rest still run
            result["failed"] += 1
            logger.exception("could not merge %s", pair.describe())
            report_failure(exc)
    return result
