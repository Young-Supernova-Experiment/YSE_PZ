"""
Row-for-row equivalence of every SQL rewrite in this repo (issue #332): the
saved-query rewrites in ``YSE_App/queries/dashboard_saved_queries.py`` and
the RawSQL fragments in ``YSE_App/queries/raw_sql.py`` return the same rows,
in the same order, as the text they replace, on a fixture built to exercise
every predicate (data-quality flags, S/N cuts, NULL mags, date windows, tags,
name patterns, statuses, hosts, bands, excluded instrument).

The saved queries use MySQL-only functions (TO_DAYS, CURDATE, ISNULL,
INTERVAL, RADIANS ...) and run only on MySQL/MariaDB (CI); the RawSQL
fragments run on sqlite too where the functions they need exist.

The saved queries count *calendar* days (``TO_DAYS(CURDATE()) - TO_DAYS(obs_date)``),
so a point ``timezone.now() - 7.5 days`` is 7 or 8 calendar days back depending on
the time of day the fixture is built (issue #381: the test failed for every run
between 00:00 and 12:00 UTC). Points meant to sit on a calendar-day boundary are
therefore pinned to noon of the database's own current date minus N days, which
gives the same TO_DAYS difference at any hour.
"""

import datetime

from django.db import connection, connections
from django.db.models.expressions import RawSQL
from django.db.utils import OperationalError
from django.test import TestCase
from django.utils import timezone

from YSE_App.models import (
    DataQuality,
    Host,
    Instrument,
    ObservationGroup,
    PhotometricBand,
    Transient,
    TransientPhotData,
    TransientPhotometry,
    TransientStatus,
    TransientTag,
)
from YSE_App.queries import dashboard_saved_queries as dsq
from YSE_App.queries import raw_sql
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_test_user,
    ensure_transient_statuses,
)

IS_MYSQL = connection.vendor == "mysql"


def _rows(sql):
    with connections["default"].cursor() as cursor:
        cursor.execute(sql.replace("%", "%%"), ())
        return [tuple(_norm(v) for v in row) for row in cursor.fetchall()]


def _norm(v):
    if isinstance(v, float):
        return round(v, 6)
    return v


def _db_today():
    """The database's current date, i.e. what its CURDATE() / CURRENT_DATE sees."""
    with connections["default"].cursor() as cursor:
        cursor.execute("SELECT CURRENT_DATE")
        value = cursor.fetchone()[0]
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value))


class _Fixture:
    """A small population that hits every branch of the rewritten predicates."""

    @classmethod
    def build(cls):
        user = create_test_user("sql_equiv_user")
        audit = audit_fields(user)
        statuses = ensure_transient_statuses(user)
        # The saved queries hard-code ``status_id = 1``; make sure that row exists.
        status1, _ = TransientStatus.objects.get_or_create(id=1, defaults={"name": "New-1", **audit})
        other_status = statuses["Watch"] if statuses["Watch"].id != 1 else statuses["Following"]
        obs_group, gpc1, r_band = create_instrument_stack(user, obs_group_name="equiv-grp")
        # Extra bands/instruments for the rising-transient fragment.
        telescope = gpc1.telescope
        gaia = Instrument.objects.create(name="Gaia-Photometric", telescope=telescope, **audit)
        g_band = PhotometricBand.objects.create(name="g", instrument=gpc1, disp_color="#0f0", disp_symbol="circle", **audit)
        gz_band = PhotometricBand.objects.create(name="g-ZTF", instrument=gpc1, disp_color="#0f0", disp_symbol="circle", **audit)
        gaia_g = PhotometricBand.objects.create(name="g", instrument=gaia, disp_color="#0f0", disp_symbol="circle", **audit)
        i_band = PhotometricBand.objects.create(name="i", instrument=gpc1, disp_color="#f00", disp_symbol="circle", **audit)
        yse_tag, _ = TransientTag.objects.get_or_create(name="YSE", defaults=audit)
        bad_flag, _ = DataQuality.objects.get_or_create(name="Bad", defaults=audit)
        now = timezone.now()
        today = _db_today()

        def calendar_days_ago(n):
            """Noon UTC, ``n`` calendar days before the database's current date: the
            saved queries see exactly ``n`` in TO_DAYS(CURDATE()) - TO_DAYS(obs_date)
            whatever the time of day, unlike ``now - n.5 days`` (issue #381)."""
            return datetime.datetime.combine(today - datetime.timedelta(days=n), datetime.time(12), tzinfo=datetime.timezone.utc)

        def transient(name, *, tagged=True, status=status1, host_z=None, t_z=None, ebv=0.1, spec=None,
                      host_offset_deg=0.001):
            host = Host.objects.create(ra=10.0 + host_offset_deg, dec=10.0, redshift=host_z, **audit)
            t = Transient.objects.create(
                name=name, ra=10.0, dec=10.0, status=status, obs_group=obs_group, host=host,
                redshift=t_z, mw_ebv=ebv, TNS_spec_class=spec, disc_date=now - datetime.timedelta(days=5), **audit)
            if tagged:
                t.tags.add(yse_tag)
            return t

        def phot(t, points, instrument=gpc1):
            """points: (when, band, mag, flux, flux_err, mag_err, flagged); ``when`` is a
            number of days before ``now`` or an explicit datetime (see calendar_days_ago)."""
            p = TransientPhotometry.objects.create(transient=t, instrument=instrument, obs_group=obs_group, **audit)
            for when, band, mag, flux, ferr, merr, flagged in points:
                obs_date = when if isinstance(when, datetime.datetime) else now - datetime.timedelta(days=when)
                pd = TransientPhotData.objects.create(
                    photometry=p, band=band, obs_date=obs_date,
                    mag=mag, flux=flux, flux_err=ferr, mag_err=merr, **audit)
                if flagged:
                    pd.data_quality.add(bad_flag)
            return p

        R = r_band
        # 1. bright, clean, recent, many points: in every result
        t = transient("2025aaa")
        phot(t, [(0.2, R, 18.1, 100, 5, 0.05, False), (1.5, R, 18.3, 90, 5, 0.06, False),
                 (2.5, R, 18.9, 60, 5, 0.1, False), (4.0, R, 19.4, 40, 5, 0.2, False)])
        phot(t, [(6.5, g_band, 17.9, 120, 4, 0.05, False), (0.1, g_band, 18.0, 110, 4, 0.05, False)])
        # 2. brightest point is flagged bad -> Mag-Limited must use the next one (>18.6: excluded)
        t = transient("2025aab")
        phot(t, [(1.0, R, 17.0, 500, 10, 0.02, True), (2.0, R, 18.8, 50, 5, 0.1, False), (3.0, R, 19.0, 45, 5, 0.1, False)])
        # 3. bright but low S/N (flux/flux_err <= 3): excluded from Mag-Limited, included in Fast&Young via mag_err
        t = transient("2025aac")
        phot(t, [(0.5, R, 17.5, 30, 20, 0.15, False), (1.5, R, 17.6, 30, 20, 0.15, False), (2.5, R, 17.7, 30, 20, 0.15, False)])
        # 4. not YSE-tagged: excluded from Mag-Limited only
        t = transient("2025aad", tagged=False)
        phot(t, [(0.5, R, 16.5, 900, 10, 0.02, False), (1.2, R, 16.7, 800, 10, 0.02, False), (2.2, R, 16.9, 700, 10, 0.02, False)])
        # 5. name does not match 201%/202% patterns
        t = transient("ZTF25abc")
        phot(t, [(0.5, R, 16.0, 900, 10, 0.02, False), (1.0, R, 16.2, 900, 10, 0.02, False), (1.8, R, 16.4, 900, 10, 0.02, False)])
        # 6. old light curve (latest detection 10 days ago): in Mag-Limited, out of Fast&Young / Last Two Days
        t = transient("2019xyz", spec="SN II")
        phot(t, [(10, R, 17.2, 400, 10, 0.03, False), (12, R, 17.4, 400, 10, 0.03, False), (14, R, 17.9, 300, 10, 0.03, False)])
        # 7. recent but only two detections (number_of_detection > 2 fails)
        t = transient("2025aae")
        phot(t, [(0.5, R, 18.0, 100, 5, 0.05, False), (1.5, R, 18.2, 100, 5, 0.05, False)])
        # 8. first detection exactly on the calendar-day boundaries: 7 days ago (< 8: in Fast & Young),
        #    8 days ago (< 8 fails: out of Fast & Young, in New Two Days), 14 days ago (< 15 ok)
        t = transient("2025aaf")
        phot(t, [(0.9, R, 18.5, 100, 5, 0.05, False), (3.5, R, 18.6, 100, 5, 0.05, False), (calendar_days_ago(7), R, 18.7, 100, 5, 0.05, False)])
        t = transient("2025aap")
        phot(t, [(0.5, R, 18.5, 100, 5, 0.05, False), (2.0, R, 18.6, 100, 5, 0.05, False), (calendar_days_ago(8), R, 18.7, 100, 5, 0.05, False)])
        t = transient("2025aag", spec="SN Ia")
        phot(t, [(1.2, R, 18.5, 100, 5, 0.05, False), (5.0, R, 18.6, 100, 5, 0.05, False), (calendar_days_ago(14), R, 18.7, 100, 5, 0.05, False)])
        # 9. NULL mag / NULL mag_err rows; latest usable point exactly 2 calendar days old
        #    (Fast & Young latest < 3: in; New Two Days latest < 2: out)
        t = transient("2025aah")
        phot(t, [(calendar_days_ago(2), R, 18.0, 100, 5, None, False), (2.6, R, None, 10, 5, None, False), (3.5, R, 18.4, 100, 5, 0.5, False), (4.5, R, 18.6, 100, 5, 0.05, False)])
        # 10. Interesting-New branches: faint but nearby host (z<=0.01, within 40 kpc); status other than 1
        t = transient("2025aai", host_z=0.005, host_offset_deg=0.0001)
        phot(t, [(1.0, R, 19.5, 50, 5, 0.1, False), (2.0, R, 19.7, 50, 5, 0.1, False)])
        t = transient("2025aaj", status=other_status)
        phot(t, [(0.5, R, 15.5, 2000, 10, 0.01, False), (1.0, R, 15.7, 2000, 10, 0.01, False), (2.0, R, 15.9, 2000, 10, 0.01, False)])
        t = transient("2025aak", ebv=0.9)
        phot(t, [(0.5, R, 15.5, 2000, 10, 0.01, False), (1.0, R, 15.7, 2000, 10, 0.01, False), (2.0, R, 15.9, 2000, 10, 0.01, False)])
        # 11. two points with the same MIN mag in different bands (Interesting New: both rows)
        t = transient("2025aal")
        phot(t, [(1.0, R, 16.8, 500, 10, 0.02, False), (2.0, g_band, 16.8, 500, 10, 0.02, False), (3.0, R, 17.5, 300, 10, 0.02, False)])
        # 12. rising-transient fragment: g via GPC1 and via Gaia (excluded), g-ZTF, i, several photometry rows
        t = transient("2025aam")
        phot(t, [(0.3, g_band, 18.0, 100, 5, 0.05, False), (1.3, gz_band, 18.4, 80, 5, 0.05, False), (2.3, g_band, 18.9, 60, 5, 0.06, False)])
        phot(t, [(0.1, gaia_g, 17.0, 300, 5, 0.01, False)], instrument=gaia)
        phot(t, [(0.4, i_band, 18.2, 100, 5, 0.05, False), (0.2, R, 18.1, 100, 5, 0.05, False)])
        # 13. no photometry at all
        transient("2025aan")
        # 14. only flagged points (Mag-Limited: none clean)
        t = transient("2025aao")
        phot(t, [(0.5, R, 16.0, 900, 10, 0.02, True), (1.0, R, 16.1, 900, 10, 0.02, True), (2.0, R, 16.2, 900, 10, 0.02, True)])
        return user


class SavedQueryRewriteEquivalenceTests(TestCase):
    """MySQL only: the saved texts are MySQL dialect."""

    @classmethod
    def setUpTestData(cls):
        if IS_MYSQL:
            _Fixture.build()

    def setUp(self):
        if not IS_MYSQL:
            self.skipTest("saved Explorer SQL is MySQL dialect; run on the docker MySQL / CI")

    def _assert_same(self, original, rewritten, *, ordered, min_rows):
        old, new = _rows(original), _rows(rewritten)
        self.assertGreaterEqual(len(old), min_rows, "fixture does not exercise the query")
        self.assertEqual(sorted(old), sorted(new))
        if ordered:
            self.assertEqual(old, new)

    def test_magnitude_limited(self):
        self._assert_same(dsq.MAG_LIMITED_ORIGINAL_M2M, dsq.MAG_LIMITED_REWRITE_M2M, ordered=False, min_rows=3)
        names = {r[0] for r in _rows(dsq.MAG_LIMITED_REWRITE_M2M)}
        self.assertIn("2025aaa", names)
        self.assertIn("2019xyz", names)
        for excluded in ("2025aab", "2025aac", "2025aad", "ZTF25abc", "2025aao", "2025aan"):
            self.assertNotIn(excluded, names)

    def test_fast_and_young(self):
        self._assert_same(dsq.FAST_YOUNG_ORIGINAL, dsq.FAST_YOUNG_REWRITE, ordered=True, min_rows=2)
        names = [r[0] for r in _rows(dsq.FAST_YOUNG_REWRITE)]
        self.assertIn("2025aaa", names)
        self.assertIn("2025aaf", names)  # first detection 7 calendar days ago (< 8)
        self.assertIn("2025aah", names)  # latest detection 2 calendar days ago (< 3)
        self.assertNotIn("2025aap", names)  # first detection 8 calendar days ago
        self.assertNotIn("2019xyz", names)
        self.assertNotIn("2025aae", names)

    def test_new_transients_last_two_days(self):
        self._assert_same(dsq.NEW_TWO_DAYS_ORIGINAL, dsq.NEW_TWO_DAYS_REWRITE, ordered=True, min_rows=2)
        names = [r[0] for r in _rows(dsq.NEW_TWO_DAYS_REWRITE)]
        self.assertIn("2025aaa", names)
        self.assertIn("2025aap", names)  # first detection 8 days ago is fine here (< 15)
        self.assertNotIn("2025aah", names)  # latest detection 2 calendar days ago (< 2 fails)
        self.assertNotIn("2025aag", names)  # SN Ia
        self.assertNotIn("2019xyz", names)

    def test_interesting_new_targets(self):
        self._assert_same(dsq.INTERESTING_ORIGINAL.rstrip(";"), dsq.INTERESTING_REWRITE.rstrip(";"), ordered=True, min_rows=3)
        rows = _rows(dsq.INTERESTING_REWRITE.rstrip(";"))
        names = [r[0] for r in rows]
        self.assertIn("2025aad", names)  # bright, status 1
        self.assertIn("2025aai", names)  # faint but nearby host
        self.assertEqual(names.count("2025aal"), 2)  # two points share the MIN mag
        self.assertNotIn("2025aaj", names)  # other status
        self.assertNotIn("2025aak", names)  # mw_ebv >= 0.5


class RawSqlFragmentEquivalenceTests(TestCase):
    """The ORM RawSQL fragments: portable where the functions exist."""

    @classmethod
    def setUpTestData(cls):
        _Fixture.build()

    def _annotated(self, fragment):
        qs = Transient.objects.filter(name__startswith="20").order_by("id").annotate(v=RawSQL(fragment, ()))
        try:
            return [(t.name, _norm(t.v)) for t in qs]
        except OperationalError as exc:  # sqlite without the MySQL functions
            if "no such function" in str(exc):
                self.skipTest(str(exc))
            raise

    def test_recent_mag_fragment(self):
        old, new = self._annotated(raw_sql.RECENT_MAG_SQL_LEGACY), self._annotated(raw_sql.RECENT_MAG_SQL)
        self.assertEqual(old, new)
        by_name = dict(new)
        self.assertEqual(by_name["2025aaa"], 18.0)  # the 0.1-day-old g point, across two photometry rows
        self.assertIsNone(by_name["2025aan"])
        self.assertEqual(by_name["2025aao"], 16.0)  # flagged data included, as before

    def test_days_since_disc_fragment(self):
        if connection.vendor != "mysql":
            self.skipTest("DATEDIFF/CURDATE are MySQL functions")
        old, new = self._annotated(raw_sql.DAYS_SINCE_DISC_SQL_LEGACY), self._annotated(raw_sql.DAYS_SINCE_DISC_SQL)
        self.assertEqual(old, new)
        self.assertTrue(all(v in (4, 5, 6) for _, v in new))

    def test_last_mag_by_band_fragment(self):
        g = "('g-ZTF', 'g', 'V', 'gp', 'g-Sloan')"
        r = "('r-ZTF', 'r', 'rp', 'r-Sloan')"
        i = "('i-ZTF', 'i')"
        cases = [
            ("pd.mag", g, 0), ("pd.mag_err", g, 0), ("UNIX_TIMESTAMP(pd.obs_date)", g, 0),
            ("pd.mag", g, 1), ("UNIX_TIMESTAMP(pd.obs_date)", g, 1),
            ("-2.5*LOG10(pd.flux+3*pd.flux_err)+27.5 as lim", g, 1), ("pd.flux", g, 1), ("pd.flux_err", g, 1),
            ("pd.mag", r, 0), ("pd.mag", r, 1), ("pd.mag", i, 0), ("pd.mag", i, 1),
        ]
        for col, bands, offset in cases:
            with self.subTest(col=col, bands=bands, offset=offset):
                old = self._annotated(raw_sql.LAST_MAG_BY_BAND_SQL_LEGACY % (col, bands, offset))
                new = self._annotated(raw_sql.LAST_MAG_BY_BAND_SQL % (col, bands, offset))
                self.assertEqual(old, new)
        by_name = dict(self._annotated(raw_sql.LAST_MAG_BY_BAND_SQL % ("pd.mag", g, 0)))
        self.assertEqual(by_name["2025aam"], 18.0)  # Gaia g point (17.0) excluded, GPC1 g newest
        self.assertEqual(dict(self._annotated(raw_sql.LAST_MAG_BY_BAND_SQL % ("pd.mag", g, 1)))["2025aam"], 18.4)  # g-ZTF next
        self.assertEqual(dict(self._annotated(raw_sql.LAST_MAG_BY_BAND_SQL % ("pd.mag", i, 0)))["2025aam"], 18.2)
        self.assertIsNone(dict(self._annotated(raw_sql.LAST_MAG_BY_BAND_SQL % ("pd.mag", i, 1)))["2025aam"])
