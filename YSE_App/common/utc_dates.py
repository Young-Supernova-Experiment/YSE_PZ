"""One date format for people (#807): UTC as ``YYYYMMDD.ddd`` with the MJD on hover.

``YYYYMMDD`` is the UTC calendar day and ``.ddd`` the fraction of that UTC day to
three decimals (one unit is 86.4 s), e.g. ``20261024.362``. The same instant as
an MJD is ``61337.362``. Both strings come from one rounding, so the day part and
the MJD always agree (an instant that rounds up to ``.000`` of the next day shows
the next day in both).

Accepted values: aware datetimes (converted to UTC), naive datetimes (taken as
UTC, the site's ``TIME_ZONE``), dates (midnight UTC), ISO strings Django can
parse, and None. Anything else gives an empty string.

* :func:`utc_day` -> ``"20261024.362"`` (plain text: CSV, emails, plot hovers, titles)
* :func:`mjd_string` -> ``"61337.362"``
* :func:`utc_day_html` -> ``<span class="yse-utcdate" title="MJD 61337.362">20261024.362</span>``

The template filters ``utcdate`` / ``utcdate_text`` (``{% load date_extras %}``)
wrap these. MJD columns stay MJD; this is for every other date shown to people.
"""

from __future__ import annotations

import datetime
from typing import Optional, Tuple

from django.utils.html import format_html

UTC = datetime.timezone.utc
MJD_EPOCH = datetime.datetime(1858, 11, 17, tzinfo=UTC)
_US_PER_UNIT = 86_400_000  # microseconds in 1/1000 of a day

#: Column header for these dates.
DATE_HEADER = 'Date (UTC)'


def as_utc_datetime(value) -> Optional[datetime.datetime]:
    """``value`` as an aware UTC datetime, or None when it is not a date/time."""
    if value is None or value == '':
        return None
    if isinstance(value, str):
        from django.utils.dateparse import parse_date, parse_datetime

        text = value.strip()
        try:
            parsed = parse_datetime(text) or parse_date(text)
        except ValueError:
            return None
        if parsed is None:
            return None
        value = parsed
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day, tzinfo=UTC)
    return None


def _day_and_thousandths(value) -> Optional[Tuple[int, int]]:
    """(whole MJD, thousandths of a day), rounded half up, or None."""
    dt = as_utc_datetime(value)
    if dt is None:
        return None
    delta = dt - MJD_EPOCH
    microseconds = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    units = (microseconds + _US_PER_UNIT // 2) // _US_PER_UNIT
    return units // 1000, units % 1000


def utc_day(value) -> str:
    """``"YYYYMMDD.ddd"`` in UTC, or ``""`` for None / unparseable values."""
    parts = _day_and_thousandths(value)
    if parts is None:
        return ''
    day, frac = parts
    date = (MJD_EPOCH + datetime.timedelta(days=day)).date()
    return '%04d%02d%02d.%03d' % (date.year, date.month, date.day, frac)


def mjd_string(value) -> str:
    """The same instant as an MJD to three decimals (``"61337.362"``), or ``""``."""
    parts = _day_and_thousandths(value)
    if parts is None:
        return ''
    day, frac = parts
    return '%d.%03d' % (day, frac)


def utc_day_html(value) -> str:
    """``utc_day`` in a span whose tooltip is the MJD; ``""`` for None."""
    text = utc_day(value)
    if not text:
        return ''
    return format_html('<span class="yse-utcdate" title="MJD {}">{}</span>', mjd_string(value), text)
