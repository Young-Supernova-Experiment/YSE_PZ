"""Query-shape and query-count regressions for the Batch B speed fixes.

Each test pins the property the fix bought (constant query count, no
per-row SQL, one moon lookup per table, cached Explorer SQL) so a later
refactor cannot silently bring the N+1 back.
"""

import datetime
import itertools
from unittest import mock

from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from YSE_App.models import (
    ClassicalNightType,
    ClassicalObservingDate,
    ClassicalResource,
    FollowupStatus,
    Transient,
)
from YSE_App.services.followup_requests import create_or_attach_request
from YSE_App.table_utils import FollowupFilter, TransientFilter
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)


def _followup_status(user, name):
    status, _ = FollowupStatus.objects.get_or_create(name=name, defaults=audit_fields(user))
    return status


def _classical_night(user, *, tag, obs_date):
    """Telescope -> ClassicalResource valid for a week -> one ClassicalObservingDate."""
    _obs_group, instrument, _band = create_instrument_stack(user, obs_group_name=tag)
    night_type, _ = ClassicalNightType.objects.get_or_create(
        name="Full", defaults=audit_fields(user)
    )
    resource = ClassicalResource.objects.create(
        telescope=instrument.telescope,
        begin_date_valid=obs_date - datetime.timedelta(days=3),
        end_date_valid=obs_date + datetime.timedelta(days=3),
        **audit_fields(user),
    )
    night = ClassicalObservingDate.objects.create(
        resource=resource, night_type=night_type, obs_date=obs_date, **audit_fields(user)
    )
    return night


def _request_followup(user, transient, *, resource, comment="", **kwargs):
    """One parent follow-up with one child request spanning the resource window."""
    parent, _child, _created = create_or_attach_request(
        user,
        transient,
        status=_followup_status(user, "Requested"),
        valid_start=resource.begin_date_valid - datetime.timedelta(days=1),
        valid_stop=resource.end_date_valid + datetime.timedelta(days=1),
        comment=comment,
        **kwargs,
    )
    return parent


# --------------------------------------------------------------------------- P1


class SearchFilterTests(TestCase):
    """P1: token-AND of field-OR replaces the permutation expansion."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("speed_search_user")
        cls.t_follow_yse = create_minimal_transient(
            cls.user, name="2026abc", status_name="Following", obs_group_name="YSE"
        )
        cls.t_watch_yse = create_minimal_transient(
            cls.user, name="2026xyz", status_name="Watch", obs_group_name="YSE"
        )
        cls.t_follow_ztf = create_minimal_transient(
            cls.user, name="2025qqq", status_name="Following", obs_group_name="ZTF"
        )

    @staticmethod
    def _search(value):
        return TransientFilter({"ex": value}, queryset=Transient.objects.all()).qs

    @staticmethod
    def _legacy_permutation_matches(value):
        """The pre-P1 algorithm: each token bound to a distinct field, any assignment."""
        tokens = value.split()
        fields = TransientFilter.search_fields
        names = set()
        for t in Transient.objects.select_related("status", "obs_group", "host"):
            values = {
                "name": t.name,
                "ra": str(t.ra),
                "dec": str(t.dec),
                "disc_date": str(t.disc_date or ""),
                "disc_mag": None,
                "obs_group_name": t.obs_group.name,
                "spec_class": None,
                "redshift": None if t.redshift is None else str(t.redshift),
                "host_redshift": None,
                "status_name": t.status.name,
            }
            for assignment in itertools.permutations(fields, len(tokens)):
                if all(
                    values[f] is not None and tok.lower() in values[f].lower()
                    for f, tok in zip(assignment, tokens)
                ):
                    names.add(t.name)
                    break
        return names

    def test_multi_token_search_matches_legacy_results(self):
        for value in ("2026 Following", "YSE 2026", "Following", "ZTF 2025 Following"):
            with self.subTest(value=value):
                got = set(self._search(value).values_list("name", flat=True))
                self.assertEqual(got, self._legacy_permutation_matches(value))
        self.assertEqual(
            set(self._search("2026 Following").values_list("name", flat=True)),
            {"2026abc"},
        )

    def test_query_count_and_like_clauses_are_linear_in_tokens(self):
        n_fields = len(TransientFilter.search_fields)
        for value in ("2026", "2026 Following", "2026 Following YSE"):
            n_tokens = len(value.split())
            with self.subTest(value=value), CaptureQueriesContext(connection) as ctx:
                list(self._search(value))
            self.assertEqual(len(ctx.captured_queries), 1)
            sql = ctx.captured_queries[0]["sql"]
            self.assertEqual(sql.upper().count(" LIKE "), n_tokens * n_fields)
            self.assertNotIn("GROUP BY", sql.upper())

    def test_followup_filter_is_linear_in_tokens(self):
        night = _classical_night(self.user, tag="p1-night", obs_date=timezone.now())
        _request_followup(self.user, self.t_follow_yse, resource=night.resource,
                          classical_resource=night.resource)
        n_fields = len(FollowupFilter.search_fields)
        value = "2026abc Requested Following"
        with CaptureQueriesContext(connection) as ctx:
            rows = list(FollowupFilter({"ex": value}, queryset=night.resource.transientfollowup_set.all()).qs)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(ctx.captured_queries), 1)
        self.assertEqual(ctx.captured_queries[0]["sql"].upper().count(" LIKE "), 3 * n_fields)
