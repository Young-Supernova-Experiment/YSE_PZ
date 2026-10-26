"""Display filters used on every page (registered as template builtins in settings).

Dates (#807, see ``YSE_App.common.utc_dates``):
  ``{{ value|utcdate }}`` -> ``<span class="yse-utcdate" title="MJD 61337.362">20261024.362</span>``
  ``{{ value|utcdate_text }}`` -> ``20261024.362`` (attributes, plain text)
  ``{{ value|utcnight }}`` -> ``20261024`` (night labels, date bounds; plain text)
  Date-only values (dates, ``'YYYY-MM-DD'``) print as plain ``YYYYMMDD``.
  All give ``""`` for None, so ``|default:"-"`` still works after them.

Transient statuses (#831, see ``YSE_App.common.status_display``):
  ``{{ transient.status|status_label }}`` -> ``Follow-up requested``
  ``{{ transient.status|status_title }}`` -> its one-line definition (for ``title=``)
"""

from django import template

from YSE_App.common.status_display import status_definition, status_label as _status_label
from YSE_App.common.utc_dates import utc_day, utc_day_html, utc_night

register = template.Library()


@register.filter(name='utcdate')
def utcdate(value):
    return utc_day_html(value)


@register.filter(name='utcdate_text')
def utcdate_text(value):
    return utc_day(value)


@register.filter(name='utcnight')
def utcnight(value):
    return utc_night(value)


@register.filter(name='status_label')
def status_label(value):
    return _status_label(value)


@register.filter(name='status_title')
def status_title(value):
    return status_definition(value)
