"""Which classical nights the transient follow-up form offers, and in what order.

A ``ClassicalResource`` created from the transient page is one observing
night: ``begin_date_valid`` is the night's date (midnight UTC, because
``TIME_ZONE = 'UTC'`` and the form posts a date), ``end_date_valid`` is one
day later, and one ``ClassicalObservingDate`` carries the same ``obs_date``.
Older resources may span a semester and hold several observing dates.

Rules implemented here (issue: classical follow-up night pulldown):

* A night is over "the morning after the run, in the time zone of the
  observatory": the night whose calendar date is ``D`` disappears from the
  pulldown at ``D+1 06:00`` local time at the resource's observatory
  (``NIGHT_ENDS_LOCAL_TIME``). The codebase has no earlier convention for
  "morning"; 06:00 is after astronomical twilight at every YSE site.
* A resource stays listed while any of its nights is still ahead of that
  cutoff. The night before ``end_date_valid`` always counts as one of its
  nights (the create form sets ``end_date_valid = night + 1 day``; a
  semester-long resource with few or no ``ClassicalObservingDate`` rows is
  therefore still offered until its validity window closes).
* The list is sorted by each resource's next remaining night, soonest first,
  so the first entry is the default the form pre-selects.

The observatory's time zone comes from ``Observatory.tz_name`` when pytz
knows it, else from the integer ``Observatory.utc_offset`` as a fixed offset,
else UTC.
"""

from __future__ import annotations

import datetime
from typing import List, Optional

import pytz
from django.db.models import Case, IntegerField, Q, QuerySet, When
from django.utils import timezone

# Import model modules directly (see services/followup_requests.py for why the
# ``YSE_App.models`` package namespace cannot be used from services).
from YSE_App.models.observatory_models import Observatory
from YSE_App.models.telescope_resource_models import (
    ClassicalObservingDate,
    ClassicalResource,
)

#: Local time at the observatory at which a night's date drops off the pulldown
#: (on the calendar day after the night).
NIGHT_ENDS_LOCAL_TIME = datetime.time(6, 0)

# Widest UTC offsets in use anywhere (UTC-12 .. UTC+14), padded, so the
# database pre-filter never drops a night that the exact rule would keep.
_COARSE_GRACE = datetime.timedelta(days=3)


def observatory_timezone(observatory: Optional[Observatory]) -> datetime.tzinfo:
    """tzinfo for ``observatory``: ``tz_name`` via pytz, else ``utc_offset``, else UTC."""
    if observatory is None:
        return datetime.timezone.utc
    tz_name = (observatory.tz_name or "").strip()
    if tz_name:
        try:
            return pytz.timezone(tz_name)
        except pytz.UnknownTimeZoneError:
            pass
    offset_hours = observatory.utc_offset
    if offset_hours is not None:
        try:
            return datetime.timezone(datetime.timedelta(hours=int(offset_hours)))
        except (TypeError, ValueError):
            pass
    return datetime.timezone.utc


def _localize(tz: datetime.tzinfo, naive: datetime.datetime) -> datetime.datetime:
    localize = getattr(tz, "localize", None)
    if localize is not None:  # pytz zones
        return localize(naive)
    return naive.replace(tzinfo=tz)


def night_cutoff(night_date: datetime.date, tz: datetime.tzinfo) -> datetime.datetime:
    """UTC instant at which the night dated ``night_date`` leaves the pulldown.

    That is ``NIGHT_ENDS_LOCAL_TIME`` on the morning after, local to ``tz``.
    """
    local_morning = datetime.datetime.combine(
        night_date + datetime.timedelta(days=1), NIGHT_ENDS_LOCAL_TIME
    )
    return _localize(tz, local_morning).astimezone(datetime.timezone.utc)


def _utc_date(value: datetime.datetime) -> datetime.date:
    if timezone.is_aware(value):
        value = value.astimezone(datetime.timezone.utc)
    return value.date()


def resource_night_dates(resource: ClassicalResource) -> List[datetime.date]:
    """Calendar dates (UTC) of the resource's nights, ascending.

    Every ``ClassicalObservingDate`` (prefetched ``classicalobservingdate_set``)
    plus the night before ``end_date_valid``, which is the last night the
    resource can be requested for.
    """
    dates = {_utc_date(night.obs_date) for night in resource.classicalobservingdate_set.all()}
    dates.add(_utc_date(resource.end_date_valid - datetime.timedelta(days=1)))
    return sorted(dates)


def resource_next_night(
    resource: ClassicalResource, now: Optional[datetime.datetime] = None
) -> Optional[datetime.date]:
    """The resource's soonest night that has not passed its local-morning cutoff."""
    if now is None:
        now = timezone.now()
    tz = observatory_timezone(resource.telescope.observatory)
    for night_date in resource_night_dates(resource):
        if now < night_cutoff(night_date, tz):
            return night_date
    return None


def upcoming_classical_resources(
    resources: QuerySet, now: Optional[datetime.datetime] = None
) -> QuerySet:
    """Restrict ``resources`` to those with a night still ahead; order soonest first.

    Returns a queryset (so it can back a ``ModelChoiceField``) ordered by each
    resource's next remaining night, then telescope name, then pk.
    """
    if now is None:
        now = timezone.now()
    coarse = now - _COARSE_GRACE
    still_relevant = ClassicalObservingDate.objects.filter(obs_date__gt=coarse).values(
        "resource_id"
    )
    candidates = (
        resources.filter(Q(end_date_valid__gt=coarse) | Q(pk__in=still_relevant))
        .select_related("telescope__observatory")
        .prefetch_related("classicalobservingdate_set")
    )

    ranked = []
    for resource in candidates:
        next_night = resource_next_night(resource, now=now)
        if next_night is not None:
            ranked.append((next_night, resource.telescope.name or "", resource.pk))
    ranked.sort()
    if not ranked:
        return resources.none()

    ordering = Case(
        *[When(pk=pk, then=position) for position, (_night, _name, pk) in enumerate(ranked)],
        output_field=IntegerField(),
    )
    kept_pks = [pk for _night, _name, pk in ranked]
    # A fresh queryset over the kept pks: the authorized queryset carries
    # ``.distinct()`` plus a groups join, which MySQL rejects together with an
    # ORDER BY expression that is not a selected column.
    return ClassicalResource.objects.filter(pk__in=kept_pks).order_by(ordering)

