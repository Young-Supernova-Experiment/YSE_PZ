"""SQL twin of ``redshift_precedence.best_redshift()`` (#736).

List columns, sorting, filters and CSV exports need the best redshift of many transients
at once, so the precedence list is repeated here as a ``Case``/``When`` expression over
``redshift_override``, ``redshift_source`` (transient and host) and the host photo-z.
``tests/test_best_redshift_display.py`` checks that the two agree on a mixed fixture.
The rank values come from ``redshift_precedence`` (read only); change the rules there
and in :func:`_rank_expression` together.

Cost: a few CASE expressions over columns already joined (``host``); no extra queries.
"""

from __future__ import annotations

from django.db.models import Case, F, FloatField, IntegerField, Q, Value, When
from django.db.models.functions import Coalesce, Lower, Trim

from YSE_App.services import redshift_precedence as rp

BEST_REDSHIFT = "best_redshift"

_OWN_LABELS = (rp.SOURCE_YSE.lower(), "yse", rp.SOURCE_MANUAL.lower())


def _rank_expression(src):
    """CASE for ``rp.source_rank`` over the lower-cased, trimmed source alias ``src``."""
    photo = Q(**{src + "__contains": "photo"}) | Q(**{src + "__contains": "phot-z"}) | Q(**{src + "__contains": "photz"})
    return Case(
        When(Q(**{src: ""}), then=Value(rp.RANK_UNKNOWN)),
        When(photo, then=Value(rp.RANK_PHOTO_Z)),
        When(Q(**{src: rp.SOURCE_NED.lower()}), then=Value(rp.RANK_NED)),
        When(Q(**{src + "__in": _OWN_LABELS}), then=Value(rp.RANK_OWN)),
        When(Q(**{src: rp.SOURCE_TNS.lower()}), then=Value(rp.RANK_TNS)),
        When(Q(**{src: "desi"}) | Q(**{src + "__startswith": "desi "}), then=Value(rp.RANK_DESI)),
        default=Value(rp.RANK_CATALOGUE),
        output_field=IntegerField(),
    )


def _score(value_field, override_field, src, tiebreak):
    """Sort key matching ``best_redshift``'s tuple (override, rank, transient-on-tie); -1 if no value."""
    return Case(
        When(Q(**{value_field + "__isnull": True}), then=Value(-1)),
        default=(
            Case(When(Q(**{override_field: True}), then=Value(1000)), default=Value(0), output_field=IntegerField())
            + _rank_expression(src) * Value(2)
            + Value(tiebreak)
        ),
        output_field=IntegerField(),
    )


def annotate_best_redshift(qs, name: str = BEST_REDSHIFT):
    """Annotate ``name`` (default ``best_redshift``) with what ``best_redshift(t).value`` returns.

    Idempotent. Returns the queryset unchanged when ``name`` is already annotated.
    """
    if name in qs.query.annotations:
        return qs
    qs = qs.alias(
        _z_t_src=Lower(Trim(Coalesce("redshift_source", Value("")))),
        _z_h_src=Lower(Trim(Coalesce("host__redshift_source", Value("")))),
    )
    qs = qs.alias(
        _z_t_score=_score("redshift", "redshift_override", "_z_t_src", 1),
        _z_h_score=_score("host__redshift", "host__redshift_override", "_z_h_src", 0),
    )
    best = Case(
        When(
            Q(redshift__isnull=False) & (Q(host__redshift__isnull=True) | Q(_z_t_score__gte=F("_z_h_score"))),
            then=F("redshift"),
        ),
        When(Q(host__redshift__isnull=False), then=F("host__redshift")),
        default=F("host__photo_z"),
        output_field=FloatField(),
    )
    return qs.annotate(**{name: best})


def has_best_redshift_q() -> Q:
    """Rows whose ``best_redshift`` is not NULL (own, host spec-z or host photo-z)."""
    return Q(redshift__isnull=False) | Q(host__redshift__isnull=False) | Q(host__photo_z__isnull=False)
