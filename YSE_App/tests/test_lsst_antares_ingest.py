"""
Rubin/LSST photometry ingest from ANTARES (``Query_LSST.AntaresLSST``, issue #224).

``antares_client`` is mocked throughout: a fake locus carries the light-curve
columns and alert properties the real API returns (taken from the client
repository's LSST fixtures), and ``cone_search`` is patched to yield it.
"""

from __future__ import annotations

import configparser
import datetime
import importlib
import math
import sys
import types
from unittest import mock

import pandas as pd
from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from YSE_App.common.bandpassdict import bandpassdict
from YSE_App.common.filter_display import (
    plot_legend_label,
    telescope_display_symbol,
)
from YSE_App.common.tns_photometry_map import resolve_tns_photometry
from YSE_App.brokers import antares as antares_provider
from YSE_App.data_ingest import Query_LSST
from YSE_App.models import (
    DataQuality,
    FollowupStatus,
    Instrument,
    ObservationGroup,
    Observatory,
    PhotometricBand,
    Telescope,
    TransientFollowup,
    TransientPhotData,
    TransientPhotometry,
)
from YSE_App.tests.fixtures_minimal import (
    create_minimal_transient,
    create_test_user,
    ensure_transient_statuses,
)

CRON_PATH = "YSE_App.data_ingest.Query_LSST.AntaresLSST"

# Values from antares-client test/data/api_responses/lsst-loci-ANT2025uns34defs98p*.json
LSST_RA, LSST_DEC = 57.03204245956507, -49.73624312694643
LSST_MJD = 61004.304272505644


class FakeAlert:
    def __init__(self, alert_id, mjd, properties):
        self.alert_id = alert_id
        self.mjd = mjd
        self.properties = properties


class FakeLocus:
    """Duck-typed ``antares_client.models.Locus``."""

    def __init__(self, locus_id, ra, dec, lightcurve, alerts=None, properties=None):
        self.locus_id = locus_id
        self.ra = ra
        self.dec = dec
        self.lightcurve = lightcurve
        self.alerts = alerts or []
        self.properties = properties if properties is not None else {}
        self.tags = []


def _lc_row(alert_id, survey, mjd, band, mag, magerr, maglim=None):
    return {
        "time": "2025-11-24 07:18:09",
        "alert_id": alert_id,
        "ant_mjd": mjd,
        "ant_survey": survey,
        "ant_ra": LSST_RA,
        "ant_dec": LSST_DEC,
        "ant_passband": band,
        "ant_mag": mag,
        "ant_magerr": magerr,
        "ant_maglim": maglim if maglim is not None else mag,
    }


def make_lsst_locus(extra_rows=()):
    rows = [
        # ZTF candidate and ZTF upper limit on the same locus: must be ignored
        _lc_row("ztf_candidate:1", 1, LSST_MJD - 30.0, "g", 19.1, 0.2),
        _lc_row("ztf_upper_limit:2", 2, LSST_MJD - 29.0, "R", math.nan, math.nan, 20.1),
        # LSST detections
        _lc_row("lsst:1", 4, LSST_MJD, "z", 22.547937138954403, 0.19186562452748476),
        _lc_row("lsst:2", 4, LSST_MJD + 0.02, "r", 21.9, 0.1),
        _lc_row("lsst:3", 4, LSST_MJD + 1.0, "i", 21.5, 0.08),
        # LSST upper limit (NaN mag) and an unknown passband: skipped
        _lc_row("lsst:4", 4, LSST_MJD + 2.0, "g", math.nan, math.nan, 23.5),
        _lc_row("lsst:5", 4, LSST_MJD + 2.1, "x", 21.0, 0.1),
    ]
    rows.extend(extra_rows)
    alerts = [
        FakeAlert("lsst:1", LSST_MJD, {"lsst_diaSource_pixelFlags": False, "lsst_diaSource_isNegative": False,
                                        "lsst_diaSource_reliability": 0.4717160761356354,
                                        "lsst_diaSource_band": "z"}),
        # bad pixels under the source -> Bad data-quality flag
        FakeAlert("lsst:2", LSST_MJD + 0.02, {"lsst_diaSource_pixelFlags": True, "lsst_diaSource_isNegative": False,
                                               "lsst_diaSource_reliability": 0.9}),
        # ANTARES serialises some booleans as strings
        FakeAlert("lsst:3", LSST_MJD + 1.0, {"lsst_diaSource_pixelFlags": "false", "lsst_diaSource_isNegative": "false",
                                              "lsst_diaSource_reliability": 0.95}),
    ]
    return FakeLocus(
        "ANT2025uns34defs98p",
        LSST_RA,
        LSST_DEC,
        pd.DataFrame(rows),
        alerts=alerts,
        properties={"num_alerts": 3, "survey": {"lsst": {"dia_object_id": ["169650292749500483"]}}},
    )


def make_ztf_only_locus():
    rows = [_lc_row("ztf_candidate:9", 1, LSST_MJD, "g", 18.5, 0.05)]
    return FakeLocus("ANT2020ztfonly", LSST_RA, LSST_DEC, pd.DataFrame(rows),
                     properties={"ztf_object_id": "ZTF25abc", "survey": {"ztf": {"id": ["ZTF25abc"]}}})


def _run_cron(loci):
    """Run the cron with ANTARES mocked to return ``loci`` for every cone search."""
    cron = Query_LSST.AntaresLSST(config=configparser.RawConfigParser())
    with mock.patch.object(antares_provider, "HAS_ANTARES", True), \
            mock.patch.object(antares_provider, "cone_search", side_effect=lambda sc, radius: list(loci), create=True):
        cron.do()
    return cron


class CronRegistrationTests(SimpleTestCase):
    def test_registered_and_well_formed(self):
        from django_cron import CronJobBase, Schedule

        self.assertIn(CRON_PATH, settings.CRON_CLASSES)
        cls = Query_LSST.AntaresLSST
        self.assertTrue(issubclass(cls, CronJobBase))
        self.assertIsInstance(cls.schedule, Schedule)
        self.assertEqual(cls.code, CRON_PATH)
        self.assertEqual(cls.RUN_EVERY_MINS, 60)

    def test_code_is_unique(self):
        # AntaresZTF's code is the one most likely to be copy-pasted (#188).
        self.assertNotEqual(Query_LSST.AntaresLSST.code, "YSE_App.data_ingest.Query_ZTF.AntaresZTF")
        self.assertEqual(settings.CRON_CLASSES.count(CRON_PATH), 1)


class GuardedImportTests(TestCase):
    """The provider module must import, and do() must exit cleanly, without antares_client (#273)."""

    def tearDown(self):
        importlib.reload(antares_provider)

    def test_import_without_antares_client(self):
        with mock.patch.dict(sys.modules, {"antares_client": None, "antares_client.search": None}):
            mod = importlib.reload(antares_provider)
            self.assertFalse(mod.HAS_ANTARES)
            self.assertIsNone(mod.cone_search)
            self.assertFalse(Query_LSST.antares_available())
            cron = Query_LSST.AntaresLSST(config=configparser.RawConfigParser())
            with mock.patch("builtins.print") as fake_print:
                cron.do()  # must not raise
        printed = " ".join(str(c.args[0]) for c in fake_print.call_args_list if c.args)
        self.assertIn("antares_client is not installed", printed)
        self.assertEqual(TransientPhotometry.objects.count(), 0)

    def test_import_with_antares_client_present(self):
        fake_pkg = types.ModuleType("antares_client")
        fake_search = types.ModuleType("antares_client.search")
        fake_search.cone_search = lambda center, radius: []
        fake_search.search = lambda query: []
        fake_search.get_by_id = lambda locus_id: None
        fake_pkg.search = fake_search
        with mock.patch.dict(sys.modules, {"antares_client": fake_pkg, "antares_client.search": fake_search}):
            mod = importlib.reload(antares_provider)
            self.assertTrue(mod.HAS_ANTARES)
            self.assertIs(mod.cone_search, fake_search.cone_search)
            self.assertTrue(Query_LSST.antares_available())


class ConfigTests(SimpleTestCase):
    def test_defaults_without_section(self):
        cfg = Query_LSST.LSSTIngestConfig.from_config(configparser.RawConfigParser())
        self.assertEqual(cfg.survey_id, 4)
        self.assertEqual(cfg.cone_radius_arcsec, 2.0)
        self.assertEqual(cfg.max_days, 30.0)
        self.assertEqual(cfg.statuses, ["New", "Following", "Watch", "FollowupRequested", "Interesting"])
        self.assertEqual(cfg.max_transients, 500)
        self.assertEqual(cfg.max_dec, 32.0)
        self.assertEqual(cfg.mjd_match_min, 0.0005)
        self.assertEqual(cfg.min_reliability, 0.0)
        self.assertTrue(cfg.use_alert_flags)

    def test_reads_ini_keys(self):
        config = configparser.RawConfigParser()
        config.read_dict({"antares": {
            "lsst_survey_id": "7", "lsst_cone_radius_arcsec": "1.5", "lsst_max_days": "10",
            "lsst_statuses": "New, Following", "lsst_max_transients": "20", "lsst_max_dec": "10",
            "lsst_mjd_match_min": "0.001", "lsst_min_reliability": "0.5", "lsst_use_alert_flags": "false",
        }})
        cfg = Query_LSST.LSSTIngestConfig.from_config(config)
        self.assertEqual((cfg.survey_id, cfg.cone_radius_arcsec, cfg.max_days), (7, 1.5, 10.0))
        self.assertEqual(cfg.statuses, ["New", "Following"])
        self.assertEqual((cfg.max_transients, cfg.max_dec, cfg.mjd_match_min), (20, 10.0, 0.001))
        self.assertEqual(cfg.min_reliability, 0.5)
        self.assertFalse(cfg.use_alert_flags)

    def test_bad_value_falls_back_to_defaults(self):
        config = configparser.RawConfigParser()
        config.read_dict({"antares": {"lsst_survey_id": "four"}})
        cfg = Query_LSST.LSSTIngestConfig.from_config(config)
        self.assertEqual(cfg.survey_id, 4)


class ParseLocusTests(SimpleTestCase):
    def test_parse_keeps_only_finite_lsst_detections(self):
        cfg = Query_LSST.LSSTIngestConfig()
        points = Query_LSST.parse_locus_lightcurve(make_lsst_locus(), cfg)
        self.assertEqual([p["band"] for p in points], ["z", "r", "i"])
        self.assertEqual([p["alert_id"] for p in points], ["lsst:1", "lsst:2", "lsst:3"])
        z = points[0]
        self.assertAlmostEqual(z["mag"], 22.547937138954403)
        self.assertAlmostEqual(z["mag_err"], 0.19186562452748476)
        self.assertAlmostEqual(z["flux"], 10 ** (-0.4 * (z["mag"] - 27.5)))
        self.assertEqual(z["flux_zero_point"], 27.5)
        self.assertFalse(z["forced"])
        self.assertTrue(z["diffim"])
        self.assertEqual(z["obs_date"], Query_LSST.mjd_to_datetime(LSST_MJD))
        self.assertTrue(timezone.is_aware(z["obs_date"]))
        self.assertEqual([p["bad"] for p in points], [False, True, False])

    def test_survey_id_is_configurable(self):
        cfg = Query_LSST.LSSTIngestConfig(survey_id=1)
        points = Query_LSST.parse_locus_lightcurve(make_lsst_locus(), cfg)
        self.assertEqual([p["alert_id"] for p in points], ["ztf_candidate:1"])

    def test_min_reliability_marks_bad(self):
        cfg = Query_LSST.LSSTIngestConfig(min_reliability=0.5)
        points = Query_LSST.parse_locus_lightcurve(make_lsst_locus(), cfg)
        # z has reliability 0.47 -> bad; r bad pixels; i fine
        self.assertEqual([p["bad"] for p in points], [True, True, False])

    def test_alert_flags_can_be_disabled(self):
        cfg = Query_LSST.LSSTIngestConfig(use_alert_flags=False)
        locus = make_lsst_locus()
        locus.alerts = mock.Mock(side_effect=AssertionError("alerts must not be fetched"))
        points = Query_LSST.parse_locus_lightcurve(locus, cfg)
        self.assertEqual([p["bad"] for p in points], [False, False, False])

    def test_is_lsst_locus(self):
        cfg = Query_LSST.LSSTIngestConfig()
        self.assertTrue(Query_LSST.is_lsst_locus(make_lsst_locus(), cfg.survey_id))
        self.assertFalse(Query_LSST.is_lsst_locus(make_ztf_only_locus(), cfg.survey_id))
        # survey block missing, only the light curve says LSST
        bare = make_lsst_locus()
        bare.properties = {}
        self.assertTrue(Query_LSST.is_lsst_locus(bare, cfg.survey_id))

    def test_mjd_roundtrip(self):
        dt = Query_LSST.mjd_to_datetime(LSST_MJD)
        self.assertAlmostEqual(Query_LSST.datetime_to_mjd(dt), LSST_MJD, places=6)


class IngestTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_test_user("admin", is_staff=True, is_superuser=True)
        ensure_transient_statuses(cls.admin)
        cls.transient = create_minimal_transient(cls.admin, name="2025lsst", ra=LSST_RA, dec=LSST_DEC)

    def _lsst_rows(self):
        return TransientPhotData.objects.filter(
            photometry__transient=self.transient, photometry__instrument__name="LSSTCam"
        ).order_by("obs_date")

    def test_ingest_creates_stack_and_rows(self):
        _run_cron([make_ztf_only_locus(), make_lsst_locus()])

        observatory = Observatory.objects.get(name="Cerro Pachón")
        telescope = Telescope.objects.get(name="Rubin Observatory / Simonyi Survey Telescope")
        self.assertEqual(telescope.observatory, observatory)
        self.assertAlmostEqual(telescope.latitude, -30.2446)
        instrument = Instrument.objects.get(name="LSSTCam")
        self.assertEqual(instrument.telescope, telescope)
        self.assertEqual(
            sorted(PhotometricBand.objects.filter(instrument=instrument).values_list("name", flat=True)),
            sorted(["u", "g", "r", "i", "z", "y"]),
        )
        self.assertTrue(ObservationGroup.objects.filter(name="LSST").exists())

        phots = TransientPhotometry.objects.filter(transient=self.transient, instrument=instrument)
        self.assertEqual(phots.count(), 1)
        phot = phots.get()
        self.assertEqual(phot.obs_group.name, "LSST")
        self.assertEqual(phot.reference, "ANTARES ANT2025uns34defs98p")
        self.assertTrue(phot.groups.filter(name="Public").exists())

        rows = list(self._lsst_rows())
        self.assertEqual([r.band.name for r in rows], ["z", "r", "i"])
        z = rows[0]
        self.assertAlmostEqual(z.mag, 22.547937138954403)
        self.assertAlmostEqual(z.mag_err, 0.19186562452748476)
        self.assertEqual(z.obs_date, Query_LSST.mjd_to_datetime(LSST_MJD))
        self.assertAlmostEqual(z.date_to_mjd(), LSST_MJD, places=6)
        self.assertTrue(z.diffim)
        self.assertFalse(z.forced)
        self.assertFalse(z.discovery_point)
        self.assertEqual(z.flux_zero_point, 27.5)
        self.assertEqual(list(z.data_quality.values_list("name", flat=True)), [])
        self.assertEqual(list(rows[1].data_quality.values_list("name", flat=True)), ["Bad"])
        self.assertEqual(list(rows[2].data_quality.values_list("name", flat=True)), [])
        # nothing from the ZTF-only locus or the ZTF rows of the mixed locus
        self.assertFalse(TransientPhotData.objects.filter(photometry__instrument__name="ZTF-Cam").exists())
        self.assertEqual(TransientPhotData.objects.count(), 3)
        # recent_mag ignores the Bad point and reports the newest good one
        self.assertEqual(self.transient.recent_mag(), "21.50")

    def test_rerun_dedupes_and_appends_new_points(self):
        _run_cron([make_lsst_locus()])
        self.assertEqual(self._lsst_rows().count(), 3)
        # same visit, MJD jittered well inside the match window
        jitter = Query_LSST.LSSTIngestConfig().mjd_match_min / 10.0
        _run_cron([make_lsst_locus(extra_rows=[_lc_row("lsst:1b", 4, LSST_MJD + jitter, "z", 22.55, 0.19)])])
        self.assertEqual(self._lsst_rows().count(), 3)
        self.assertEqual(TransientPhotometry.objects.filter(transient=self.transient).count(), 1)
        # a genuinely new visit lands
        _run_cron([make_lsst_locus(extra_rows=[_lc_row("lsst:6", 4, LSST_MJD + 3.0, "y", 21.2, 0.12)])])
        self.assertEqual(self._lsst_rows().count(), 4)
        self.assertEqual(self._lsst_rows().last().band.name, "y")
        # one Bad row and one DataQuality flag row, however often we run
        self.assertEqual(DataQuality.objects.filter(name="Bad").count(), 1)

    def test_same_visit_in_different_bands_is_kept(self):
        rows = [
            _lc_row("lsst:1", 4, LSST_MJD, "g", 22.0, 0.1),
            _lc_row("lsst:2", 4, LSST_MJD, "r", 21.5, 0.1),
        ]
        _run_cron([FakeLocus("ANT1", LSST_RA, LSST_DEC, pd.DataFrame(rows))])
        self.assertEqual(sorted(r.band.name for r in self._lsst_rows()), ["g", "r"])

    def test_broker_error_on_one_transient_does_not_stop_the_run(self):
        other = create_minimal_transient(self.admin, name="2025other", ra=10.0, dec=-10.0)
        calls = {"n": 0}

        def flaky(sc, radius):
            calls["n"] += 1
            if abs(sc.ra.deg - 10.0) < 1e-6:
                raise ConnectionError("broker down")
            return [make_lsst_locus()]

        cron = Query_LSST.AntaresLSST(config=configparser.RawConfigParser())
        with mock.patch.object(antares_provider, "HAS_ANTARES", True), \
                mock.patch.object(antares_provider, "cone_search", side_effect=flaky, create=True):
            cron.do()
        self.assertEqual(calls["n"], 2)
        self.assertEqual(self._lsst_rows().count(), 3)
        self.assertFalse(TransientPhotData.objects.filter(photometry__transient=other).exists())

    def test_no_admin_user_skips_cleanly(self):
        with mock.patch.object(Query_LSST, "get_audit_user", return_value=None):
            _run_cron([make_lsst_locus()])
        self.assertEqual(TransientPhotData.objects.count(), 0)

    def test_get_lsst_photometry_dict_shape(self):
        with mock.patch.object(antares_provider, "HAS_ANTARES", True), \
                mock.patch.object(antares_provider, "cone_search", return_value=[make_lsst_locus()], create=True):
            d = Query_LSST.getLSSTPhotometry_ANTARES(LSST_RA, LSST_DEC)
        self.assertEqual(d["instrument"], "LSSTCam")
        self.assertEqual(d["obs_group"], "LSST")
        self.assertEqual(len(d["photdata"]), 3)
        point = next(iter(d["photdata"].values()))
        self.assertEqual(set(point) >= {"obs_date", "band", "mag", "mag_err", "flux", "flux_err", "diffim"}, True)


class SelectTransientsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_test_user("admin", is_staff=True, is_superuser=True)
        ensure_transient_statuses(cls.admin)

    def test_selection_rule(self):
        cfg = Query_LSST.LSSTIngestConfig(max_days=30, max_dec=32.0)
        recent_new = create_minimal_transient(self.admin, name="recent-new", dec=-20.0)
        create_minimal_transient(self.admin, name="north", dec=60.0)
        ignored = create_minimal_transient(self.admin, name="ignored", status_name="Ignore", dec=-20.0)
        old = create_minimal_transient(self.admin, name="old-following", status_name="Following", dec=-20.0)
        old_with_followup = create_minimal_transient(self.admin, name="old-followup", status_name="Ignore", dec=-20.0)
        long_ago = timezone.now() - datetime.timedelta(days=200)
        for t in (old, old_with_followup):
            type(t).objects.filter(pk=t.pk).update(modified_date=long_ago, created_date=long_ago, disc_date=long_ago)
        status, _ = FollowupStatus.objects.get_or_create(
            name="Requested", defaults={"created_by": self.admin, "modified_by": self.admin}
        )
        TransientFollowup.objects.create(
            transient=old_with_followup, status=status,
            valid_start=timezone.now() - datetime.timedelta(days=1),
            valid_stop=timezone.now() + datetime.timedelta(days=5),
            created_by=self.admin, modified_by=self.admin,
        )
        selected = {t.name for t in Query_LSST.select_transients(cfg)}
        self.assertEqual(selected, {"recent-new", "old-followup"})
        self.assertNotIn(ignored.name, selected)
        # most recently modified first, then the cap applies
        capped = Query_LSST.select_transients(Query_LSST.LSSTIngestConfig(max_transients=1))
        self.assertEqual([t.pk for t in capped], [recent_new.pk])


class DisplayIntegrationTests(SimpleTestCase):
    """The light-curve plot needs no code change to label/colour Rubin points."""

    def test_legend_label_and_symbol(self):
        self.assertEqual(
            plot_legend_label("g", instrument_name="LSSTCam",
                              telescope_name="Rubin Observatory / Simonyi Survey Telescope"),
            "LSST g",
        )
        self.assertEqual(plot_legend_label("y", instrument_name="LSSTCam", telescope_name=None), "LSST y")
        self.assertEqual(telescope_display_symbol("LSSTCam", None), "plus")

    def test_bandpassdict_has_lsst_bands(self):
        for band in "ugrizy":
            self.assertEqual(bandpassdict["Band: LSSTCam - %s" % band], "lsst%s" % band)

    def test_tns_instrument_map(self):
        r = resolve_tns_photometry("LSSTCam", "g")
        self.assertEqual(r.instrument, "LSSTCam")
        self.assertEqual(r.band, "g")
        self.assertEqual(r.obs_group_hint, "LSST")
