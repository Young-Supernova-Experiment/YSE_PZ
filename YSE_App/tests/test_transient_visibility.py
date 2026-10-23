"""One visibility policy for transient lists and their derived values (#607).

* Listing (``services.visibility.filter_listed_transients``): a TNS-named
  object is always listed; any other object only when the user may see some
  of its data; service readers (cron and script accounts) see everything; staff are users like any other (#706); anonymous users nothing.
* Derived values: per-scope ``TransientPhotStatScope`` rows (option (a)); a
  viewer with hidden photometry sees the best scope they may see, never the
  all-data row; before the backfill, no value at all.
* The invariance rule: for a non-staff user without access to a group's
  photometry, adding that group's data changes no listed value, sort order or
  API field on the search table, the API, the dashboard or the export.

Synthetic data only.
"""

import csv
import datetime
import io
import re
from unittest import mock

from django.apps import apps
from django.contrib.auth.models import AnonymousUser, Group
from django.core.cache import cache
from django.core.management import call_command
from django.db.models import F
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ProjectState
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from YSE_App.models import (
    AlternateTransientNames,
    Transient,
    TransientPhotData,
    TransientPhotometry,
    TransientPhotStat,
    TransientPhotStatScope,
)
from YSE_App.services import bazin, fit_status, photstat
from YSE_App.services.photstat_visibility import (
    alias_stat_fields,
    public_stat,
    visible_stat,
    visible_stats,
)
from YSE_App.services.visibility import (
    filter_listed_transients,
    transients_with_hidden_photometry,
    user_can_list_transient,
)
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

GROUP = "vis607-private"
OTHER_GROUP = "vis607-other"


def _point(photometry, band, days_ago, mag, user):
    return TransientPhotData.objects.create(
        photometry=photometry, band=band,
        obs_date=timezone.now() - datetime.timedelta(days=days_ago),
        mag=mag, mag_err=0.05, **audit_fields(user),
    )


# The 'staff' fixture account is a service reader here (it reads all data); plain staff see only
# what their groups allow since #706 (tests/test_staff_visibility.py).
@override_settings(DATA_SERVICE_READER_USERNAMES=['vis607_staff'])
class _VisibilityFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("vis607_staff")
        cls.member = create_test_user("vis607_member", is_staff=False)
        cls.outsider = create_test_user("vis607_outsider", is_staff=False)
        cls.group, _ = Group.objects.get_or_create(name=GROUP)
        cls.other_group, _ = Group.objects.get_or_create(name=OTHER_GROUP)
        cls.member.groups.add(cls.group)
        cls.obs_group, cls.instrument, cls.band = create_instrument_stack(cls.staff, obs_group_name="vis607")

        # A and B: public photometry; B's last detection is the newer one.
        cls.a = create_minimal_transient(cls.staff, name="YSEvis607a", ra=10.0, dec=10.0)
        cls.b = create_minimal_transient(cls.staff, name="YSEvis607b", ra=11.0, dec=10.0)
        for transient, points in ((cls.a, ((10, 19.0), (6, 18.5))), (cls.b, ((8, 19.5), (4, 19.2)))):
            phot = cls._photometry(transient)
            for days_ago, mag in points:
                _point(phot, cls.band, days_ago, mag, cls.staff)
        # P: photometry shared with the private group only.
        cls.p = create_minimal_transient(cls.staff, name="YSEvis607p", ra=12.0, dec=10.0)
        _point(cls._photometry(cls.p, cls.group), cls.band, 3, 18.0, cls.staff)
        # T: a TNS designation and no data at all.
        cls.t = create_minimal_transient(cls.staff, name="2026vtns", ra=13.0, dec=10.0)
        # Q: no data, a TNS alias.
        cls.q = create_minimal_transient(cls.staff, name="YSEvis607q", ra=14.0, dec=10.0)
        AlternateTransientNames.objects.create(transient=cls.q, name="2026vali", **audit_fields(cls.staff))

    @classmethod
    def _photometry(cls, transient, *groups):
        phot = TransientPhotometry.objects.create(
            transient=transient, instrument=cls.instrument, obs_group=cls.obs_group, **audit_fields(cls.staff))
        if groups:
            phot.groups.add(*groups)
        return phot

    def setUp(self):
        super().setUp()
        cache.clear()

    def add_private_detection(self, transient, days_ago=1, mag=17.0, group=None):
        phot = self._photometry(transient, group or self.group)
        return _point(phot, self.band, days_ago, mag, self.staff)


class ListingTests(_VisibilityFixture):
    def names(self, user):
        return set(filter_listed_transients(user, Transient.objects.all()).values_list("name", flat=True))

    def test_outsider_sees_tns_named_and_data_visible_objects_only(self):
        names = self.names(self.outsider)
        self.assertTrue({"YSEvis607a", "YSEvis607b", "2026vtns", "YSEvis607q"} <= names)
        self.assertNotIn("YSEvis607p", names)

    def test_member_and_staff(self):
        self.assertIn("YSEvis607p", self.names(self.member))
        self.assertEqual(self.names(self.staff), set(Transient.objects.values_list("name", flat=True)))

    def test_anonymous_sees_nothing(self):
        self.assertEqual(self.names(AnonymousUser()), set())

    def test_iterable_form_and_single_check_agree(self):
        rows = list(Transient.objects.all())
        self.assertEqual({t.name for t in filter_listed_transients(self.outsider, rows)}, self.names(self.outsider))
        self.assertTrue(user_can_list_transient(self.outsider, self.t.pk))
        self.assertFalse(user_can_list_transient(self.outsider, self.p.pk))

    @override_settings(ONBOARDING_GATE_ENABLED=False)
    def test_search_page_api_and_export_list_the_same_objects(self):
        client = Client()
        client.force_login(self.outsider)
        expected = self.names(self.outsider)
        html = client.get("/search/?per_page=200").content.decode()
        slugs = set(re.findall(r'/transient_detail/([^/"]+)/', html))
        page = set(Transient.objects.filter(slug__in=slugs).values_list("name", flat=True))
        data = client.get("/api/transients/?limit=200").json()
        rows = data["results"] if isinstance(data, dict) else data
        api = {r["name"] for r in rows}
        export = {row[0] for row in csv.reader(io.StringIO(client.get("/search/export/").content.decode()))
                  if row and not row[0].startswith("#")} - {"name"}
        self.assertEqual(page, expected)
        self.assertEqual(api, expected)
        self.assertEqual(export, expected)


class ScopeRowTests(_VisibilityFixture):
    def test_scopes_only_for_restricted_transients(self):
        self.assertFalse(TransientPhotStatScope.objects.filter(transient=self.a).exists())
        keys = set(TransientPhotStatScope.objects.filter(transient=self.p).values_list("scope_key", flat=True))
        self.assertEqual(keys, {"public", "g%d" % self.group.pk})
        public = TransientPhotStatScope.objects.get(transient=self.p, scope_key="public")
        self.assertEqual(public.num_obs_global, 0)
        self.assertEqual(TransientPhotStatScope.objects.get(transient=self.p, group=self.group).num_det_global, 1)

    def test_group_change_moves_points_between_scopes(self):
        phot = TransientPhotometry.objects.get(transient=self.p)
        phot.groups.remove(self.group)
        self.assertFalse(TransientPhotStatScope.objects.filter(transient=self.p).exists())
        phot.groups.add(self.group)
        self.assertEqual(TransientPhotStatScope.objects.filter(transient=self.p).count(), 2)

    def test_best_scope_is_the_most_complete_one_the_user_may_see(self):
        self.add_private_detection(self.a, days_ago=2, mag=17.5, group=self.other_group)
        self.add_private_detection(self.a, days_ago=1, mag=17.0, group=self.group)
        both = create_test_user("vis607_both", is_staff=False)
        both.groups.add(self.group, self.other_group)
        # Sees every restricting group: the exact all-data row.
        self.assertIsInstance(visible_stat(both, self.a.pk), TransientPhotStat)
        # Sees one group: that group's scope (public points plus the group's).
        row = visible_stat(self.member, self.a.pk)
        self.assertIsInstance(row, TransientPhotStatScope)
        self.assertEqual(row.group_id, self.group.pk)
        self.assertEqual(row.num_det_global, 3)
        self.assertAlmostEqual(row.peak_mag, 17.0)
        # Sees no group: the public scope.
        row = visible_stat(self.outsider, self.a.pk)
        self.assertEqual(row.scope_key, "public")
        self.assertAlmostEqual(row.peak_mag, 18.5)
        self.assertEqual(row.num_det_global, 2)

    def test_several_groups_with_hidden_data_use_the_larger_single_scope(self):
        third, _ = Group.objects.get_or_create(name="vis607-third")
        self.add_private_detection(self.a, days_ago=2, mag=17.5, group=self.other_group)
        self.add_private_detection(self.a, days_ago=1.5, mag=17.4, group=self.other_group)
        self.add_private_detection(self.a, days_ago=1, mag=17.0, group=self.group)
        self.add_private_detection(self.a, days_ago=0.5, mag=16.0, group=third)
        two = create_test_user("vis607_two", is_staff=False)
        two.groups.add(self.group, self.other_group)
        row = visible_stat(two, self.a.pk)
        # The documented approximation: the other group's scope (4 points) wins over this one (3).
        self.assertEqual(row.group_id, self.other_group.pk)
        self.assertEqual(row.num_obs_global, 4)

    def test_sql_and_python_pick_the_same_row(self):
        self.add_private_detection(self.a, days_ago=1, mag=17.0)
        for user in (self.staff, self.member, self.outsider):
            qs, names = alias_stat_fields(Transient.objects.filter(pk=self.a.pk), user, ["peak_mag"])
            sql_value = qs.annotate(v=F(names["peak_mag"])).values_list("v", flat=True).first()
            self.assertAlmostEqual(sql_value, visible_stats(user, [self.a.pk])[self.a.pk].peak_mag)

    def test_before_the_backfill_no_values_and_the_detail_page_heals(self):
        self.add_private_detection(self.a)
        TransientPhotStatScope.objects.filter(transient=self.a).delete()
        self.assertIsNone(visible_stats(self.outsider, [self.a.pk])[self.a.pk])
        qs, names = alias_stat_fields(Transient.objects.filter(pk=self.a.pk), self.outsider, ["peak_mag"])
        self.assertEqual(qs.filter(**{names["peak_mag"] + "__isnull": True}).count(), 1)
        self.assertAlmostEqual(visible_stat(self.outsider, self.a.pk).peak_mag, 18.5)

    def test_public_stat_for_collaboration_wide_outputs(self):
        self.add_private_detection(self.a)
        self.assertAlmostEqual(public_stat(self.a.pk).peak_mag, 18.5)
        self.assertAlmostEqual(TransientPhotStat.objects.get(transient=self.a).peak_mag, 17.0)


class InvarianceTests(_VisibilityFixture):
    """Adding a group's photometry changes nothing an outsider sees (#607, item 4)."""

    def _client(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _tables(self, html):
        return re.findall(r"<table.*?</table>", html, flags=re.S)

    def snapshot(self, user):
        client = self._client(user)
        out = {}
        for key, url in (
            ("search", "/search/?per_page=200"),
            ("search_sorted", "/search/?per_page=200&ordering=-last_det_mjd"),
            ("search_table_sort", "/search/?per_page=200&sort=-recent_mag"),
            ("search_peak", "/search/?per_page=200&sort=peak_mag"),
            ("search_filtered", "/search/?per_page=200&latest_mag_max=18.9"),
            ("dashboard", "/dashboard/"),
        ):
            response = client.get(url)
            self.assertEqual(response.status_code, 200, url)
            out[key] = self._tables(response.content.decode())
        for key, url in (
            ("api_sorted", "/api/transients/?limit=200&ordering=-last_det_mjd"),
            ("api_filtered", "/api/transients/?limit=200&latest_mag_max=18.9&num_det_min=1"),
            ("api_peak", "/api/transients/?limit=200&ordering=peak_mag"),
        ):
            data = client.get(url).json()
            rows = data["results"] if isinstance(data, dict) else data
            out[key] = [r["name"] for r in rows]
        data = client.get("/api/transientphotstats/?limit=200").json()
        out["photstat_api"] = data["results"] if isinstance(data, dict) else data
        out["export"] = client.get("/search/export/?ordering=-last_det_mjd").content.decode()
        out["visible"] = {
            tid: None if row is None else (row.peak_mag, row.last_detected_mag, row.last_obs_date, row.num_det_global)
            for tid, row in visible_stats(user, Transient.objects.values_list("pk", flat=True)).items()
        }
        return out

    @override_settings(ONBOARDING_GATE_ENABLED=False)
    def test_outsider_sees_no_change(self):
        before = self.snapshot(self.outsider)
        self.assertEqual(before["api_sorted"][:2], ["YSEvis607b", "YSEvis607a"])
        self.add_private_detection(self.a, days_ago=1, mag=17.0)
        self.add_private_detection(self.b, days_ago=0.5, mag=21.0)
        cache.clear()
        after = self.snapshot(self.outsider)
        for key in before:
            self.assertEqual(before[key], after[key], key)

    @override_settings(ONBOARDING_GATE_ENABLED=False)
    def test_member_and_staff_do_see_the_new_data(self):
        self.add_private_detection(self.a, days_ago=1, mag=17.0)
        for user in (self.member, self.staff):
            data = self._client(user).get("/api/transients/?limit=200&ordering=-last_det_mjd").json()
            rows = data["results"] if isinstance(data, dict) else data
            self.assertEqual([r["name"] for r in rows][0], "YSEvis607a", user.username)
            self.assertAlmostEqual(visible_stat(user, self.a.pk).peak_mag, 17.0)


class FittedValueTests(_VisibilityFixture):
    def test_bazin_detections_are_the_viewers(self):
        self.add_private_detection(self.a)
        n_all = sum(len(pts) for _name, pts in bazin.detections_by_transient([self.a.pk])[self.a.pk]["bands"].values())
        mine = bazin.detections_by_transient([self.a.pk], user=self.outsider)[self.a.pk]
        self.assertEqual(n_all, 3)
        self.assertEqual(sum(len(pts) for _name, pts in mine["bands"].values()), 2)
        self.assertEqual(bazin._scope_token(self.staff), "all")
        self.assertNotEqual(bazin._scope_token(self.outsider), "all")

    def test_stored_salt_fit_hidden_from_a_viewer_with_hidden_photometry(self):
        self.add_private_detection(self.a)
        sentinel = object()
        with mock.patch.object(fit_status, "latest_successful_run", return_value=sentinel):
            self.assertIs(fit_status.stored_salt_fit(self.a, user=self.staff), sentinel)
            self.assertIs(fit_status.stored_salt_fit(self.a, user=self.member), sentinel)
            self.assertIsNone(fit_status.stored_salt_fit(self.a, user=self.outsider))
            self.assertIs(fit_status.stored_salt_fit(self.b, user=self.outsider), sentinel)
        self.assertEqual(transients_with_hidden_photometry(self.outsider, [self.a.pk, self.b.pk]), {self.a.pk})


class BackfillCommandTests(_VisibilityFixture):
    def test_dry_run_writes_nothing_and_apply_writes(self):
        TransientPhotStatScope.objects.all().delete()
        out = io.StringIO()
        call_command("backfill_photstat_scopes", "--quiet", stdout=out)
        self.assertIn("Dry run", out.getvalue())
        self.assertFalse(TransientPhotStatScope.objects.exists())
        call_command("backfill_photstat_scopes", "--apply", "--quiet", stdout=io.StringIO())
        self.assertEqual(TransientPhotStatScope.objects.filter(transient=self.p).count(), 2)
        out = io.StringIO()
        call_command("backfill_photstat_scopes", "--apply", "--quiet", stdout=out)
        self.assertIn("created 0, updated 0, deleted 0", out.getvalue())

    def test_scope_rows_follow_a_recompute(self):
        self.assertEqual(photstat.sync_scopes([self.p.pk], dry_run=True).created, 0)


class MigrationTests(TestCase):
    def test_migrations_match_the_models(self):
        """makemigrations would create nothing for the stat tables."""
        loader = MigrationLoader(None, ignore_no_migrations=True)
        changes = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(apps)).changes(
            graph=loader.graph)
        mine = [
            op for migration in changes.get("YSE_App", []) for op in migration.operations
            if (getattr(op, "model_name", None) or getattr(op, "name", "")).lower()
            in ("transientphotstat", "transientphotstatscope")
        ]
        self.assertEqual(mine, [], [op.describe() for op in mine])
