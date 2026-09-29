"""Per-transient photometry statistics (``TransientPhotStat``, #268).

Pins the detection and upper-limit rules (a magnitude and no data-quality
flag; the light-curve plot's ``flux + 3 flux_err`` limit), the derived
numbers (first / last / peak, mean, faintest, deepest limit, rise and decay
rates, time to non-detection, per-band JSON), the ``rebuild_photstats``
command, the incremental updates through the ``TransientPhotData`` signals
and ``deferred_updates()``, the read-only API and the surfaces that show
the stored values (detail-page summary block, ``TransientTable`` Peak Mag
column and its ordering).
"""

import datetime
import json
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from YSE_App.models import (
    DataQuality,
    PhotometricBand,
    Transient,
    TransientPhotData,
    TransientPhotometry,
    TransientPhotStat,
)
from YSE_App.models.phot_stat_models import MJD_EPOCH, datetime_to_mjd, mjd_to_datetime
from YSE_App.services import photstat
from YSE_App.table_utils import TransientTable, annotate_dashboard_transient_fields
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)


def mjd_dt(mjd):
    return MJD_EPOCH + datetime.timedelta(days=float(mjd))


def add_point(photometry, band, mjd, *, mag=None, mag_err=None, flux=None, flux_err=None,
              zp=None, bad=False, user=None):
    row = TransientPhotData.objects.create(
        photometry=photometry, band=band, obs_date=mjd_dt(mjd), mag=mag, mag_err=mag_err,
        flux=flux, flux_err=flux_err, flux_zero_point=zp, **audit_fields(user),
    )
    if bad:
        dq, _ = DataQuality.objects.get_or_create(name="Bad", defaults=audit_fields(user))
        row.data_quality.add(dq)
    return row


def second_band(user, instrument, name="g-photstat"):
    band, _ = PhotometricBand.objects.get_or_create(
        name=name, instrument=instrument,
        defaults={"disp_color": "#00ff00", "disp_symbol": "square", **audit_fields(user)},
    )
    return band


class PhotStatFixture(TestCase):
    """A transient with limits, detections in two bands and a flagged point."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("photstat_user")
        cls.transient = create_minimal_transient(cls.user, name="2026pst")
        cls.obs_group, cls.instrument, cls.r = create_instrument_stack(cls.user, obs_group_name="photstat-group")
        cls.g = second_band(cls.user, cls.instrument)
        cls.photometry = TransientPhotometry.objects.create(
            transient=cls.transient, instrument=cls.instrument, obs_group=cls.obs_group,
            **audit_fields(cls.user),
        )
        u = cls.user
        with photstat.deferred_updates():
            # upper limits before discovery: 3-sigma limit = -2.5 log10(flux + 3 flux_err) + zp
            add_point(cls.photometry, cls.r, 60000.0, flux=5.0, flux_err=20.0, zp=27.5, user=u)   # 22.97
            add_point(cls.photometry, cls.r, 60003.0, flux=2.0, flux_err=10.0, zp=27.5, user=u)   # 23.74 (deepest)
            add_point(cls.photometry, cls.g, 60004.0, flux=8.0, flux_err=30.0, zp=27.5, user=u)   # 22.52 (last before first det)
            # detections, r band: first 60006 @ 19.0, peak 60012 @ 17.5, last 60024 @ 18.7
            add_point(cls.photometry, cls.r, 60006.0, mag=19.0, mag_err=0.1, user=u)
            add_point(cls.photometry, cls.r, 60009.0, mag=18.0, mag_err=0.1, user=u)
            add_point(cls.photometry, cls.r, 60012.0, mag=17.5, mag_err=0.1, user=u)
            add_point(cls.photometry, cls.r, 60018.0, mag=18.1, mag_err=0.1, user=u)
            add_point(cls.photometry, cls.r, 60024.0, mag=18.7, mag_err=0.1, user=u)
            # g band: peak 60011 @ 17.8, last g detection 60020 @ 18.9
            add_point(cls.photometry, cls.g, 60008.0, mag=18.6, mag_err=0.1, user=u)
            add_point(cls.photometry, cls.g, 60011.0, mag=17.8, mag_err=0.1, user=u)
            add_point(cls.photometry, cls.g, 60020.0, mag=18.9, mag_err=0.1, user=u)
            # flagged: would be the brightest and the latest if it counted
            add_point(cls.photometry, cls.r, 60030.0, mag=12.0, mag_err=0.1, bad=True, user=u)
            # an unflagged point with neither mag nor usable flux counts as an observation only
            add_point(cls.photometry, cls.g, 60002.0, user=u)


class ComputeStatsTests(PhotStatFixture):
    def test_values_on_the_fixture(self):
        stat = TransientPhotStat.objects.get(transient=self.transient)
        self.assertEqual(stat.num_obs_global, 12)  # 13 rows minus the flagged one
        self.assertEqual(stat.num_det_global, 8)
        self.assertEqual(stat.num_limits_global, 3)
        self.assertAlmostEqual(stat.first_detected_mjd, 60006.0)
        self.assertAlmostEqual(stat.first_detected_mag, 19.0)
        self.assertEqual(stat.first_detected_band, self.r)
        self.assertAlmostEqual(stat.last_detected_mjd, 60024.0)
        self.assertAlmostEqual(stat.last_detected_mag, 18.7)
        self.assertEqual(stat.last_detected_band, self.r)
        self.assertAlmostEqual(stat.peak_mjd, 60012.0)
        self.assertAlmostEqual(stat.peak_mag, 17.5)
        self.assertEqual(stat.peak_band, self.r)
        self.assertAlmostEqual(stat.faintest_mag, 19.0)
        self.assertAlmostEqual(stat.mean_mag, (19.0 + 18.0 + 17.5 + 18.1 + 18.7 + 18.6 + 17.8 + 18.9) / 8)
        # latest unflagged point of any kind: the last detection (the flagged 60030 row is ignored)
        self.assertAlmostEqual(stat.last_obs_mjd, 60024.0)
        self.assertEqual(stat.last_obs_date, mjd_dt(60024.0))
        self.assertEqual(stat.peak_date, mjd_dt(60012.0))
        # limits
        import math
        deepest = -2.5 * math.log10(2.0 + 3 * 10.0) + 27.5
        self.assertAlmostEqual(stat.deepest_limit, deepest, places=6)
        self.assertAlmostEqual(stat.deepest_limit_mjd, 60003.0)
        self.assertAlmostEqual(stat.last_non_detection_mjd, 60004.0)
        self.assertAlmostEqual(stat.time_to_non_detection, 2.0)
        # rates: rise in the first-detection band (r): (19.0 - 17.5) / (60012 - 60006)
        self.assertAlmostEqual(stat.rise_rate, 1.5 / 6.0)
        # decay in the last-detection band (r): (18.7 - 17.5) / (60024 - 60012)
        self.assertAlmostEqual(stat.decay_rate, 1.2 / 12.0)
        per_band = stat.per_band
        self.assertEqual(set(per_band), {str(self.r.id), str(self.g.id)})
        self.assertEqual(per_band[str(self.g.id)]["n_det"], 3)
        self.assertAlmostEqual(per_band[str(self.g.id)]["peak_mag"], 17.8)
        self.assertAlmostEqual(per_band[str(self.g.id)]["last_mjd"], 60020.0)
        self.assertEqual(per_band[str(self.r.id)]["name"], self.r.name)

    def test_last_detection_matches_the_dashboard_recent_mag_rule(self):
        stat = TransientPhotStat.objects.get(transient=self.transient)
        self.assertEqual("%.2f" % stat.last_detected_mag, self.transient.recent_mag())
        qs = annotate_dashboard_transient_fields(Transient.objects.filter(pk=self.transient.pk))
        row = qs[0]
        self.assertAlmostEqual(row.recent_mag, stat.last_detected_mag)
        self.assertEqual(row.recent_magdate, stat.last_obs_date)

    def test_pure_compute_edge_cases(self):
        P = photstat.PhotPoint
        empty = photstat.compute_stats([])
        self.assertEqual((empty.num_obs_global, empty.num_det_global), (0, 0))
        self.assertIsNone(empty.peak_mag)
        self.assertIsNone(empty.rise_rate)
        # one detection: no rates, no time to non-detection
        one = photstat.compute_stats([P(mjd=60000.0, band_id=1, mag=18.0, mag_err=0.1)])
        self.assertEqual(one.num_det_global, 1)
        self.assertAlmostEqual(one.peak_mag, 18.0)
        self.assertIsNone(one.rise_rate)
        self.assertIsNone(one.decay_rate)
        self.assertIsNone(one.time_to_non_detection)
        # first detection is the peak: no rise rate; last is the peak: no decay rate
        fading = photstat.compute_stats([
            P(mjd=60000.0, band_id=1, mag=17.0), P(mjd=60005.0, band_id=1, mag=18.0),
        ])
        self.assertIsNone(fading.rise_rate)
        self.assertAlmostEqual(fading.decay_rate, 0.2)
        rising = photstat.compute_stats([
            P(mjd=60000.0, band_id=1, mag=18.0), P(mjd=60005.0, band_id=1, mag=17.0),
        ])
        self.assertAlmostEqual(rising.rise_rate, 0.2)
        self.assertIsNone(rising.decay_rate)
        # only limits: deepest limit and last non-detection, nothing else
        limits = photstat.compute_stats([
            P(mjd=60000.0, band_id=1, flux=1.0, flux_err=1.0, flux_zero_point=25.0),
            P(mjd=60001.0, band_id=1, flux=1.0, flux_err=10.0, flux_zero_point=25.0),
        ])
        self.assertEqual(limits.num_det_global, 0)
        self.assertEqual(limits.num_limits_global, 2)
        self.assertAlmostEqual(limits.deepest_limit_mjd, 60000.0)
        self.assertAlmostEqual(limits.last_non_detection_mjd, 60001.0)
        self.assertIsNone(limits.time_to_non_detection)
        # flagged rows never count; non-positive flux + 3 sigma is not a limit
        junk = photstat.compute_stats([
            P(mjd=60000.0, band_id=1, mag=10.0, flagged=True),
            P(mjd=60001.0, band_id=1, flux=-50.0, flux_err=1.0, flux_zero_point=25.0),
        ])
        self.assertEqual(junk.num_obs_global, 1)
        self.assertEqual(junk.num_limits_global, 0)
        self.assertIsNone(junk.deepest_limit)
        # fingerprints are stable and value-sensitive
        self.assertEqual(rising.fingerprint(), photstat.compute_stats([
            P(mjd=60000.0, band_id=1, mag=18.0), P(mjd=60005.0, band_id=1, mag=17.0),
        ]).fingerprint())
        self.assertNotEqual(rising.fingerprint(), fading.fingerprint())

    def test_mjd_helpers_round_trip(self):
        now = timezone.now().replace(microsecond=0)
        self.assertAlmostEqual(datetime_to_mjd(mjd_to_datetime(60000.5)), 60000.5)
        self.assertEqual(mjd_to_datetime(datetime_to_mjd(now)), now)
        self.assertIsNone(datetime_to_mjd(None))
        self.assertIsNone(mjd_to_datetime(None))

    def test_recompute_is_idempotent_and_skips_no_op_writes(self):
        stat = TransientPhotStat.objects.get(transient=self.transient)
        before = stat.last_updated
        again = photstat.recompute(self.transient.id)
        self.assertEqual(again.pk, stat.pk)
        self.assertEqual(again.last_updated, before)
        self.assertEqual(TransientPhotStat.objects.filter(transient=self.transient).count(), 1)

    def test_transient_without_photometry_gets_an_empty_row(self):
        bare = create_minimal_transient(self.user, name="2026bare")
        stat = photstat.recompute(bare.id)
        self.assertEqual(stat.num_obs_global, 0)
        self.assertFalse(stat.has_detections)
        self.assertEqual(stat.per_band, {})
        self.assertIsNone(photstat.recompute(10 ** 9))


class SignalTests(PhotStatFixture):
    def test_saving_a_point_updates_the_row(self):
        add_point(self.photometry, self.r, 60040.0, mag=19.5, mag_err=0.1, user=self.user)
        stat = TransientPhotStat.objects.get(transient=self.transient)
        self.assertAlmostEqual(stat.last_detected_mjd, 60040.0)
        self.assertAlmostEqual(stat.last_detected_mag, 19.5)
        self.assertEqual(stat.num_det_global, 9)
        self.assertAlmostEqual(stat.decay_rate, (19.5 - 17.5) / (60040.0 - 60012.0))

    def test_flagging_and_unflagging_a_point_updates_the_row(self):
        peak = TransientPhotData.objects.get(photometry=self.photometry, mag=17.5)
        dq, _ = DataQuality.objects.get_or_create(name="Bad", defaults=audit_fields(self.user))
        peak.data_quality.add(dq)
        stat = TransientPhotStat.objects.get(transient=self.transient)
        self.assertAlmostEqual(stat.peak_mag, 17.8)  # g-band point is now the brightest
        self.assertEqual(stat.peak_band, self.g)
        self.assertEqual(stat.num_det_global, 7)
        peak.data_quality.remove(dq)
        stat.refresh_from_db()
        self.assertAlmostEqual(stat.peak_mag, 17.5)
        # the reverse side (DataQuality.transientphotdata_set) works too, including clear()
        suspect = DataQuality.objects.create(name="Suspect", **audit_fields(self.user))
        suspect.transientphotdata_set.add(peak)
        stat.refresh_from_db()
        self.assertAlmostEqual(stat.peak_mag, 17.8)
        suspect.transientphotdata_set.clear()
        stat.refresh_from_db()
        self.assertAlmostEqual(stat.peak_mag, 17.5)

    def test_deleting_a_point_updates_the_row_without_creating_one(self):
        TransientPhotData.objects.get(photometry=self.photometry, mag=17.5).delete()
        stat = TransientPhotStat.objects.get(transient=self.transient)
        self.assertAlmostEqual(stat.peak_mag, 17.8)
        # no stat row yet + delete: nothing is created (a Transient cascade may be running)
        other = create_minimal_transient(self.user, name="2026del")
        phot = TransientPhotometry.objects.create(
            transient=other, instrument=self.instrument, obs_group=self.obs_group, **audit_fields(self.user))
        row = add_point(phot, self.r, 60000.0, mag=18.0, mag_err=0.1, user=self.user)
        TransientPhotStat.objects.filter(transient=other).delete()
        row.delete()
        self.assertFalse(TransientPhotStat.objects.filter(transient=other).exists())

    def test_deleting_a_transient_with_photometry_and_a_stat_row_works(self):
        other = create_minimal_transient(self.user, name="2026gone")
        phot = TransientPhotometry.objects.create(
            transient=other, instrument=self.instrument, obs_group=self.obs_group, **audit_fields(self.user))
        add_point(phot, self.r, 60000.0, mag=18.0, mag_err=0.1, user=self.user)
        add_point(phot, self.r, 60001.0, mag=17.0, mag_err=0.1, user=self.user)
        self.assertTrue(TransientPhotStat.objects.filter(transient=other).exists())
        other.delete()
        self.assertFalse(TransientPhotStat.objects.filter(transient_id=other.id).exists())
        # and without a stat row (the FK hazard case)
        third = create_minimal_transient(self.user, name="2026gone2")
        phot = TransientPhotometry.objects.create(
            transient=third, instrument=self.instrument, obs_group=self.obs_group, **audit_fields(self.user))
        add_point(phot, self.r, 60000.0, mag=18.0, mag_err=0.1, user=self.user)
        TransientPhotStat.objects.filter(transient=third).delete()
        third.delete()
        self.assertFalse(Transient.objects.filter(pk=third.id).exists())

    def test_deferred_updates_recompute_once_per_transient(self):
        with mock.patch.object(photstat, "recompute", wraps=photstat.recompute) as spy:
            with photstat.deferred_updates():
                for i in range(5):
                    add_point(self.photometry, self.r, 60050.0 + i, mag=19.0 + 0.1 * i, mag_err=0.1, user=self.user)
                self.assertEqual(spy.call_count, 0)
            self.assertEqual(spy.call_count, 1)
        stat = TransientPhotStat.objects.get(transient=self.transient)
        self.assertEqual(stat.num_det_global, 13)
        self.assertAlmostEqual(stat.last_detected_mjd, 60054.0)

    def test_saving_a_point_costs_a_bounded_number_of_queries(self):
        with CaptureQueriesContext(connection) as ctx:
            add_point(self.photometry, self.r, 60060.0, mag=19.5, mag_err=0.1, user=self.user)
        # INSERT + photometry->transient lookup + stat fetch + points + UPDATE (+ savepoints)
        self.assertLessEqual(len(ctx.captured_queries), 12, [q["sql"][:80] for q in ctx.captured_queries])


class CommandTests(PhotStatFixture):
    def _run(self, *args):
        out = StringIO()
        call_command("rebuild_photstats", *args, stdout=out)
        return out.getvalue()

    def test_rebuild_all_creates_missing_rows_and_reports_counts(self):
        bare = create_minimal_transient(self.user, name="2026cmd")
        TransientPhotStat.objects.filter(transient=bare).delete()
        TransientPhotStat.objects.filter(transient=self.transient).update(phot_hash="stale", peak_mag=1.0)
        out = self._run("--batch-size", "1")
        self.assertIn("processed 2 transient(s); created 1, updated 1, unchanged 0", out)
        self.assertIn("batch 1-1 of 2", out)
        stat = TransientPhotStat.objects.get(transient=self.transient)
        self.assertAlmostEqual(stat.peak_mag, 17.5)
        self.assertTrue(TransientPhotStat.objects.filter(transient=bare).exists())
        # second run: nothing to do
        out = self._run("--quiet")
        self.assertIn("created 0, updated 0, unchanged 2", out)
        self.assertNotIn("batch 1-", out)

    def test_single_transient_and_missing_only(self):
        bare = create_minimal_transient(self.user, name="2026cmd2")
        TransientPhotStat.objects.filter(transient=bare).delete()
        out = self._run("--transient", self.transient.name)
        self.assertIn("processed 1 transient(s); created 0, updated 0, unchanged 1", out)
        self.assertFalse(TransientPhotStat.objects.filter(transient=bare).exists())
        out = self._run("--missing-only")
        self.assertIn("processed 1 transient(s); created 1", out)
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            self._run("--transient", "nope-2026")
        with self.assertRaises(CommandError):
            self._run("--batch-size", "0")

    def test_recompute_many_uses_a_flat_number_of_queries(self):
        ids = [self.transient.id]
        for i in range(4):
            t = create_minimal_transient(self.user, name="2026many%d" % i)
            phot = TransientPhotometry.objects.create(
                transient=t, instrument=self.instrument, obs_group=self.obs_group, **audit_fields(self.user))
            add_point(phot, self.r, 60000.0 + i, mag=18.0 + i * 0.1, mag_err=0.1, user=self.user)
            ids.append(t.id)
        TransientPhotStat.objects.filter(transient_id__in=ids).delete()
        with CaptureQueriesContext(connection) as ctx:
            result = photstat.recompute_many(ids, batch_size=100)
        self.assertEqual((result.processed, result.created), (5, 5))
        self.assertLessEqual(len(ctx.captured_queries), 6, [q["sql"][:80] for q in ctx.captured_queries])


class ApiTests(PhotStatFixture):
    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_list_and_detail(self):
        response = self.client.get("/api/transientphotstats/?transient_name=%s" % self.transient.name)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        rows = data["results"] if isinstance(data, dict) and "results" in data else data
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["transient_name"], self.transient.name)
        self.assertAlmostEqual(row["peak_mag"], 17.5)
        self.assertEqual(row["peak_band_name"], self.r.name)
        self.assertEqual(row["num_det_global"], 8)
        self.assertIn(str(self.g.id), row["per_band"])
        self.assertNotIn("phot_hash", row)
        self.assertNotIn("per_band_json", row)
        detail = self.client.get(row["url"])
        self.assertEqual(detail.status_code, 200)
        self.assertAlmostEqual(detail.json()["rise_rate"], 0.25)

    def test_filters_and_ordering(self):
        other = create_minimal_transient(self.user, name="2026faint")
        phot = TransientPhotometry.objects.create(
            transient=other, instrument=self.instrument, obs_group=self.obs_group, **audit_fields(self.user))
        add_point(phot, self.r, 60000.0, mag=20.5, mag_err=0.1, user=self.user)

        def names(query):
            response = self.client.get("/api/transientphotstats/?" + query)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            rows = data["results"] if isinstance(data, dict) and "results" in data else data
            return [r["transient_name"] for r in rows]

        self.assertEqual(names("peak_mag_lte=18"), [self.transient.name])
        self.assertEqual(names("peak_mag_gte=20"), [other.name])
        self.assertEqual(names("num_det_gte=2"), [self.transient.name])
        self.assertEqual(names("ordering=peak_mag"), [self.transient.name, other.name])
        self.assertEqual(names("ordering=-peak_mag"), [other.name, self.transient.name])
        # the transients endpoint filters and orders on the same columns
        response = self.client.get("/api/transients/?peak_mag_lte=18")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        rows = data["results"] if isinstance(data, dict) and "results" in data else data
        self.assertEqual([r["name"] for r in rows], [self.transient.name])
        response = self.client.get("/api/transients/?ordering=-peak_mag&name=%s" % other.name)
        self.assertEqual(response.status_code, 200)

    def test_read_only_and_login_required(self):
        response = self.client.post("/api/transientphotstats/", {"peak_mag": 1}, content_type="application/json")
        self.assertIn(response.status_code, (403, 405))
        anonymous = Client()
        response = anonymous.get("/api/transientphotstats/")
        self.assertIn(response.status_code, (401, 403))


class SurfaceTests(PhotStatFixture):
    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_detail_page_shows_the_stored_peak_and_rates(self):
        response = self.client.get(reverse("transient_detail", kwargs={"slug": self.transient.slug}))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="photstat-peak-row"', html)
        self.assertIn("17.50", html)
        self.assertIn("8 detections", html)
        self.assertIn("3 upper limits", html)
        self.assertIn("Rise 0.250 mag/day", html)
        self.assertIn("Decay 0.100 mag/day", html)
        self.assertIn("Last limit 2.0 d before first detection", html)

    def test_detail_page_without_a_stat_row_still_renders(self):
        TransientPhotStat.objects.filter(transient=self.transient).delete()
        response = self.client.get(reverse("transient_detail", kwargs={"slug": self.transient.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'id="photstat-peak-row"', response.content)

    def test_transient_table_peak_mag_column_and_ordering(self):
        other = create_minimal_transient(self.user, name="2026faint2")
        phot = TransientPhotometry.objects.create(
            transient=other, instrument=self.instrument, obs_group=self.obs_group, **audit_fields(self.user))
        add_point(phot, self.r, 60000.0, mag=20.5, mag_err=0.1, user=self.user)
        bare = create_minimal_transient(self.user, name="2026nophot")
        qs = Transient.objects.filter(pk__in=[self.transient.pk, other.pk, bare.pk])

        table = TransientTable(qs)
        cells = {row.record.name: row.get_cell("peak_mag") for row in table.rows}
        self.assertEqual(cells[self.transient.name], "17.50")
        self.assertEqual(cells[other.name], "20.50")
        self.assertEqual(cells[bare.name], table.columns["peak_mag"].default)

        # ascending: brightest peak first (rows without a stat row sort with NULL, like Last Mag)
        names = [r.record.name for r in TransientTable(qs, order_by="peak_mag").rows]
        self.assertLess(names.index(self.transient.name), names.index(other.name))
        names = [r.record.name for r in TransientTable(qs, order_by="-peak_mag").rows]
        self.assertEqual(names[:2], [other.name, self.transient.name])

        # the dashboard queryset carries the annotation: no per-row query for the column
        annotated = annotate_dashboard_transient_fields(qs)
        table = TransientTable(annotated)
        with CaptureQueriesContext(connection) as ctx:
            list(row.get_cell("peak_mag") for row in table.rows)
        self.assertLessEqual(len(ctx.captured_queries), 2)

    def test_dashboard_section_renders_and_sorts_the_column(self):
        following = self.transient.status.__class__.objects.get(name="Following")
        Transient.objects.filter(pk=self.transient.pk).update(status=following)
        other = create_minimal_transient(self.user, name="2026faint3", status_name="Following")
        phot = TransientPhotometry.objects.create(
            transient=other, instrument=self.instrument, obs_group=self.obs_group, **audit_fields(self.user))
        add_point(phot, self.r, 60000.0, mag=20.5, mag_err=0.1, user=self.user)
        response = self.client.get("/dashboard/section/following/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Peak Mag", body)
        self.assertIn("17.50", body)
        asc = self.client.get("/dashboard/section/following/?followingsort=peak_mag").content.decode()
        desc = self.client.get("/dashboard/section/following/?followingsort=-peak_mag").content.decode()
        self.assertLess(asc.index(self.transient.name), asc.index(other.name))
        self.assertLess(desc.index(other.name), desc.index(self.transient.name))
