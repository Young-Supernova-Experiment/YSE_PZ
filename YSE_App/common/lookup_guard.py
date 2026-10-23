"""Allowlist checks for ORM keys built at run time (#540).

Code that builds a ``filter(**{key: value})``, ``exclude(**...)``, ``Q(**...)``
or ``order_by(key)`` from a name it did not write literally passes the key
through one of these helpers first. A key is accepted only when its field
path is on the caller's allowlist (relation hops included, so a hop must be
listed explicitly) and its trailing lookup, if any, is on the allowed
lookups. Keys with a part that starts with ``_`` or is empty are refused
whatever the allowlist says.
"""

from typing import Iterable, Optional

LOOKUP_SEP = '__'


class DisallowedLookup(ValueError):
    """A run-time ORM key that is not on the allowlist."""


def _parts_ok(parts):
    return all(part and not part.startswith('_') for part in parts)


def checked_lookup(key: str, fields: Iterable[str], lookups: Iterable[str] = ()) -> str:
    """Return ``key`` if it is ``<field>`` or ``<field>__<lookup>`` for an allowed field and lookup.

    ``fields`` are full field paths (``'has_hst'``, ``'antares_classification__name'``).
    ``lookups`` are the lookup names allowed after the field (``'gte'``, ``'isnull'``);
    a bare field (exact match) is always allowed. Raises ``DisallowedLookup`` otherwise.
    """
    if not isinstance(key, str) or not key:
        raise DisallowedLookup('lookup key must be a non-empty string')
    parts = key.split(LOOKUP_SEP)
    if not _parts_ok(parts):
        raise DisallowedLookup('lookup key %r is not allowed' % (key,))
    fields = set(fields)
    if key in fields:
        return key
    field, _sep, lookup = key.rpartition(LOOKUP_SEP)
    if field in fields and lookup in set(lookups):
        return key
    raise DisallowedLookup('lookup key %r is not allowed' % (key,))


def checked_ordering(value: str, fields: Iterable[str]) -> str:
    """Return ``value`` (``'field'`` or ``'-field'``) if the field path is allowed; raise otherwise."""
    if not isinstance(value, str) or not value:
        raise DisallowedLookup('ordering must be a non-empty string')
    name = value[1:] if value.startswith('-') else value
    if not _parts_ok(name.split(LOOKUP_SEP)) or name not in set(fields):
        raise DisallowedLookup('ordering %r is not allowed' % (value,))
    return value


def is_allowed_lookup(key: str, fields: Iterable[str], lookups: Iterable[str] = ()) -> bool:
    try:
        checked_lookup(key, fields, lookups)
    except DisallowedLookup:
        return False
    return True


def is_allowed_ordering(value: str, fields: Iterable[str]) -> bool:
    try:
        checked_ordering(value, fields)
    except DisallowedLookup:
        return False
    return True


def positive_int_or_none(raw) -> Optional[int]:
    """``int(raw)`` for a string of digits (an id from a query string), else ``None``."""
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw if raw > 0 else None
    if isinstance(raw, str) and raw.isdigit() and raw.isascii():
        value = int(raw)
        return value if value > 0 else None
    return None
