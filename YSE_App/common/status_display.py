"""One display map for transient status names (#831).

The database keeps the CamelCase names (``FollowupRequested``); people see the
label (``Follow-up requested``) with a one-line definition on hover. Use
:func:`status_label` / :func:`status_definition` (or the ``status_label`` /
``status_title`` template filters) wherever a status is shown. Lookups ignore
case and also accept a label, so ``status_definition("follow-up requested")``
works. Names not in the map show unchanged with no definition.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

#: name -> (label, definition)
TRANSIENT_STATUS_DISPLAY: Dict[str, Tuple[str, str]] = {
    'New': ('New', 'Not reviewed yet.'),
    'FollowupRequested': ('Follow-up requested', 'Someone asked for follow-up observations.'),
    'Following': ('Following', 'Follow-up observations are under way.'),
    'Interesting': ('Interesting', 'Worth a closer look; no follow-up yet.'),
    'Watch': ('Watch', 'Keep an eye on it; no follow-up yet.'),
    'FollowupFinished': ('Follow-up finished', 'Follow-up observations are done.'),
    'NeedsTemplate': ('Needs template', 'Waiting for a template image after the transient fades.'),
    'Ignore': ('Ignore', 'Not of interest; left off the status lists.'),
    'StatusDeleted': ('Status deleted', 'Its status was removed; pick a new one.'),
}

_LOOKUP: Dict[str, Tuple[str, str]] = {}
for _name, _entry in TRANSIENT_STATUS_DISPLAY.items():
    _LOOKUP[_name.lower()] = _entry
    _LOOKUP[_entry[0].lower()] = _entry


def _entry(status) -> Optional[Tuple[str, str]]:
    if status is None:
        return None
    name = getattr(status, 'name', status)
    return _LOOKUP.get(str(name).strip().lower())


def status_label(status) -> str:
    """The label people see for a status name (or ``TransientStatus``)."""
    if status is None:
        return ''
    entry = _entry(status)
    return entry[0] if entry else str(getattr(status, 'name', status))


def status_definition(status) -> str:
    """The one-line definition shown on hover, or ``""``."""
    entry = _entry(status)
    return entry[1] if entry else ''
