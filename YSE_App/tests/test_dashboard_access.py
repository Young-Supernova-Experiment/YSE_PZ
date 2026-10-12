"""
Access-filtered dashboard and API paths for non-staff users (#451-#455).

* #451: ``filter_transients_by_user_access`` is one queryset condition instead
  of a ``user_can_view_transient`` call per transient. A parity test compares
  it with the old per-id implementation (kept here as the reference) over a
  fixture mix of public and private photometry and spectra.
* #452: the deferred personal-dashboard section and the synchronous build
  return the same rows for a non-staff user.
* #453: ``PageFirstQuerySet.count()`` leaves the correlated subqueries out.
* #454: "Last Mag" / "Last Obs. Date" sort on the TransientPhotStat columns and
  keep the displayed annotation.
* #455: personal-dashboard sections filter ``pk__in`` once.

Query counts are measured as a non-staff user (staff bypass the access filter)
and must not grow with the number of transients.
"""

import os
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import AnonymousUser, Group
from django.core.cache import cache
from django.db import connection
from django.db.models import QuerySet
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from YSE_App import views as views_module
from YSE_App.models import Transient, TransientPhotStat, UserQuery
from YSE_App.perf.access import (
    ACCESS_MIX,
    MEMBER_GROUP_NAME,
    OTHER_GROUP_NAME,
    create_non_staff_user,
    seed_access_transients,
    visible_suffixes_for,
)
from YSE_App.services.photstat import recompute_many
from YSE_App.services.visibility import (
    filter_transients_by_user_access,
    user_can_view_transient,
)
from YSE_App.table_utils import (
    AdjustFieldTransientTable,
    FieldTransientTable,
    NewTransientTable,
    PageFirstQuerySet,
    RecentPhotOrderMixin,
    TransientTable,
    YSEFullTransientTable,
    YSERisingTransientTable,
    YSETransientTable,
    annotate_dashboard_transient_fields,
    annotate_open_interest_count,
    annotate_peak_mag,
)
from YSE_App.tests.fixtures_minimal import audit_fields, create_test_user

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def legacy_filter_transients_by_user_access(user, transients):
    """The pre-#451 implementation, verbatim: one visibility check per transient."""
    if user.is_staff or user.is_superuser:
        return transients
    if isinstance(transients, QuerySet):
        ids = list(transients.values_list("id", flat=True))
        allowed = [pk for pk in ids if user_can_view_transient(user, pk)]
        return transients.filter(id__in=allowed)
    return [t for t in transients if user_can_view_transient(user, t.id)]


def _pks(rows):
    if isinstance(rows, QuerySet):
        return sorted(rows.values_list("pk", flat=True))
    return sorted(t.pk for t in rows)


class VisibilityParityTests(TestCase):
    """New queryset condition == old per-id checks, for every kind of user."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = create_test_user("dacc_owner")
        cls.dataset = seed_access_transients(cls.owner, prefix="daccpar")
        cls.users = {
            "member": create_non_staff_user("dacc_member", (MEMBER_GROUP_NAME,)),
            "other": create_non_staff_user("dacc_other", (OTHER_GROUP_NAME,)),
            "both": create_non_staff_user("dacc_both", (MEMBER_GROUP_NAME, OTHER_GROUP_NAME)),
            "nogroups": create_non_staff_user("dacc_nogroups", ()),
            "staff": create_test_user("dacc_staff", is_staff=True),
            "superuser": create_test_user("dacc_super", is_staff=False, is_superuser=True),
            "anonymous": AnonymousUser(),
        }
        Group.objects.get_or_create(name="dacc-unrelated")

    def _qs(self):
        return Transient.objects.filter(name__startswith="daccpar").order_by("pk")

    def test_queryset_and_list_match_legacy_for_every_user(self):
        for label, user in self.users.items():
            with self.subTest(user=label):
                expected = _pks(legacy_filter_transients_by_user_access(user, self._qs()))
                self.assertEqual(_pks(filter_transients_by_user_access(user, self._qs())), expected)
                rows = list(self._qs())
                self.assertEqual(
                    _pks(filter_transients_by_user_access(user, rows)),
                    _pks(legacy_filter_transients_by_user_access(user, rows)),
                )

    def test_expected_rows_per_user(self):
        """The parity test is not vacuous: each user sees exactly the rule's rows."""
        by_suffix = self.dataset.by_suffix
        cases = {
            "member": visible_suffixes_for((MEMBER_GROUP_NAME,)),
            "other": visible_suffixes_for((OTHER_GROUP_NAME,)),
            "both": visible_suffixes_for((MEMBER_GROUP_NAME, OTHER_GROUP_NAME)),
            "nogroups": visible_suffixes_for(()),
            "anonymous": visible_suffixes_for(None),
        }
        for label, suffixes in cases.items():
            with self.subTest(user=label):
                got = _pks(filter_transients_by_user_access(self.users[label], self._qs()))
                self.assertEqual(got, sorted(by_suffix[s].pk for s in suffixes))
        self.assertNotIn("nodata", cases["both"])
        self.assertIn("otherphot-memberspec", cases["member"])
        self.assertNotIn("otherphot-otherspec", cases["member"])
        all_pks = sorted(t.pk for t in self.dataset.transients)
        for label in ("staff", "superuser"):
            self.assertEqual(_pks(filter_transients_by_user_access(self.users[label], self._qs())), all_pks)

    def test_single_object_check_unchanged(self):
        member = self.users["member"]
        for suffix, transient in self.dataset.by_suffix.items():
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    user_can_view_transient(member, transient.pk),
                    suffix in visible_suffixes_for((MEMBER_GROUP_NAME,)),
                )

    def test_queryset_keeps_ordering_and_annotations(self):
        member = self.users["member"]
        from django.db.models import IntegerField, Value

        qs = annotate_dashboard_transient_fields(self._qs().order_by("-disc_date", "-pk")).annotate(
            marker=Value(1, output_field=IntegerField())
        )
        filtered = filter_transients_by_user_access(member, qs)
        self.assertIsInstance(filtered, PageFirstQuerySet)
        self.assertIn("marker", filtered.query.annotations)
        names = [t.name for t in filtered]
        self.assertEqual(names, [t.name for t in legacy_filter_transients_by_user_access(member, qs)])


class VisibilityQueryCountTests(TestCase):
    """Non-staff filtering costs the same number of queries for 11 or 44 transients."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = create_test_user("dacc_qc_owner")
        cls.member = create_non_staff_user("dacc_qc_member", (MEMBER_GROUP_NAME,))

    def _measure(self, prefix):
        qs = Transient.objects.filter(name__startswith=prefix)
        with CaptureQueriesContext(connection) as ctx_qs:
            n_qs = len(list(filter_transients_by_user_access(self.member, qs)))
        rows = list(qs)
        with CaptureQueriesContext(connection) as ctx_list:
            n_list = len(filter_transients_by_user_access(self.member, rows))
        return (len(ctx_qs), len(ctx_list)), n_qs, n_list

    def test_query_count_flat_in_number_of_transients(self):
        seed_access_transients(self.owner, prefix="daccqs", copies=1)
        small, small_n, _ = self._measure("daccqs")
        seed_access_transients(self.owner, prefix="daccql", copies=4)
        large, large_n, large_list_n = self._measure("daccql")
        self.assertEqual(large_n, 4 * small_n)
        self.assertEqual(large_list_n, large_n)
        self.assertEqual(small, large)
        self.assertLessEqual(large[0], 2)  # group names + the filtered SELECT
        self.assertLessEqual(large[1], 2)


@override_settings(PASSWORD_HASHERS=PASSWORD_HASHERS)
class PersonalDashboardAccessTests(TestCase):
    """Deferred section and synchronous build agree for a non-staff user (#452, #455)."""

    @classmethod
    def setUpTestData(cls):
        from explorer.models import Query

        cls.owner = create_test_user("dacc_pd_owner")
        cls.member = create_non_staff_user("dacc_pd_member", (MEMBER_GROUP_NAME,))
        cls.dataset = seed_access_transients(cls.owner, prefix="daccpd")
        cls.names = [t.name for t in cls.dataset.transients]
        query = Query.objects.create(
            title="dacc access query",
            sql="SELECT name FROM YSE_App_transient WHERE name = 'daccpd'",
            description="non-staff access test",
            snapshot=False,
            created_by_user=cls.owner,
        )
        cls.query = query
        cls.sql_uq = UserQuery.objects.create(user=cls.member, query=query, **audit_fields(cls.member))
        cls.py_uq = UserQuery.objects.create(
            user=cls.member, python_query="dacc_access_python_query", **audit_fields(cls.member)
        )
        cls.expected = sorted(
            cls.dataset.by_suffix[s].pk for s in visible_suffixes_for((MEMBER_GROUP_NAME,))
        )

    def setUp(self):
        cache.clear()
        cache.set(views_module.explorer_query_cache_key(self.query.id), list(self.names), timeout=600)
        self.client = Client()
        self.client.force_login(self.member)
        # Python queries are looked up in the registry (#505), not by attribute.
        patcher = mock.patch.dict(
            views_module.yse_python_queries.python_query_reg.all,
            {"dacc_access_python_query": lambda: Transient.objects.filter(name__startswith="daccpd")},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        cache.clear()

    @staticmethod
    def _table_pks(row):
        table = row[0]
        return sorted(table.data.data.values_list("pk", flat=True))

    def _deferred_pks(self, uq):
        response = self.client.get(reverse("personaldashboard_section", args=[uq.id]))
        self.assertEqual(response.status_code, 200)
        return self._table_pks(response.context["transient_cat"])

    def _sync_pks(self):
        with mock.patch.dict(os.environ, {"YSE_PERSONAL_DASHBOARD_DEFER": "0"}):
            response = self.client.get(reverse("personaldashboard"))
        self.assertEqual(response.status_code, 200)
        rows = {row[4]: row for row in response.context["transient_categories"]}
        return {uq_id: self._table_pks(row) for uq_id, row in rows.items()}

    def test_deferred_and_sync_paths_return_same_visible_rows(self):
        sync = self._sync_pks()
        for uq in (self.sql_uq, self.py_uq):
            with self.subTest(section=uq.python_query or "sql"):
                deferred = self._deferred_pks(uq)
                self.assertEqual(deferred, sync[uq.id])
                self.assertEqual(deferred, self.expected)
                self.assertNotIn(self.dataset.by_suffix["otherphot"].pk, deferred)
                self.assertNotIn(self.dataset.by_suffix["nodata"].pk, deferred)

    def test_section_count_is_visible_rows_on_both_paths(self):
        # #558: the count is the number of rows the user can see, not the
        # length of the saved-query result.
        with mock.patch.dict(os.environ, {"YSE_PERSONAL_DASHBOARD_DEFER": "0"}):
            response = self.client.get(reverse("personaldashboard"))
        sync = {row[4]: row for row in response.context["transient_categories"]}
        for uq in (self.sql_uq, self.py_uq):
            with self.subTest(section=uq.python_query or "sql"):
                deferred = self.client.get(reverse("personaldashboard_section", args=[uq.id]))
                row = deferred.context["transient_cat"]
                self.assertEqual(row[5], len(self._table_pks(row)))
                self.assertEqual(row[5], len(self.expected))
                self.assertEqual(sync[uq.id][5], len(self.expected))
        self.assertLess(len(self.expected), len(self.names))

    def test_staff_section_count_is_unchanged(self):
        staff_uq = UserQuery.objects.create(user=self.owner, query=self.query, **audit_fields(self.owner))
        client = Client()
        client.force_login(self.owner)
        row = client.get(reverse("personaldashboard_section", args=[staff_uq.id])).context["transient_cat"]
        self.assertEqual(row[5], len(self.names))

    def test_section_filters_pks_not_names(self):
        with CaptureQueriesContext(connection) as ctx:
            self._deferred_pks(self.sql_uq)
        name_lists = [
            q["sql"] for q in ctx.captured_queries
            if "yse_app_transient`.`name` in (" in q["sql"].lower()
            or 'yse_app_transient"."name" in (' in q["sql"].lower()
        ]
        self.assertEqual(len(name_lists), 1, name_lists)


@override_settings(PASSWORD_HASHERS=PASSWORD_HASHERS)
class NonStaffQueryCountTests(TestCase):
    """Dashboard section and API list queries do not grow with the number of transients."""

    @classmethod
    def setUpTestData(cls):
        from explorer.models import Query

        cls.owner = create_test_user("dacc_nq_owner")
        cls.member = create_non_staff_user("dacc_nq_member", (MEMBER_GROUP_NAME,))
        cls.queries = {}
        for prefix, copies in (("daccns", 2), ("daccnl", 6)):
            dataset = seed_access_transients(cls.owner, prefix=prefix, copies=copies)
            recompute_many([t.pk for t in dataset.transients])
            query = Query.objects.create(
                title=f"dacc nq {prefix}",
                sql="SELECT name FROM YSE_App_transient WHERE name = 'dacc'",
                description="non-staff query count",
                snapshot=False,
                created_by_user=cls.owner,
            )
            uq = UserQuery.objects.create(user=cls.member, query=query, **audit_fields(cls.member))
            cls.queries[prefix] = (query, uq, [t.name for t in dataset.transients])

    def setUp(self):
        cache.clear()
        for query, _uq, names in self.queries.values():
            cache.set(views_module.explorer_query_cache_key(query.id), names, timeout=600)
        self.client = Client()
        self.client.force_login(self.member)

    def tearDown(self):
        cache.clear()

    def _count(self, url):
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(ctx)

    def test_personal_dashboard_section_flat(self):
        # Both sections have more than one page (10 rows) of visible transients.
        small = self._count(reverse("personaldashboard_section", args=[self.queries["daccns"][1].id]))
        large = self._count(reverse("personaldashboard_section", args=[self.queries["daccnl"][1].id]))
        self.assertEqual(small, large)

    def test_personal_dashboard_sorted_by_recent_phot_flat(self):
        prefix_s = views_module._personaldashboard_sql_query_prefix(self.queries["daccns"][0].title)
        prefix_l = views_module._personaldashboard_sql_query_prefix(self.queries["daccnl"][0].title)
        for key in ("recent_mag", "-recent_magdate"):
            with self.subTest(sort=key):
                small = self._count(
                    reverse("personaldashboard_section", args=[self.queries["daccns"][1].id])
                    + f"?{prefix_s}sort={key}"
                )
                large = self._count(
                    reverse("personaldashboard_section", args=[self.queries["daccnl"][1].id])
                    + f"?{prefix_l}sort={key}"
                )
                self.assertEqual(small, large)

    def test_viewset_querysets_flat(self):
        from YSE_App.api_views import (
            SharingSubmissionViewSet,
            TransientPhotStatViewSet,
            TransientViewSet,
        )

        def measure(viewset_cls):
            viewset = viewset_cls()
            viewset.request = SimpleNamespace(user=self.member, query_params={})
            with CaptureQueriesContext(connection) as ctx:
                list(viewset.get_queryset().values_list("pk", flat=True))
            return len(ctx)

        before = {cls: measure(cls) for cls in (TransientViewSet, TransientPhotStatViewSet, SharingSubmissionViewSet)}
        seed_access_transients(self.owner, prefix="daccvx", copies=3)
        after = {cls: measure(cls) for cls in before}
        self.assertEqual(before, after)

    def test_photstat_api_flat(self):
        url = "/api/transientphotstats/?limit=5"
        small = self._count(url)
        more = seed_access_transients(self.owner, prefix="daccpx", copies=3)
        recompute_many([t.pk for t in more.transients])
        self.assertEqual(small, self._count(url))


class PageFirstCountTests(TestCase):
    """count() on the dashboard queryset leaves the correlated subqueries out (#453)."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = create_test_user("dacc_cnt_owner")
        seed_access_transients(cls.owner, prefix="dacccnt")

    def _qs(self):
        """The dashboard queryset plus a caller-added photometry subquery (the
        dashboard itself has none since #270), so count() has two to leave out."""
        from django.db.models import OuterRef, Subquery

        from YSE_App.models import TransientPhotData

        base = Transient.objects.filter(name__startswith="dacccnt").order_by("-disc_date", "-pk")
        latest = TransientPhotData.objects.filter(photometry__transient=OuterRef("pk"), mag__isnull=False).exclude(
            data_quality__isnull=False
        )
        return annotate_open_interest_count(annotate_peak_mag(annotate_dashboard_transient_fields(base))).annotate(
            recent_mag=Subquery(latest.order_by("-obs_date").values("mag")[:1])
        )

    def test_django_32_count_selects_annotations(self):
        """The claim: the stock count() keeps every annotation in the derived table."""
        with CaptureQueriesContext(connection) as ctx:
            QuerySet.count(self._qs())
        sql = ctx.captured_queries[-1]["sql"].lower()
        self.assertIn("yse_app_transientphotdata", sql)
        self.assertIn("yse_app_transientinterest", sql)

    def test_count_without_annotations(self):
        qs = self._qs()
        with CaptureQueriesContext(connection) as ctx:
            n = qs.count()
        self.assertEqual(len(ctx), 1)
        sql = ctx.captured_queries[0]["sql"].lower()
        self.assertNotIn("yse_app_transientphotdata", sql)
        self.assertNotIn("yse_app_transientinterest", sql)
        self.assertEqual(n, len(list(self._qs())))
        self.assertEqual(n, len(ACCESS_MIX))

    def test_filter_on_annotation_still_applies(self):
        qs = self._qs().filter(recent_mag__isnull=False)
        with CaptureQueriesContext(connection) as ctx:
            n = qs.count()
        self.assertIn("yse_app_transientphotdata", ctx.captured_queries[0]["sql"].lower())
        expected = sum(1 for _s, phot, _spec in ACCESS_MIX if phot is not None)
        self.assertEqual(n, expected)
        self.assertEqual(n, len(list(self._qs().filter(recent_mag__isnull=False))))

    def test_distinct_and_values_fall_back(self):
        self.assertEqual(self._qs().distinct().count(), len(ACCESS_MIX))
        self.assertEqual(self._qs().values("pk").count(), len(ACCESS_MIX))


class RecentPhotOrderingTests(TestCase):
    """Last Mag / Last Obs. Date sort on TransientPhotStat and keep the displayed values (#454)."""

    TABLES = (
        TransientTable, FieldTransientTable, AdjustFieldTransientTable, YSETransientTable,
        YSEFullTransientTable, YSERisingTransientTable, NewTransientTable,
    )

    @classmethod
    def setUpTestData(cls):
        cls.owner = create_test_user("dacc_ord_owner")
        dataset = seed_access_transients(cls.owner, prefix="daccord", status_name="NeedsTemplate")
        recompute_many([t.pk for t in dataset.transients])
        cls.dataset = dataset

    def _qs(self):
        return annotate_dashboard_transient_fields(
            Transient.objects.filter(name__startswith="daccord").order_by("-disc_date", "-pk")
        )

    def test_one_implementation_for_every_table(self):
        for table_cls in self.TABLES:
            with self.subTest(table=table_cls.__name__):
                self.assertTrue(issubclass(table_cls, RecentPhotOrderMixin))
                self.assertIs(table_cls.order_recent_mag, RecentPhotOrderMixin.order_recent_mag)
                self.assertIs(table_cls.order_recent_magdate, RecentPhotOrderMixin.order_recent_magdate)

    @staticmethod
    def _shown(t):
        """What the Last Det. / Last Obs. cells show: the stat row's values (#557, #270)."""
        stat = getattr(t, "photstat", None)
        return (getattr(stat, "last_detected_mag", None), getattr(stat, "last_obs_date", None))

    def test_sort_uses_photstat_and_keeps_display(self):
        displayed = {t.pk: self._shown(t) for t in self._qs()}
        table = TransientTable(self._qs())
        for column, field in (("recent_mag", "last_detected_mag"), ("recent_magdate", "last_obs_date")):
            for descending in (False, True):
                with self.subTest(column=column, descending=descending):
                    ordered, handled = getattr(table, f"order_{column}")(self._qs(), descending)
                    self.assertTrue(handled)
                    with CaptureQueriesContext(connection) as ctx:
                        page = list(ordered[0:20])
                    self.assertNotIn("yse_app_transientphotdata", ctx.captured_queries[0]["sql"].lower())
                    stats = {
                        s.transient_id: getattr(s, field)
                        for s in TransientPhotStat.objects.filter(transient__in=[t.pk for t in page])
                    }
                    keys = [stats.get(t.pk) for t in page]
                    with_value = [k for k in keys if k is not None]
                    self.assertEqual(with_value, sorted(with_value, reverse=descending))
                    for t in page:
                        self.assertEqual(self._shown(t), displayed[t.pk])

    @override_settings(PASSWORD_HASHERS=PASSWORD_HASHERS)
    def test_transient_summary_sort(self):
        client = Client()
        client.force_login(self.owner)
        url = reverse("transient_summary", kwargs={"status_or_query_name": "NeedsTemplate"})
        for key, field in (("-last_mag", "last_detected_mag"), ("last_obs_date", "last_obs_date")):
            with self.subTest(sort=key):
                response = client.get(url, {"sort": key})
                self.assertEqual(response.status_code, 200)
                rows = [t for t in response.context["transients"] if t.name.startswith("daccord")]
                stats = {s.transient_id: getattr(s, field) for s in TransientPhotStat.objects.all()}
                with_value = [stats[t.pk] for t in rows if stats.get(t.pk) is not None]
                self.assertTrue(with_value)
                self.assertEqual(with_value, sorted(with_value, reverse=key.startswith("-")))
