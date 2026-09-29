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

        with mock.patch("YSE_App.table_utils.get_moon", wraps=__import__("astropy.coordinates", fromlist=["get_moon"]).get_moon) as moon, \
             mock.patch.object(Observer, "target_rise_time", counted_rise), \
             mock.patch.object(Observer, "target_set_time", counted_set):
            _table, cells = self._render()
        self.assertEqual(len(cells), 5)
        self.assertEqual(moon.call_count, 1)
        self.assertEqual(len(rise_calls), 1)
        self.assertEqual(len(set_calls), 1)

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
