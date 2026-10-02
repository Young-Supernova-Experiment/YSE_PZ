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
from django.urls import reverse
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


# --------------------------------------------------------------------------- P2


class FollowupTableRecentMagTests(TestCase):
    """P2: recent_mag is one subquery on the follow-up queryset, not a query per row."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("speed_recent_mag_user")
        cls.night = _classical_night(cls.user, tag="p2-night", obs_date=timezone.now())
        cls.transients = []
        for i in range(6):
            t = create_minimal_transient(cls.user, name=f"p2mag{i}", ra=10.0 + i, dec=-5.0 + i)
            if i != 5:  # one row without photometry
                attach_synthetic_photometry(cls.user, t, n_points=2 + i)
            _request_followup(cls.user, t, resource=cls.night.resource,
                              classical_resource=cls.night.resource)
            cls.transients.append(t)

    def _cells(self, table_cls, qs, **kwargs):
        table = table_cls(qs, **kwargs)
        return [(row.record.transient.name, row.get_cell("recent_mag")) for row in table.rows]

    def test_recent_mag_cells_match_model_method(self):
        from YSE_App.table_utils import FollowupTable, ObsNightFollowupTable, ToOFollowupTable

        qs = self.night.resource.transientfollowup_set.select_related("transient")
        expected = {t.name: t.recent_mag() for t in self.transients}
        for table_cls, kwargs in (
            (FollowupTable, {}),
            (ObsNightFollowupTable, {"classical_obs_date": self.night}),
            (ToOFollowupTable, {"too_resource": self.night.resource}),
        ):
            with self.subTest(table=table_cls.__name__):
                for name, cell in self._cells(table_cls, qs, **kwargs):
                    if expected[name] is None:
                        self.assertEqual(cell, "—")  # django-tables2 default for None
                    else:
                        self.assertEqual(cell, expected[name])

    def test_recent_mag_query_count_does_not_grow_with_rows(self):
        from YSE_App.table_utils import FollowupTable

        def count_for(n):
            ids = [t.id for t in self.transients[:n]]
            qs = self.night.resource.transientfollowup_set.filter(
                transient_id__in=ids
            ).select_related("transient")
            with CaptureQueriesContext(connection) as ctx:
                cells = self._cells(FollowupTable, qs)
            self.assertEqual(len(cells), n)
            return len(ctx.captured_queries)

        self.assertEqual(count_for(2), count_for(6))
        self.assertLessEqual(count_for(6), 2)

    def test_ordering_by_recent_mag_uses_the_annotation(self):
        from YSE_App.table_utils import FollowupTable

        qs = self.night.resource.transientfollowup_set.select_related("transient")
        with CaptureQueriesContext(connection) as ctx:
            table = FollowupTable(qs, order_by="-recent_mag")
            names = [row.record.transient.name for row in table.rows]
        self.assertLessEqual(len(ctx.captured_queries), 2)
        with_mag = [t for t in self.transients if t.recent_mag() is not None]
        expected = [t.name for t in sorted(with_mag, key=lambda t: float(t.recent_mag()), reverse=True)]
        self.assertEqual(names[: len(expected)], expected)
        self.assertEqual(names[-1], "p2mag5")  # NULL magnitude sorts last on descending


# --------------------------------------------------------------------------- P5


class ObservingPagesQueryCountTests(TestCase):
    """P5: observing_night / too_requests prefetch child requests; rows add no queries."""

    @classmethod
    def setUpTestData(cls):
        from YSE_App.models import ToOResource

        cls.user = create_test_user("speed_obsnight_user")
        cls.other = create_test_user("speed_obsnight_other", is_staff=False)
        cls.obs_date = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
        cls.night = _classical_night(cls.user, tag="p5-night", obs_date=cls.obs_date)
        cls.too = ToOResource.objects.create(
            telescope=cls.night.resource.telescope,
            begin_date_valid=cls.obs_date - datetime.timedelta(days=3),
            end_date_valid=cls.obs_date + datetime.timedelta(days=3),
            **audit_fields(cls.user),
        )
        cls.transients = []
        for i in range(10):
            t = create_minimal_transient(cls.user, name=f"p5obs{i}", ra=20.0 + i, dec=-10.0 + i)
            attach_synthetic_photometry(cls.user, t, n_points=2)
            for resource_kw in ({"classical_resource": cls.night.resource}, {"too_resource": cls.too}):
                resource = list(resource_kw.values())[0]
                _request_followup(cls.user, t, resource=resource, comment=f"first {i}", **resource_kw)
                _request_followup(cls.other, t, resource=resource, comment=f"second {i}", **resource_kw)
            cls.transients.append(t)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _queries_for(self, url, n_rows):
        from YSE_App.models import TransientFollowup
        from YSE_App.tests.deploy_checklist_helpers import iers_offline

        keep = [t.id for t in self.transients[:n_rows]]
        # Materialise the ids: MySQL rejects UPDATE ... WHERE pk IN (SELECT ... same table).
        hidden_pks = list(
            TransientFollowup.objects.exclude(transient_id__in=keep).values_list("pk", flat=True)
        )
        hidden = TransientFollowup.objects.filter(pk__in=hidden_pks)
        # Park the other rows outside the resource window instead of deleting them.
        far = self.obs_date + datetime.timedelta(days=400)
        hidden.update(valid_start=far, valid_stop=far)
        try:
            with iers_offline(), CaptureQueriesContext(connection) as ctx:
                response = self.client.get(url)
        finally:
            hidden.update(
                valid_start=self.obs_date - datetime.timedelta(days=4),
                valid_stop=self.obs_date + datetime.timedelta(days=4),
            )
        self.assertEqual(response.status_code, 200)
        for t in self.transients[:n_rows]:
            self.assertContains(response, t.name)
        self.assertContains(response, "second %d" % (n_rows - 1))
        return len(ctx.captured_queries)

    def test_observing_night_query_count_is_flat_in_rows(self):
        from django.urls import reverse

        url = reverse(
            "observing_night",
            kwargs={
                "telescope": self.night.resource.telescope.name.replace(" ", "_"),
                "obs_date": self.obs_date.strftime("%Y-%m-%d"),
                "pi_name": "None",
            },
        )
        self.assertEqual(self._queries_for(url, 2), self._queries_for(url, 10))

    def test_too_requests_query_count_is_flat_in_rows(self):
        from django.urls import reverse

        url = reverse(
            "too_requests",
            kwargs={
                "telescope": self.too.telescope.name.replace(" ", "_"),
                "pi_name": "None",
            },
        )
        self.assertEqual(self._queries_for(url, 2), self._queries_for(url, 10))


# --------------------------------------------------------------------------- P3


class ObservingTableAstroTests(TestCase):
    """P3: one moon lookup and one vectorised rise/set solve per table render."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("speed_astro_user")
        cls.obs_date = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
        cls.night = _classical_night(cls.user, tag="p3-night", obs_date=cls.obs_date)
        cls.night.resource.telescope.latitude = 19.8
        cls.night.resource.telescope.longitude = -155.5
        cls.night.resource.telescope.elevation = 4200.0
        cls.night.resource.telescope.save()
        cls.transients = []
        for i, (ra, dec) in enumerate(((30.0, 10.0), (150.0, -20.0), (250.0, 45.0), (10.0, -80.0), (30.0, 10.0))):
            t = create_minimal_transient(cls.user, name=f"p3astro{i}", ra=ra, dec=dec)
            _request_followup(cls.user, t, resource=cls.night.resource,
                              classical_resource=cls.night.resource)
            cls.transients.append(t)

    def setUp(self):
        from django.core.cache import cache

        # rise/set and the moon are cached per (telescope, night); every test starts cold
        cache.clear()
        self.addCleanup(cache.clear)

    def _render(self):
        from django.test import RequestFactory
        from YSE_App.table_utils import ObsNightFollowupTable
        from YSE_App.tests.deploy_checklist_helpers import iers_offline

        request = RequestFactory().get("/")
        request.user = self.user
        qs = self.night.resource.transientfollowup_set.select_related("transient")
        table = ObsNightFollowupTable(qs, classical_obs_date=self.night)
        with iers_offline():
            table.as_html(request)
            cells = [
                (
                    row.record.transient,
                    row.get_cell("rise_time"),
                    row.get_cell("set_time"),
                    row.get_cell("moon_angle"),
                )
                for row in table.rows
            ]
        return table, cells

    def test_moon_and_rise_set_are_solved_once_per_table(self):
        from astroplan import Observer

        rise_calls, set_calls = [], []
        orig_rise, orig_set = Observer.target_rise_time, Observer.target_set_time

        def counted_rise(obs, *a, **k):
            rise_calls.append(a)
            return orig_rise(obs, *a, **k)

        def counted_set(obs, *a, **k):
            set_calls.append(a)
            return orig_set(obs, *a, **k)

        with mock.patch("YSE_App.services.night_astro.get_moon", wraps=__import__("astropy.coordinates", fromlist=["get_moon"]).get_moon) as moon, \
             mock.patch.object(Observer, "target_rise_time", counted_rise), \
             mock.patch.object(Observer, "target_set_time", counted_set):
            _table, cells = self._render()
        self.assertEqual(len(cells), 5)
        self.assertEqual(moon.call_count, 1)
        self.assertEqual(len(rise_calls), 1)
        self.assertEqual(len(set_calls), 1)

    def test_second_render_of_the_same_night_is_served_from_the_cache(self):
        """P3 follow-up (#216): rise/set and the moon are cached per (telescope, night)."""
        from astroplan import Observer

        _table, first = self._render()
        with mock.patch("YSE_App.services.night_astro.get_moon") as moon, \
             mock.patch.object(Observer, "target_rise_time") as rise, \
             mock.patch.object(Observer, "target_set_time") as sett:
            table, second = self._render()
        self.assertEqual(second, first)
        self.assertEqual((moon.call_count, rise.call_count, sett.call_count), (0, 0, 0))
        self.assertEqual(len(table._rise_set), 4)

    def test_cache_key_changes_with_the_telescope_site(self):
        from YSE_App.services.night_astro import rise_set_cache_key

        tel = self.night.resource.telescope
        before = rise_set_cache_key(tel, "2026-01-01", 18.0)
        tel.latitude += 1.0
        self.assertNotEqual(before, rise_set_cache_key(tel, "2026-01-01", 18.0))
        self.assertNotEqual(before, rise_set_cache_key(tel, "2026-01-02", 18.0))

    def test_vectorised_values_match_per_row_astroplan(self):
        import astropy.units as u
        from astropy.coordinates import EarthLocation, SkyCoord, get_moon
        from astropy.time import Time
        from astroplan import Observer
        from YSE_App.tests.deploy_checklist_helpers import iers_offline

        table, cells = self._render()
        tel = self.night.resource.telescope
        observer = Observer(
            location=EarthLocation.from_geodetic(tel.longitude * u.deg, tel.latitude * u.deg, tel.elevation * u.m),
            timezone="UTC",
        )
        tme = Time(str(self.night.obs_date).split()[0])

        def legacy(t):  # the pre-P3 per-row expression, masked/NaN -> None
            if t and t.value == t.value:
                return t.isot.split("T")[-1].split(".")[0]
            return None

        with iers_offline():
            moon = get_moon(tme)
            for transient, rise, sett, moon_angle in cells:
                sc = SkyCoord("%s %s" % tuple(transient.CoordString()), unit=(u.hourangle, u.deg))
                exp_rise = observer.target_rise_time(tme, sc, horizon=18 * u.deg, which="previous")
                exp_set = observer.target_set_time(tme, sc, horizon=18 * u.deg, which="previous")
                self.assertEqual(rise, legacy(exp_rise))
                self.assertEqual(sett, legacy(exp_set))
                self.assertEqual(moon_angle, "%.1f" % sc.separation(moon).deg)
        # Duplicate coordinates share one cache entry; the circumpolar-south target is None.
        self.assertEqual(len(table._rise_set), 4)
        by_name = {cell[0].name: cell for cell in cells}
        self.assertIsNone(by_name["p3astro3"][1])
        self.assertTrue(all(by_name[f"p3astro{i}"][1] for i in (0, 1, 2, 4)))


# ------------------------------------------------------------------------ P6/P7


class YseHomeQueryCountTests(TestCase):
    """P6: yse_home tables prefetch follow-up resources; rows add no queries."""

    @classmethod
    def setUpTestData(cls):
        from YSE_App.models import ToOResource, TransientTag
        from YSE_App.tests.deploy_checklist_helpers import (
            create_instrument,
            create_telescope,
            ensure_observation_group,
        )

        cls.user = create_test_user("speed_home_user")
        ensure_observation_group(cls.user, "YSE")
        ps1 = create_telescope(cls.user, "Pan-STARRS1")
        create_instrument(cls.user, ps1, "GPC1", band_names=("g", "r"))
        tag, _ = TransientTag.objects.get_or_create(name="YSE", defaults=audit_fields(cls.user))
        now = timezone.now()
        cls.night = _classical_night(cls.user, tag="p6-classical", obs_date=now)
        _obs_group, too_instrument, _band = create_instrument_stack(cls.user, obs_group_name="p6-too")
        cls.too = ToOResource.objects.create(
            telescope=too_instrument.telescope,
            begin_date_valid=now - datetime.timedelta(days=3),
            end_date_valid=now + datetime.timedelta(days=3),
            **audit_fields(cls.user),
        )
        successful = _followup_status(cls.user, "Successful")
        cls.transients = []
        for i in range(12):
            t = create_minimal_transient(cls.user, name=f"p6home{i}", ra=40.0 + i, dec=5.0 + i)
            t.tags.add(tag)
            # distinct discovery dates, newest first: the tables order by -disc_date and
            # a 10-row page of 12 all-NULL dates is whichever ten MySQL feels like
            Transient.objects.filter(pk=t.pk).update(disc_date=now - datetime.timedelta(days=i))
            attach_synthetic_photometry(cls.user, t, n_points=2)
            _request_followup(cls.user, t, resource=cls.night.resource,
                              classical_resource=cls.night.resource, comment=f"note {i}")
            done = _request_followup(cls.user, t, resource=cls.too, too_resource=cls.too)
            done.status = successful
            done.save(update_fields=["status"])
            cls.transients.append(t)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    SECTION_KEYS = ("yse", "yse_follow", "yserise", "ysefastrise", "yseztf")

    def _queries_for(self, n_rows, url="/yse_home/"):
        from YSE_App.tests.deploy_checklist_helpers import iers_offline

        ignore = Transient.objects.get(name=self.transients[0].name).status.__class__.objects.get(name="Ignore")
        hidden = Transient.objects.filter(name__startswith="p6home").exclude(
            pk__in=[t.pk for t in self.transients[:n_rows]]
        )
        new_status = self.transients[0].status
        hidden.update(status=ignore)
        try:
            with iers_offline(), CaptureQueriesContext(connection) as ctx:
                response = self.client.get(url)
        finally:
            hidden.update(status=new_status)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        for t in self.transients[:min(n_rows, 10)]:
            self.assertIn(t.name, body)
        if url == "/yse_home/":  # Next Telescope Nights / ToO Resources boxes
            self.assertIn(self.night.resource.telescope.name, body)
            self.assertIn(self.too.telescope.name, body)
        return len(ctx.captured_queries)

    def test_yse_home_query_count_is_flat_in_rows(self):
        self.assertEqual(self._queries_for(3), self._queries_for(12))

    def test_yse_home_query_count_is_flat_in_rows_without_deferral(self):
        import os

        with mock.patch.dict(os.environ, {"YSE_HOME_DEFER": "0"}):
            self.assertEqual(self._queries_for(3), self._queries_for(12))

    def test_section_fragment_query_count_is_flat_in_rows(self):
        url = reverse("yse_home_section", kwargs={"section_key": "yse"})
        self.assertEqual(self._queries_for(3, url), self._queries_for(12, url))

    def test_shell_defers_four_sections_and_is_cheaper_than_the_synchronous_page(self):
        """P7 (#217): Latest Transients renders inline; the other four tables load via AJAX."""
        import os

        from YSE_App.tests.deploy_checklist_helpers import iers_offline

        with iers_offline(), CaptureQueriesContext(connection) as shell_ctx:
            shell = self.client.get("/yse_home/")
        self.assertEqual(shell.status_code, 200)
        body = shell.content.decode()
        for key in self.SECTION_KEYS[1:]:
            self.assertIn(
                'data-url="%s"' % reverse("yse_home_section", kwargs={"section_key": key}), body
            )
        self.assertNotIn(reverse("yse_home_section", kwargs={"section_key": "yse"}), body)
        self.assertIn("yhome-section-loading", body)
        self.assertIn("yseHomeSectionUrl", body)
        # the synchronous table still carries its rows and the status dropdown
        self.assertIn(self.transients[0].name, body)
        self.assertIn('class="transientStatusChange"', body)
        # the deferred sections keep their (query-free) filter forms
        for key in self.SECTION_KEYS[1:]:
            self.assertIn('name="%s-ex"' % key, body)

        with mock.patch.dict(os.environ, {"YSE_HOME_DEFER": "0"}), iers_offline(), \
             CaptureQueriesContext(connection) as sync_ctx:
            sync = self.client.get("/yse_home/")
        self.assertEqual(sync.status_code, 200)
        self.assertNotIn("yhome-section-loading", sync.content.decode())
        self.assertLess(len(shell_ctx.captured_queries), len(sync_ctx.captured_queries))

    def test_each_section_fragment_renders_its_table(self):
        from YSE_App.tests.deploy_checklist_helpers import iers_offline

        for key in self.SECTION_KEYS:
            with iers_offline():
                response = self.client.get(reverse("yse_home_section", kwargs={"section_key": key}))
            self.assertEqual(response.status_code, 200, key)
            self.assertIn("<table", response.content.decode(), key)
        fragment = self.client.get(reverse("yse_home_section", kwargs={"section_key": "yse"})).content.decode()
        self.assertIn(self.transients[0].name, fragment)
        # the status dropdown needs all_transient_statuses in the fragment context
        self.assertIn('data-status_name="Ignore"', fragment)
        # the follow-up table (Req. Followup / Followed By columns) reads the prefetch
        from YSE_App.models import TransientStatus

        following, _ = TransientStatus.objects.get_or_create(name="Following", defaults=audit_fields(self.user))
        Transient.objects.filter(pk=self.transients[0].pk).update(status=following)
        try:
            with CaptureQueriesContext(connection) as ctx:
                follow = self.client.get(reverse("yse_home_section", kwargs={"section_key": "yse_follow"})).content.decode()
        finally:
            Transient.objects.filter(pk=self.transients[0].pk).update(status=self.transients[0].status)
        self.assertIn(self.transients[0].name, follow)
        self.assertIn(self.night.resource.telescope.name, follow)
        self.assertIn(self.too.telescope.name, follow)
        self.assertIn("note 0", follow)
        import re

        # one prefetch of the follow-ups (plus one of their requests), never one per row
        followup_queries = [q for q in ctx.captured_queries if re.search(r'FROM .YSE_App_transientfollowup(?!request)', q["sql"])]
        self.assertEqual(len(followup_queries), 1, [q["sql"][:120] for q in followup_queries])
        self.assertEqual(
            self.client.get(reverse("yse_home_section", kwargs={"section_key": "nope"})).status_code, 404
        )

    def test_section_fragment_honours_search_and_sort_parameters(self):
        url = reverse("yse_home_section", kwargs={"section_key": "yse"})
        response = self.client.get(url, {"yse-ex": "p6home1", "ysesort": "-name_string"})
        body = response.content.decode()
        self.assertIn("p6home11", body)
        self.assertNotIn("p6home2", body)
        self.assertLess(body.index("p6home11"), body.index("p6home10"))

    def test_resource_columns_fall_back_without_prefetch(self):
        from YSE_App.table_utils import followup_comments_text, followup_resource_names

        t = Transient.objects.get(name="p6home0")
        self.assertEqual(followup_resource_names(t, ("Requested", "InProcess")), self.night.resource.telescope.name)
        self.assertEqual(followup_resource_names(t, ("Successful",)), self.too.telescope.name)
        self.assertIn("note 0", followup_comments_text(t))
        prefetched = Transient.objects.filter(pk=t.pk)
        from YSE_App.table_utils import prefetch_followup_resources

        with CaptureQueriesContext(connection) as ctx:
            row = prefetch_followup_resources(prefetched).get()
            self.assertEqual(followup_resource_names(row, ("Requested", "InProcess")), self.night.resource.telescope.name)
            self.assertIn("note 0", followup_comments_text(row))
        self.assertEqual(len(ctx.captured_queries), 3)  # transient + followups + requests


# ------------------------------------------------------------------- P10 (#245)


class FollowupPageTelescopeLoopTests(TestCase):
    """followup: only telescopes with a visible follow-up cost a query and a table."""

    @classmethod
    def setUpTestData(cls):
        from YSE_App.tests.deploy_checklist_helpers import create_telescope

        cls.user = create_test_user("speed_followup_page_user")
        cls.obs_date = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
        cls.night = _classical_night(cls.user, tag="p10-night", obs_date=cls.obs_date)
        cls.transient = create_minimal_transient(cls.user, name="p10follow", ra=70.0, dec=2.0)
        attach_synthetic_photometry(cls.user, cls.transient, n_points=2)
        _request_followup(cls.user, cls.transient, resource=cls.night.resource,
                          classical_resource=cls.night.resource)
        cls.empty = [create_telescope(cls.user, f"p10-empty-{i}") for i in range(10)]

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _queries_with_empty_telescopes(self, n_empty):
        """Page query count with ``n_empty`` follow-up-less telescopes in the catalogue."""
        from YSE_App.models import Telescope
        from YSE_App.tests.deploy_checklist_helpers import create_telescope

        surplus = Telescope.objects.filter(pk__in=[t.pk for t in self.empty[n_empty:]])
        removed = list(surplus.values_list("name", flat=True))
        surplus.delete()
        try:
            with CaptureQueriesContext(connection) as ctx:
                response = self.client.get(reverse("followup"))
        finally:
            self.empty[n_empty:] = [create_telescope(self.user, name) for name in removed]
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("p10follow", body)
        self.assertIn(self.night.resource.telescope.name, body)
        self.assertNotIn("p10-empty-", body)
        return len(ctx.captured_queries)

    def test_query_count_does_not_grow_with_empty_telescopes(self):
        self.assertEqual(self._queries_with_empty_telescopes(2), self._queries_with_empty_telescopes(10))


# ------------------------------------------------------------------- P11 (#246)


class YseObservingNightTwilightTests(TestCase):
    """yse_observing_night: one telescope lookup and cached twilight times."""

    @classmethod
    def setUpTestData(cls):
        from YSE_App.tests.deploy_checklist_helpers import create_instrument, create_telescope

        cls.user = create_test_user("speed_twilight_user")
        cls.ps1 = create_telescope(cls.user, "Pan-STARRS1")
        create_instrument(cls.user, cls.ps1, "GPC1", band_names=("g",))

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.addCleanup(cache.clear)
        self.client = Client()
        self.client.force_login(self.user)

    def _get(self):
        from YSE_App.tests.deploy_checklist_helpers import iers_offline

        with iers_offline(), CaptureQueriesContext(connection) as ctx:
            response = self.client.get(reverse("yse_observing_night", kwargs={"obs_date": "2026-03-15"}))
        self.assertEqual(response.status_code, 200)
        return response, ctx.captured_queries

    def test_telescope_is_fetched_once_and_twilight_is_solved_once(self):
        from astroplan import Observer

        first, queries = self._get()
        self.assertEqual(sum("Pan-STARRS1" in q["sql"] for q in queries), 1)

        with mock.patch.object(Observer, "sun_set_time") as sunset, \
             mock.patch.object(Observer, "twilight_evening_nautical") as nautical:
            second, _queries = self._get()
        self.assertEqual((sunset.call_count, nautical.call_count), (0, 0))

        def without_csrf(response):
            import re

            return re.sub(r"[A-Za-z0-9]{64}", "", response.content.decode())  # CSRF tokens differ

        self.assertEqual(without_csrf(second), without_csrf(first))
        from YSE_App.services.night_astro import twilight_times

        night = twilight_times(self.ps1, "2026-03-15 00:00:00")
        for key in ("sunset", "night_start_12", "night_end_18", "sunrise", "moon_illum"):
            self.assertIn("<td>%s</td>" % night[key], first.content.decode())

    def test_cached_twilight_values_match_the_inline_astroplan_expressions(self):
        import astropy.units as u
        from astroplan import Observer, moon_illumination
        from astropy.coordinates import EarthLocation
        from astropy.time import Time

        from YSE_App.common.utilities import date_to_mjd
        from YSE_App.services.night_astro import twilight_times
        from YSE_App.tests.deploy_checklist_helpers import iers_offline

        ut_obs_date = "2026-03-15 00:00:00"
        with iers_offline():
            night = twilight_times(self.ps1, ut_obs_date)
            location = EarthLocation.from_geodetic(
                self.ps1.longitude * u.deg, self.ps1.latitude * u.deg, self.ps1.elevation * u.m
            )
            time = Time(ut_obs_date, format="iso")
            tel = Observer(location=location, timezone="UTC")
            expected = {
                "sunset": tel.sun_set_time(time, which="next").isot.split("T")[-1][:-7],
                "night_start_12": tel.twilight_evening_nautical(time, which="next").isot.split("T")[-1][:-7],
                "night_start_18": tel.twilight_evening_astronomical(time, which="next").isot.split("T")[-1][:-7],
                "night_end_18": tel.twilight_morning_astronomical(time, which="next").isot.split("T")[-1][:-7],
                "night_end_12": tel.twilight_morning_nautical(time, which="next").isot.split("T")[-1][:-7],
                "sunrise": tel.sun_rise_time(time, which="next").isot.split("T")[-1][:-7],
                "moon_illum": "%.3f" % moon_illumination(time),
                "sunset_mjd": float(date_to_mjd(tel.sun_set_time(time, which="next"))),
                "sunrise_mjd": float(date_to_mjd(tel.sun_rise_time(time, which="next"))),
            }
        self.assertEqual(night, expected)
        self.assertRegex(night["sunset"], r"^\d\d:\d\d$")

    def test_obs_night_table_falls_back_to_the_lookup_without_a_telescope(self):
        from YSE_App.models import SurveyObservation
        from YSE_App.table_utils import YSEObsNightTable

        with CaptureQueriesContext(connection) as ctx:
            table = YSEObsNightTable(SurveyObservation.objects.none(), obs_date="2026-03-15")
        self.assertEqual(sum("Pan-STARRS1" in q["sql"] for q in ctx.captured_queries), 1)
        with CaptureQueriesContext(connection) as ctx:
            YSEObsNightTable(SurveyObservation.objects.none(), obs_date="2026-03-15", telescope=self.ps1)
        self.assertEqual(len(ctx.captured_queries), 0)
        self.assertAlmostEqual(table.tel.location.lat.deg, self.ps1.latitude, places=6)


# ------------------------------------------------------------------- P15 (#247)


class OnCallUserPrefetchTests(TestCase):
    """yse_oncall_calendar / yse_home on-call box: users come from one prefetch."""

    @classmethod
    def setUpTestData(cls):
        from YSE_App.models import YSEOnCallDate
        from YSE_App.tests.deploy_checklist_helpers import create_instrument, create_telescope

        cls.user = create_test_user("speed_oncall_user")
        ps1 = create_telescope(cls.user, "Pan-STARRS1")
        create_instrument(cls.user, ps1, "GPC1", band_names=("g",))
        cls.observers = [create_test_user(f"speed_oncall_obs{i}", is_staff=False) for i in range(8)]
        now = timezone.now()
        cls.dates = []
        for i, observer in enumerate(cls.observers):
            row = YSEOnCallDate.objects.create(on_call_date=now, **audit_fields(cls.user))
            row.user.add(observer, cls.user)
            cls.dates.append(row)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _queries_for(self, url, n_dates):
        from YSE_App.models import YSEOnCallDate
        from YSE_App.tests.deploy_checklist_helpers import iers_offline

        far = timezone.now() + datetime.timedelta(days=400)
        hidden = YSEOnCallDate.objects.filter(pk__in=[d.pk for d in self.dates[n_dates:]])
        if url == "/yse_home/":
            hidden.update(on_call_date=far)  # only today's dates are on the page
        else:
            hidden_pks = list(hidden.values_list("pk", flat=True))
            hidden_users = {pk: list(YSEOnCallDate.objects.get(pk=pk).user.all()) for pk in hidden_pks}
            hidden.delete()
        try:
            with iers_offline(), CaptureQueriesContext(connection) as ctx:
                response = self.client.get(url)
        finally:
            if url == "/yse_home/":
                hidden.update(on_call_date=timezone.now())
            else:
                for pk, users in hidden_users.items():
                    row = YSEOnCallDate.objects.create(pk=pk, on_call_date=timezone.now(), **audit_fields(self.user))
                    row.user.add(*users)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        for observer in self.observers[:n_dates]:
            self.assertIn(observer.email if url == "/yse_home/" else observer.username, body)
        return len(ctx.captured_queries)

    def test_oncall_calendar_query_count_is_flat_in_dates(self):
        url = reverse("yse_oncall_calendar")
        self.assertEqual(self._queries_for(url, 2), self._queries_for(url, 8))

    def test_yse_home_oncall_box_query_count_is_flat_in_dates(self):
        self.assertEqual(self._queries_for("/yse_home/", 2), self._queries_for("/yse_home/", 8))


# ------------------------------------------------------------------------- #256


class PageFirstQuerySetTests(TestCase):
    """Dashboard page slices select the page pks first, then the annotated rows."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("speed_pagefirst_user")
        cls.transients = []
        for i in range(8):
            t = create_minimal_transient(cls.user, name=f"pf{i}", ra=10.0 + i, dec=1.0 + i)
            attach_synthetic_photometry(cls.user, t, n_points=2 + i % 3)
            Transient.objects.filter(pk=t.pk).update(
                disc_date=timezone.now() - datetime.timedelta(days=i)
            )
            cls.transients.append(t)

    def _qs(self):
        from YSE_App.table_utils import annotate_dashboard_transient_fields

        return annotate_dashboard_transient_fields(
            Transient.objects.filter(name__startswith="pf").order_by("-disc_date")
        )

    def _plain(self):
        from YSE_App.table_utils import _recent_phot_subqueries

        recent_mag, recent_magdate = _recent_phot_subqueries("pk")
        return (
            Transient.objects.filter(name__startswith="pf")
            .order_by("-disc_date")
            .select_related("status", "host", "obs_group")
            .annotate(recent_mag=recent_mag, recent_magdate=recent_magdate)
        )

    def test_slice_runs_a_bare_pk_query_then_the_annotated_rows(self):
        with CaptureQueriesContext(connection) as ctx:
            rows = list(self._qs()[2:5])
        self.assertEqual(len(ctx.captured_queries), 2)
        ids_sql, rows_sql = (q["sql"] for q in ctx.captured_queries)
        self.assertIn("LIMIT 3", ids_sql)
        self.assertNotIn("JOIN", ids_sql)
        self.assertNotIn("YSE_App_transientphotdata", ids_sql)
        self.assertIn("IN (", rows_sql)
        self.assertIn("YSE_App_transientphotdata", rows_sql)
        self.assertIn("JOIN", rows_sql)
        expected = list(self._plain()[2:5])
        self.assertEqual([r.pk for r in rows], [r.pk for r in expected])
        self.assertEqual(
            [(r.recent_mag, r.recent_magdate, r.status.name) for r in rows],
            [(r.recent_mag, r.recent_magdate, r.status.name) for r in expected],
        )
        # FKs came with the rows, not per row
        with CaptureQueriesContext(connection) as ctx:
            _names = [(r.status.name, r.obs_group.name, r.host) for r in rows]
        self.assertEqual(len(ctx.captured_queries), 0)

    def test_wrap_keeps_prefetch_lookups(self):
        from YSE_App.table_utils import annotate_dashboard_transient_fields

        qs = annotate_dashboard_transient_fields(
            Transient.objects.filter(name__startswith="pf").order_by("-disc_date").prefetch_related("tags")
        )
        with CaptureQueriesContext(connection) as ctx:
            rows = list(qs[:4])
            tags = [list(r.tags.all()) for r in rows]
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(tags), 4)
        self.assertEqual(len(ctx.captured_queries), 3)  # pk page, rows, one tags prefetch

    def test_ordering_by_an_annotation_and_plain_operations_are_unchanged(self):
        qs = self._qs()
        by_mag = list(qs.order_by("-recent_mag", "-pk")[:3])
        self.assertEqual([r.pk for r in by_mag], [r.pk for r in self._plain().order_by("-recent_mag", "-pk")[:3]])
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(qs.count(), 8)
            self.assertEqual(list(qs.values_list("pk", flat=True)[:2]), [t.pk for t in self.transients[:2]])
            self.assertEqual(qs[0].pk, self.transients[0].pk)
        self.assertEqual(len(ctx.captured_queries), 3)
        self.assertEqual(list(self._qs().none()[:5]), [])
        self.assertEqual(type(qs.filter(ra__gt=0)).__name__, "PageFirstQuerySet")

    def test_dashboard_section_renders_the_same_rows(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("dashboard_section", kwargs={"status_key": "new"}), {"newpage": 1})
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        for t in self.transients:
            self.assertIn(t.name, body)


# -------------------------------------------------------------------------- P13


class _ExplorerToDefault:
    """Route the 'explorer' alias to 'default' so the saved SQL sees the seeded rows.

    CI has no test database for the explorer alias (its MySQL user cannot open
    test_YSE), and inside a TestCase transaction only 'default' sees the rows.
    """

    def __getitem__(self, alias):
        from django.db import connections

        return connections["default" if alias == "explorer" else alias]


class ExplorerQueryCacheReuseTests(TestCase):
    """P13: one Explorer SQL run serves the dashboard section and transient_summary."""

    @classmethod
    def setUpTestData(cls):
        from YSE_App.tests.fixtures_minimal import seed_personal_dashboard_queries

        cls.user = create_test_user("speed_explorer_cache_user")
        cls.user_query = seed_personal_dashboard_queries(cls.user, n_queries=1)[0]
        cls.query = cls.user_query.query

    def setUp(self):
        from django.core.cache import cache

        from YSE_App import views as views_module

        cache.clear()
        self.addCleanup(cache.clear)
        patcher = mock.patch.object(views_module, "connections", _ExplorerToDefault())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = Client()
        self.client.force_login(self.user)

    def _explorer_runs(self, url, expect_status=200):
        """(response, number of times the saved SQL itself was executed)."""
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, expect_status)
        saved_sql = self.query.sql.lower()
        runs = [q for q in ctx.captured_queries if q["sql"].lower().strip() == saved_sql]
        return response, len(runs)

    def test_summary_reuses_dashboard_result_within_ttl(self):
        from django.core.cache import cache
        from urllib.parse import quote

        from YSE_App.views import explorer_query_cache_key

        _section, cold = self._explorer_runs(f"/personaldashboard/section/{self.user_query.id}/")
        self.assertEqual(cold, 1)
        self.assertEqual(cache.get(explorer_query_cache_key(self.query.id)), ["perf-pdash-q0"])

        summary, warm = self._explorer_runs(f"/transient_summary/{quote(self.query.title)}/")
        self.assertEqual(warm, 0)
        self.assertContains(summary, "perf-pdash-q0")

        _again, warm_section = self._explorer_runs(f"/personaldashboard/section/{self.user_query.id}/")
        self.assertEqual(warm_section, 0)

    def test_summary_runs_sql_once_when_cache_is_cold(self):
        from urllib.parse import quote

        url = f"/transient_summary/{quote(self.query.title)}/"
        first, cold = self._explorer_runs(url)
        _second, warm = self._explorer_runs(url)
        self.assertEqual((cold, warm), (1, 0))
        self.assertContains(first, "perf-pdash-q0")

    def test_change_status_for_query_uses_cached_names(self):
        from django.core.cache import cache

        from YSE_App.models import TransientStatus
        from YSE_App.views import explorer_query_cache_key

        cache.set(explorer_query_cache_key(self.query.id), ["perf-pdash-q0"], timeout=3600)
        watch = TransientStatus.objects.get(name="Watch")
        response, runs = self._explorer_runs(
            reverse("change_status_for_query", kwargs={"query_id": self.user_query.id, "status_id": watch.id}),
        )
        self.assertEqual(runs, 0)
        self.assertEqual(Transient.objects.get(name="perf-pdash-q0").status, watch)
        # #398: JSON with the count instead of a redirect the browser re-sent as PATCH
        self.assertEqual(response.json()["n_changed"], 1)
        self.assertEqual(response.json()["n_selected"], 1)
        self.assertTrue(response.json()["msg"].startswith("success"))

    def test_change_status_for_query_is_one_update_and_skips_unchanged(self):
        """#398: one UPDATE however many transients; the per-row save() loop timed out."""
        from django.core.cache import cache

        from YSE_App.models import TransientStatus
        from YSE_App.views import explorer_query_cache_key

        cache.set(explorer_query_cache_key(self.query.id), ["perf-pdash-q0"], timeout=3600)
        watch = TransientStatus.objects.get(name="Watch")
        url = reverse("change_status_for_query", kwargs={"query_id": self.user_query.id, "status_id": watch.id})
        with CaptureQueriesContext(connection) as ctx:
            first = self.client.patch(url)
        updates = [q for q in ctx.captured_queries if q["sql"].lstrip().upper().startswith("UPDATE")]
        self.assertEqual(len(updates), 1, [q["sql"] for q in updates])
        self.assertEqual(first.json()["n_changed"], 1)
        again = self.client.patch(url)
        self.assertEqual(again.json()["n_changed"], 0)
        self.assertEqual(again.json()["n_selected"], 1)

    def test_change_status_for_query_refuses_another_users_query(self):
        from YSE_App.models import TransientStatus

        other = create_test_user("speed_explorer_cache_other", is_staff=False)
        self.client.force_login(other)
        before = Transient.objects.get(name="perf-pdash-q0").status_id
        watch = TransientStatus.objects.get(name="Watch")
        response = self.client.patch(
            reverse("change_status_for_query", kwargs={"query_id": self.user_query.id, "status_id": watch.id}))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Transient.objects.get(name="perf-pdash-q0").status_id, before)

    def test_change_status_for_query_reports_a_failing_query(self):
        from YSE_App import views as views_module
        from YSE_App.models import TransientStatus

        before = Transient.objects.get(name="perf-pdash-q0").status_id
        watch = TransientStatus.objects.get(name="Watch")
        with mock.patch.object(views_module, "run_explorer_query_cached", side_effect=RuntimeError("boom")), \
                mock.patch.object(views_module.logger, "exception"):
            response = self.client.patch(
                reverse("change_status_for_query", kwargs={"query_id": self.user_query.id, "status_id": watch.id}))
        self.assertEqual(response.status_code, 502)
        self.assertIn("no status changed", response.json()["msg"])
        self.assertEqual(Transient.objects.get(name="perf-pdash-q0").status_id, before)


# ------------------------------------------------------------- Batch A residual


class DownloadTargetListTests(TestCase):
    """download_target_list / download_targets_and_finders: 404 on a missing night, not 500."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("speed_download_user")
        cls.obs_date = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
        cls.night = _classical_night(cls.user, tag="a-res-night", obs_date=cls.obs_date)
        cls.transient = create_minimal_transient(cls.user, name="ares0", ra=50.0, dec=1.0)
        attach_synthetic_photometry(cls.user, cls.transient, n_points=2)
        _request_followup(cls.user, cls.transient, resource=cls.night.resource,
                          classical_resource=cls.night.resource)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _url(self, name, obs_date):
        return reverse(
            name,
            kwargs={
                "telescope": self.night.resource.telescope.name.replace(" ", "_"),
                "obs_date": obs_date,
            },
        )

    def test_target_list_404_without_a_night_and_200_with_one(self):
        from YSE_App.tests.deploy_checklist_helpers import iers_offline

        missing = (self.obs_date + datetime.timedelta(days=30)).strftime("%Y-%m-%d")
        self.assertEqual(self.client.get(self._url("download_target_list", missing)).status_code, 404)
        with iers_offline():
            response = self.client.get(self._url("download_target_list", self.obs_date.strftime("%Y-%m-%d")))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("ares0", body)
        self.assertIn("mag = %s" % self.transient.recent_mag(), body)

    def test_targets_and_finders_404_without_a_night_and_200_with_one(self):
        from YSE_App.tests.deploy_checklist_helpers import iers_offline

        missing = (self.obs_date + datetime.timedelta(days=30)).strftime("%Y-%m-%d")
        self.assertEqual(
            self.client.get(self._url("download_targets_and_finders", missing)).status_code, 404
        )
        finder = mock.MagicMock()
        finder.return_value.finderchart_noview.return_value = (
            [{"id": "star1", "ra": "03:20:00.0", "dec": "+01:00:00.0", "mag": 15.0, "ra_off": 1.5, "dec_off": -2.0}],
            "finder.png",
        )
        with iers_offline(), mock.patch("YSE_App.view_utils.finder", finder):
            response = self.client.get(
                self._url("download_targets_and_finders", self.obs_date.strftime("%Y-%m-%d"))
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("ares0", response.content.decode())
