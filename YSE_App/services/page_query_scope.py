"""Which tables a saved Explorer query that feeds an app page may read (#715).

Saved Explorer (SQL) queries select the rows that dashboards, personal
dashboards, ``/yse_sky/``, the pointing tool, Summary View, the bulk photometry
download and ``/query_api/`` show. Explorer itself is a staff-only SQL console
and is unchanged; this module only decides what such a query may read when it
runs for a page viewer.

The rule (Ryan, 2026-10-18): selection may use only data the viewer can see.
Access to data is per point (a photometry point or a spectrum), and that rule is
evaluated by the application (``services/visibility.py``), not by the database.
A viewer who does not see everything therefore gets only queries over
transient metadata tables. A query that reads a photometry, spectrum or
photometry-statistics table (including the stored per-scope values, which are
tied to whoever saved the query) is refused for that viewer, with a reason.
Staff, superusers and the service accounts the query API already leaves
unfiltered (and internal callers that pass no viewer, such as the cache warmer)
are unchanged.

Derived values (fit parameters such as SALT) may later be offered to page
queries for selection only: create a view that exposes just those columns and
list its name in ``PAGE_QUERY_EXTRA_ALLOWED_TABLES``. Pages never display those
columns to a viewer who cannot see the data.

``PAGE_QUERY_TABLE_POLICY`` (settings.ini ``[site_settings]``, or the
``YSE_PAGE_QUERY_TABLE_POLICY`` environment variable) is ``enforce`` (default)
or ``off`` (the old behaviour: no table check).
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from django.conf import settings

POLICY_ENFORCE = "enforce"
POLICY_OFF = "off"

#: Tables a viewer-scoped page query may read: transient metadata only.
DEFAULT_ALLOWED_TABLES = frozenset(
    {
        "yse_app_transient",
        "yse_app_transient_tags",
        "yse_app_transienttag",
        "yse_app_tag",
        "yse_app_transientstatus",
        "yse_app_transientclass",
        "yse_app_host",
        "yse_app_observationgroup",
        "yse_app_internalsurvey",
        "yse_app_antaresclassification",
        "yse_app_alternatetransientnames",
    }
)

#: Transient columns a viewer-scoped page query may not read (#720). They are
#: derived from spectra, whose visibility is decided per spectrum by the
#: application, so a scoped viewer must not select on or receive them.
DENIED_COLUMNS = frozenset({"best_spectrum_id"})

# Non-application tables that must never back a page query.
_DENIED_PREFIXES = (
    "auth_", "authtoken_", "axes_", "otp_", "auditlog_", "django_", "sessions_",
    "admin_", "explorer_", "contenttypes_",
)
_DENIED_SCHEMAS = frozenset({"information_schema", "performance_schema", "mysql"})

# Every identifier-like token. Literals and comments are deliberately not
# stripped: a name inside a string only causes a (conservative) refusal, while
# stripping could be fooled by quoting tricks.
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


class PageQueryRefused(ValueError):
    """A saved query may not feed this viewer's page (reason in ``args[0]``)."""


def page_query_policy() -> str:
    value = str(getattr(settings, "PAGE_QUERY_TABLE_POLICY", POLICY_ENFORCE) or POLICY_ENFORCE).strip().lower()
    return POLICY_OFF if value == POLICY_OFF else POLICY_ENFORCE


def allowed_tables() -> frozenset:
    extra = getattr(settings, "PAGE_QUERY_EXTRA_ALLOWED_TABLES", ()) or ()
    if isinstance(extra, str):
        extra = [p for p in re.split(r"[\s,]+", extra) if p]
    return DEFAULT_ALLOWED_TABLES | {str(t).strip().lower() for t in extra}


def disallowed_tables(sql: str, allowed: Optional[Iterable[str]] = None) -> list:
    """Sorted table-like names in ``sql`` that a viewer-scoped page query may not read."""
    ok = frozenset(allowed) if allowed is not None else allowed_tables()
    found = set()
    for token in _TOKEN_RE.findall(sql or ""):
        low = token.lower()
        if low in _DENIED_SCHEMAS or low.startswith(_DENIED_PREFIXES):
            found.add(low)
        elif low.startswith("yse_app_") and low not in ok:
            found.add(low)
    return sorted(found)


def disallowed_columns(sql: str) -> list:
    """Sorted column names in ``sql`` that a viewer-scoped page query may not read."""
    return sorted({t.lower() for t in _TOKEN_RE.findall(sql or "") if t.lower() in DENIED_COLUMNS})


def viewer_is_scoped(user) -> bool:
    """True when ``user`` is a page viewer whose data access is limited.

    ``None`` means an internal caller (cache warmer) and is not scoped.
    """
    if user is None or page_query_policy() == POLICY_OFF:
        return False
    from YSE_App.services.visibility import user_sees_everything

    return not user_sees_everything(user)


def page_query_table_refusal(sql: str, user) -> Optional[str]:
    """Why ``user`` may not use ``sql`` to feed a page, or None."""
    if not viewer_is_scoped(user):
        return None
    bad_columns = disallowed_columns(sql)
    if bad_columns:
        return (
            "the query reads %s; a saved query that feeds a page may not read columns derived "
            "from spectra for viewers who do not see all data" % ", ".join(bad_columns)
        )
    bad = disallowed_tables(sql)
    if not bad:
        return None
    return (
        "the query reads %s; a saved query that feeds a page may read only transient "
        "metadata tables for viewers who do not see all data (photometry, spectra and "
        "their statistics are checked per data point by the application)" % ", ".join(bad)
    )


def check_page_query(sql: str, user) -> None:
    """Raise :class:`PageQueryRefused` when ``sql`` may not feed ``user``'s page."""
    reason = page_query_table_refusal(sql, user)
    if reason is not None:
        raise PageQueryRefused(reason)


#: Columns a scoped viewer may still receive from a query that reads an extra
#: (derived-value) table: identifiers and position, never the derived values.
_IDENTIFYING_COLUMNS = frozenset(
    {"name", "transient_name", "transient_id", "id", "ra", "dec", "transient_ra", "transient_dec"}
)


def reads_extra_tables(sql: str) -> bool:
    """True when ``sql`` reads a table or view added through ``PAGE_QUERY_EXTRA_ALLOWED_TABLES``."""
    extra = allowed_tables() - DEFAULT_ALLOWED_TABLES
    if not extra:
        return False
    return any(t.lower() in extra for t in _TOKEN_RE.findall(sql or ""))


def selection_only_result(sql: str, user, field_names, rows):
    """``(field_names, rows)`` with derived-value columns dropped for a scoped viewer.

    A query that reads an extra (derived-value) table may select on it, but the
    viewer only gets identifying columns back (#715). Other viewers and queries
    are returned unchanged.
    """
    if not viewer_is_scoped(user):
        return field_names, rows
    if not reads_extra_tables(sql):
        # ``SELECT *`` over the transient table must not return a spectrum-derived column (#720).
        keep = [i for i, f in enumerate(field_names) if str(f).lower() not in DENIED_COLUMNS]
        if len(keep) == len(field_names):
            return field_names, rows
    else:
        keep = [i for i, f in enumerate(field_names) if str(f).lower() in _IDENTIFYING_COLUMNS]
        if not keep:
            keep = [0] if field_names else []
    return (
        [field_names[i] for i in keep],
        [tuple(row[i] for i in keep) for row in rows],
    )
