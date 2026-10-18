"""Dashboard SQL queries rewritten as viewer-scoped saved Python queries (#720).

For a viewer who sees all data each Python query returns the rows of the saved
SQL it replaces (compared on MySQL only, where that SQL runs); for an outsider
it selects only from visible photometry. ``switch_dashboard_queries`` repoints
dashboard sections, dry run by default. Synthetic data only.
"""

import datetime
import json
import tempfile
from io import StringIO

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from YSE_App.management.commands.switch_dashboard_queries import DEFAULT_MAPPING
from YSE_App.models import (
    Host,
    PhotometricBand,
    Transient,
    TransientPhotData,
    TransientPhotometry,
    TransientStatus,
    TransientTag,
    UserQuery,
)
from YSE_App.perf.mag_limited import mag_limited_sample_sql
from YSE_App.queries import dashboard_saved_queries as dsq
from YSE_App.queries import yse_python_queries as ypq
from YSE_App.services import page_query_scope as pqs
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
    ensure_transient_statuses,
)

GROUP = "dpq720-private"
ON_MYSQL = connection.vendor == "mysql"


def sql_names(sql):
    with connection.cursor() as cursor:
        cursor.execute(sql)
        return sorted({row[0] for row in cursor.fetchall()})


def names(qs):
    return sorted(qs.values_list("name", flat=True))


@override_settings(DATA_SERVICE_READER_USERNAMES=["dpq720_staff"])
class _Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("dpq720_staff")
        cls.member = create_test_user("dpq720_member", is_staff=False)
        cls.outsider = create_test_user("dpq720_outsider", is_staff=False)
        cls.group, _ = Group.objects.get_or_create(name=GROUP)
        cls.member.groups.add(cls.group)
        cls.obs_group, cls.instrument, _ = create_instrument_stack(cls.staff, obs_group_name="dpq720")
        cls.band, _ = PhotometricBand.objects.get_or_create(
            name="r", defaults={"instrument": cls.instrument, "disp_color": "#ff0000",
                                "disp_symbol": "circle", **audit_fields(cls.staff)})
        cls.tag = TransientTag.objects.create(name="YSE", **audit_fields(cls.staff))
        cls.statuses = ensure_transient_statuses(cls.staff)

    @classmethod
    def transient(cls, name, **fields):
        t = create_minimal_transient(cls.staff, name=name, obs_group_name="dpq720")
        if fields:
            Transient.objects.filter(pk=t.pk).update(**fields)
        return Transient.objects.get(pk=t.pk)

    @classmethod
    def points(cls, transient, specs, private=False):
        """specs: (days_ago, mag, mag_err, flux, flux_err) tuples, one header per call."""
        phot = TransientPhotometry.objects.create(
            transient=transient, instrument=cls.instrument, obs_group=cls.obs_group, **audit_fields(cls.staff))
        if private:
            phot.groups.add(cls.group)
        for days_ago, mag, mag_err, flux, flux_err in specs:
            TransientPhotData.objects.create(
                photometry=phot, band=cls.band, obs_date=timezone.now() - datetime.timedelta(days=days_ago),
                mag=mag, mag_err=mag_err, flux=flux, flux_err=flux_err, **audit_fields(cls.staff))
        return phot


class MagnitudeLimitedTests(_Fixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.public = cls.transient("2026dpqa")
        cls.public.tags.add(cls.tag)
        cls.points(cls.public, [(5, 18.0, 0.05, 100.0, 10.0)])
        cls.hidden = cls.transient("2026dpqb")  # bright only in a private set
        cls.hidden.tags.add(cls.tag)
        cls.points(cls.hidden, [(5, 19.5, 0.05, 100.0, 10.0)])
        cls.points(cls.hidden, [(4, 17.5, 0.05, 100.0, 10.0)], private=True)
        cls.noisy = cls.transient("2026dpqc")  # bright but flux/flux_err <= 3
        cls.noisy.tags.add(cls.tag)
        cls.points(cls.noisy, [(5, 17.0, 0.5, 20.0, 10.0)])
        cls.untagged = cls.transient("2026dpqd")
        cls.points(cls.untagged, [(5, 17.0, 0.05, 100.0, 10.0)])

    def test_viewer_who_sees_all_gets_the_sql_rows(self):
        got = names(ypq.yse_magnitude_limited_sample(user=self.staff))
        self.assertEqual(got, ["2026dpqa", "2026dpqb"])
        self.assertEqual(names(ypq.yse_magnitude_limited_sample()), got)
        if ON_MYSQL:
            self.assertEqual(sql_names(mag_limited_sample_sql()), got)

    def test_outsider_sees_only_visible_points(self):
        self.assertEqual(names(ypq.yse_magnitude_limited_sample(user=self.outsider)), ["2026dpqa"])

    def test_member_sees_the_private_set(self):
        self.assertEqual(names(ypq.yse_magnitude_limited_sample(user=self.member)), ["2026dpqa", "2026dpqb"])

    def test_runs_through_the_saved_query_runner(self):
        qs = ypq.run_python_query("yse_magnitude_limited_sample", self.outsider)
        self.assertEqual(names(qs), ["2026dpqa"])


class RecentDetectionTests(_Fixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        steady = [(6, 19.0, 0.1, None, None), (4, 18.8, 0.1, None, None), (1, 18.5, 0.1, None, None)]
        cls.fast = cls.transient("2026dpqe")
        cls.points(cls.fast, steady)
        cls.hidden = cls.transient("2026dpqf")  # its last point is private
        cls.points(cls.hidden, steady[:2])
        cls.points(cls.hidden, steady[2:], private=True)
        cls.classified = cls.transient("2026dpqg", TNS_spec_class="SN Ia")
        cls.points(cls.classified, steady)
        cls.stale = cls.transient("2026dpqh")  # first detection 30 days ago
        cls.points(cls.stale, [(30, 19.0, 0.1, None, None), (4, 18.8, 0.1, None, None), (1, 18.5, 0.1, None, None)])

    def test_fast_and_young_sql_rows_for_viewer_who_sees_all(self):
        got = names(ypq.fast_and_young_targets(user=self.staff))
        self.assertEqual(got, ["2026dpqe", "2026dpqf"])
        if ON_MYSQL:
            self.assertEqual(sql_names(dsq.FAST_YOUNG_ORIGINAL), got)

    def test_fast_and_young_outsider_sees_only_visible_points(self):
        self.assertEqual(names(ypq.fast_and_young_targets(user=self.outsider)), ["2026dpqe"])
        self.assertEqual(names(ypq.fast_and_young_targets(user=self.member)), ["2026dpqe", "2026dpqf"])

    def test_new_two_days_sql_rows_for_viewer_who_sees_all(self):
        got = names(ypq.new_transients_last_two_days(user=self.staff))
        self.assertEqual(got, ["2026dpqe", "2026dpqf"])
        if ON_MYSQL:
            self.assertEqual(sql_names(dsq.NEW_TWO_DAYS_ORIGINAL), got)

    def test_new_two_days_outsider_sees_only_visible_points(self):
        self.assertEqual(names(ypq.new_transients_last_two_days(user=self.outsider)), ["2026dpqe"])
        self.assertEqual(names(ypq.new_transients_last_two_days(user=self.member)), ["2026dpqe", "2026dpqf"])


class InterestingNewTargetsTests(_Fixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # The saved SQL hard-codes status_id = 1.
        TransientStatus.objects.get_or_create(pk=1, defaults={"name": "New", **audit_fields(cls.staff)})
        cls.bright = cls.transient("2026dpqi", status_id=1, mw_ebv=0.1)
        cls.attach_host(cls.bright)
        cls.points(cls.bright, [(3, 16.5, 0.05, None, None)])
        cls.hidden = cls.transient("2026dpqj", status_id=1, mw_ebv=0.1)  # bright only in a private set
        cls.attach_host(cls.hidden)
        cls.points(cls.hidden, [(3, 18.5, 0.05, None, None)])
        cls.points(cls.hidden, [(2, 16.0, 0.05, None, None)], private=True)
        cls.nearby = cls.transient("2026dpqk", status_id=1, mw_ebv=0.1)  # faint, close to a z=0.005 host
        cls.attach_host(cls.nearby, redshift=0.005, offset=0.01)
        cls.points(cls.nearby, [(3, 19.0, 0.05, None, None)])
        cls.faint = cls.transient("2026dpql", status_id=1, mw_ebv=0.1)
        cls.attach_host(cls.faint)
        cls.points(cls.faint, [(3, 19.0, 0.05, None, None)])
        cls.dusty = cls.transient("2026dpqm", status_id=1, mw_ebv=0.9)
        cls.attach_host(cls.dusty)
        cls.points(cls.dusty, [(3, 16.0, 0.05, None, None)])
        cls.hostless = cls.transient("2026dpqn", status_id=1, mw_ebv=0.1)
        cls.points(cls.hostless, [(3, 16.0, 0.05, None, None)])

    @classmethod
    def attach_host(cls, transient, redshift=0.05, offset=0.0):
        host = Host.objects.create(
            ra=transient.ra + offset, dec=transient.dec, name="host-" + transient.name,
            redshift=redshift, **audit_fields(cls.staff))
        Transient.objects.filter(pk=transient.pk).update(host=host)

    def test_viewer_who_sees_all_gets_the_sql_rows(self):
        got = names(ypq.interesting_new_targets(user=self.staff))
        self.assertEqual(got, ["2026dpqi", "2026dpqj", "2026dpqk"])
        if ON_MYSQL:
            self.assertEqual(sql_names(dsq.INTERESTING_ORIGINAL), got)

    def test_outsider_sees_only_visible_points(self):
        self.assertEqual(names(ypq.interesting_new_targets(user=self.outsider)), ["2026dpqi", "2026dpqk"])
        self.assertEqual(names(ypq.interesting_new_targets(user=self.member)), ["2026dpqi", "2026dpqj", "2026dpqk"])

    def test_nearby_branch_needs_a_visible_point(self):
        TransientPhotData.objects.filter(photometry__transient=self.nearby).update(
            photometry=self.points(self.nearby, [], private=True))
        self.assertNotIn("2026dpqk", names(ypq.interesting_new_targets(user=self.outsider)))
        self.assertIn("2026dpqk", names(ypq.interesting_new_targets(user=self.member)))


class SpectrumColumnTests(SimpleTestCase):
    def test_best_spectrum_id_is_not_a_readable_column(self):
        self.assertIn("best_spectrum_id", pqs.DENIED_COLUMNS)
        self.assertEqual(pqs.disallowed_columns("SELECT t.name, t.Best_Spectrum_ID FROM yse_app_transient t"),
                         ["best_spectrum_id"])
        self.assertEqual(pqs.disallowed_columns("SELECT t.name FROM yse_app_transient t"), [])


class SpectrumColumnViewerTests(TestCase):
    def test_refused_for_scoped_viewers_only(self):
        member = create_test_user("dpq720_plain", is_staff=False)
        sql = "SELECT t.name FROM YSE_App_transient t WHERE t.best_spectrum_id IS NOT NULL"
        self.assertIn("best_spectrum_id", pqs.page_query_table_refusal(sql, member))
        self.assertIsNone(pqs.page_query_table_refusal(sql, None))
        with override_settings(DATA_SERVICE_READER_USERNAMES=["dpq720_plain"]):
            self.assertIsNone(pqs.page_query_table_refusal(sql, member))

    def test_select_star_result_drops_the_column_for_scoped_viewers(self):
        member = create_test_user("dpq720_plain2", is_staff=False)
        fields, rows = ["id", "name", "best_spectrum_id"], [(1, "a", 7)]
        sql = "SELECT * FROM YSE_App_transient"
        self.assertEqual(pqs.selection_only_result(sql, member, fields, rows), (["id", "name"], [(1, "a")]))
        self.assertEqual(pqs.selection_only_result(sql, None, fields, rows), (fields, rows))


class SwitchCommandTests(TestCase):
    RAW_SQL = ("SELECT t.name FROM YSE_App_transient t WHERE EXISTS "
               "(SELECT 1 FROM YSE_App_transientphotdata pd WHERE pd.mag < 30)")

    @classmethod
    def setUpTestData(cls):
        from explorer.models import Query

        cls.user = create_test_user("dpq720_owner", is_staff=False)
        cls.known = Query.objects.create(
            title="Fast & Young Target Search", sql=cls.RAW_SQL, description="", snapshot=False,
            created_by_user=cls.user)
        cls.other = Query.objects.create(
            title="Some Other Section", sql=cls.RAW_SQL, description="", snapshot=False,
            created_by_user=cls.user)
        cls.meta = Query.objects.create(
            title="Plain", sql="SELECT name FROM YSE_App_transient", description="", snapshot=False,
            created_by_user=cls.user)
        cls.sections = {q.title: UserQuery.objects.create(user=cls.user, query=q, **audit_fields(cls.user))
                        for q in (cls.known, cls.other, cls.meta)}

    def run_command(self, *args):
        out = StringIO()
        call_command("switch_dashboard_queries", *args, stdout=out)
        return out.getvalue()

    def section(self, title):
        return UserQuery.objects.get(pk=self.sections[title].pk)

    def test_default_mapping_names_registered_queries(self):
        for name in DEFAULT_MAPPING.values():
            self.assertIn(name, ypq.python_query_reg.all)

    def test_dry_run_is_the_default_and_lists_unmapped_queries(self):
        text = self.run_command()
        self.assertIn("SWITCH", text)
        self.assertIn("UNMAPPED   SQL %d 'Some Other Section'" % self.other.pk, text)
        self.assertNotIn("Plain", text)  # reads only metadata: still allowed
        self.assertEqual(self.section("Fast & Young Target Search").query_id, self.known.pk)

    def test_apply_switches_mapped_sections_and_keeps_the_sql(self):
        from explorer.models import Query

        with tempfile.TemporaryDirectory() as tmp:
            text = self.run_command("--apply", "--backup-dir", tmp)
        row = self.section("Fast & Young Target Search")
        self.assertIsNone(row.query_id)
        self.assertEqual(row.python_query, "fast_and_young_targets")
        self.assertEqual(self.section("Some Other Section").query_id, self.other.pk)
        self.assertEqual(self.section("Plain").query_id, self.meta.pk)
        self.assertTrue(Query.objects.filter(pk=self.known.pk).exists())
        self.assertIn("Switched 1 section", text)

    def test_map_argument_and_file_cover_other_queries(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_command("--apply", "--backup-dir", tmp, "--map",
                             "Some Other Section=interesting_new_targets")
            self.assertEqual(self.section("Some Other Section").python_query, "interesting_new_targets")
            path = tmp + "/map.json"
            with open(path, "w") as fh:
                json.dump({str(self.meta.pk): "new_transients_last_two_days"}, fh)
            self.run_command("--apply", "--backup-dir", tmp, "--mapping-file", path)
        self.assertEqual(self.section("Plain").python_query, "new_transients_last_two_days")

    def test_unknown_python_name_is_an_error(self):
        with self.assertRaises(CommandError):
            self.run_command("--map", "Plain=no_such_query")

    def test_revert_restores_the_sql_sections(self):
        import glob

        with tempfile.TemporaryDirectory() as tmp:
            self.run_command("--apply", "--backup-dir", tmp)
            backup = glob.glob(tmp + "/dashboard_sections_switch_*.json")[0]
            self.run_command("--revert", backup, "--apply")
        row = self.section("Fast & Young Target Search")
        self.assertEqual(row.query_id, self.known.pk)
        self.assertIsNone(row.python_query)
