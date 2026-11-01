"""NED and DESI host redshifts, provenance and the one precedence list (#436, #437).

Fake HTTP sessions and fake clients only: no network in CI.
"""

from __future__ import annotations

from io import StringIO
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from YSE_App.models import ExternalServiceRun, Host
from YSE_App.models.job_models import Job
from YSE_App.services import desi, ned, redshift_catalogs as rc, redshift_precedence as rp, tns_resync
from YSE_App.tests.fixtures_minimal import create_minimal_transient, create_test_user

SERVICE_USERNAME = "redshift_cron"

# Shaped like a real NED::API ConeSearchByPosition response (VOTable 1.5, TABLEDATA).
NED_CONE = """<?xml version="1.0" encoding="UTF-8"?>
<VOTABLE version="1.5" xmlns="http://www.ivoa.net/xml/VOTable/v1.3">
<RESOURCE type="results"><TABLE ID="conesearch" name="ConeSearch">
<GROUP ID="RESPONSE" name="RESPONSE"><PARAM name="QUERY_STATUS" datatype="char" arraysize="*" value="OK"/></GROUP>
<FIELD ID="row" name="No." datatype="int"/>
<FIELD ID="prefname" name="Object Name" datatype="char" arraysize="*"/>
<FIELD ID="ra" name="RA" datatype="double" unit="deg"/>
<FIELD ID="dec" name="Dec" datatype="double" unit="deg"/>
<FIELD ID="z" name="Redshift (z)" datatype="double"/>
<FIELD ID="zflag" name="z flag" datatype="char" arraysize="*"/>
<FIELD ID="ptype" name="Phys Type" datatype="char" arraysize="*"/>
<FIELD ID="sep" name="Separation" datatype="double" unit="arcmin"/>
<DATA><TABLEDATA>
<TR><TD>1</TD><TD>SYNTH GAL A</TD><TD>150.00010</TD><TD>2.00000</TD><TD>0.0300</TD><TD>SLS</TD><TD>G</TD><TD>0.006</TD></TR>
<TR><TD>2</TD><TD>SYNTH PHOTZ</TD><TD>150.00001</TD><TD>2.00000</TD><TD>0.2000</TD><TD>PUN</TD><TD>G</TD><TD>0.001</TD></TR>
<TR><TD>3</TD><TD>SYNTH STAR</TD><TD>150.00002</TD><TD>2.00000</TD><TD>0.0001</TD><TD>SLS</TD><TD>*</TD><TD>0.002</TD></TR>
<TR><TD>4</TD><TD>SYNTH UNSURE</TD><TD>150.00003</TD><TD>2.00000</TD><TD>0.0500</TD><TD>SLS?</TD><TD>G</TD><TD>0.003</TD></TR>
<TR><TD>5</TD><TD>SYNTH GAL B</TD><TD>150.00030</TD><TD>2.00000</TD><TD>0.0310</TD><TD>SLS</TD><TD>G</TD><TD>0.018</TD></TR>
</TABLEDATA></DATA></TABLE></RESOURCE></VOTABLE>"""

NED_REDSHIFTS = """<?xml version="1.0" encoding="UTF-8"?>
<VOTABLE version="1.5" xmlns="http://www.ivoa.net/xml/VOTable/v1.3"><RESOURCE type="results"><TABLE>
<FIELD ID="row" name="No." datatype="int"/>
<FIELD ID="z" name="Redshift (z)" datatype="double"/>
<FIELD ID="unc_z" name="Redshift(z) Unc" datatype="double"/>
<FIELD ID="fiducial" name="Fiducial" datatype="boolean"/>
<DATA><TABLEDATA>
<TR><TD>1</TD><TD>0.0299</TD><TD>0.0009</TD><TD/></TR>
<TR><TD>2</TD><TD>0.0300</TD><TD>0.0001</TD><TD>true</TD></TR>
</TABLEDATA></DATA></TABLE></RESOURCE></VOTABLE>"""

NED_ERROR = """<?xml version="1.0"?><VOTABLE xmlns="http://www.ivoa.net/xml/VOTable/v1.3"><RESOURCE>
<INFO name="QUERY_STATUS" value="ERROR">bad input</INFO></RESOURCE></VOTABLE>"""

DESI_CSV = ("targetid,mean_fiber_ra,mean_fiber_dec,z,zerr,deltachi2\n"
            "39000000000000002,150.0003,2.0,0.0420,0.00002,90.0\n"
            "39000000000000001,150.0001,2.0,0.0410,0.00001,120.0\n")
DESI_ERROR = ('<?xml version="1.0"?><VOTABLE><RESOURCE type="results">'
              '<INFO name="QUERY_STATUS" value="ERROR">Column [x] does not exist.</INFO></RESOURCE></VOTABLE>')


class FakeResponse:
    def __init__(self, text, status=200):
        self.text = text
        self.content = text.encode()
        self.status_code = status


class FakeSession:
    def __init__(self, replies):
        self.replies = replies  # service suffix -> text
        self.calls = []

    def _reply(self, url):
        for key, text in self.replies.items():
            if url.endswith(key):
                return FakeResponse(text)
        return FakeResponse("", 404)

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, dict(params or {})))
        return self._reply(url)

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url, dict(data or {})))
        return self._reply(url)


class FakeCatalogue:
    """Stands in for NEDClient/DESIClient in the job tests: returns a fixed match."""

    def __init__(self, match=None, error=None):
        self.match = match
        self.error = error
        self.requests = 0


def _fake_clients(ned_match=None, desi_match=None, ned_error=None):
    return {"ned": FakeCatalogue(ned_match, ned_error), "desi": FakeCatalogue(desi_match)}


def _patched_matches():
    def fake_ned(ra, dec, client=None, **kw):
        client.requests += 1
        if client.error:
            raise ned.NEDError(client.error)
        return client.match

    def fake_desi(ra, dec, client=None, **kw):
        client.requests += 1
        return client.match

    return mock.patch.multiple("YSE_App.services.redshift_catalogs",
                               ned=mock.Mock(host_match=fake_ned, NEDError=ned.NEDError),
                               desi=mock.Mock(host_match=fake_desi, DESIError=desi.DESIError))


class Obj:
    def __init__(self, z=None, source=None, override=None):
        self.redshift, self.redshift_err, self.redshift_source, self.redshift_override = z, None, source, override


# --- pure logic ------------------------------------------------------------------

class PrecedenceTests(SimpleTestCase):
    def test_rank_order_is_ned_desi_own_catalogue_unknown_tns_photoz(self):
        ranks = [rp.source_rank(s) for s in ("NED", "DESI DR1", "manual", "Prost", "", "TNS", "Prost photo-z")]
        self.assertEqual(ranks, sorted(ranks, reverse=True))
        self.assertEqual(len(set(ranks)), len(ranks))
        self.assertEqual(rp.source_rank("YSE spectrum"), rp.RANK_OWN)
        self.assertEqual(rp.source_rank("ANTARES ned"), rp.RANK_CATALOGUE)  # not NED itself

    def test_override_wins_over_every_catalogue(self):
        for source in ("NED", "DESI DR1", "TNS"):
            self.assertFalse(rp.decide(Obj(0.05, "manual", True), 0.03, source).write)
        self.assertTrue(rp.decide(Obj(0.05, "manual", None), 0.03, "NED").write)  # NULL = no override

    def test_catalogues_replace_lower_ranks_only(self):
        self.assertTrue(rp.decide(Obj(None), 0.03, "TNS").write)
        self.assertTrue(rp.decide(Obj(0.05, None), 0.03, "NED").write)
        self.assertTrue(rp.decide(Obj(0.05, "DESI DR1"), 0.03, "NED").write)
        self.assertFalse(rp.decide(Obj(0.05, "NED"), 0.03, "DESI DR1").write)
        self.assertFalse(rp.decide(Obj(0.05, None), 0.03, "TNS").write)
        self.assertTrue(rp.decide(Obj(0.05, "TNS"), 0.03, "TNS").write)
        self.assertFalse(rp.decide(Obj(0.05, "Prost"), 0.3, "SDSS photo-z").write)
        self.assertEqual(rp.decide(Obj(0.03, "NED"), 0.03, "NED").reason, "unchanged")

    def test_upload_guard(self):
        fields = {"redshift": 0.2, "redshift_err": 0.01, "redshift_source": "Prost", "redshift_override": True}
        self.assertNotIn("redshift", rp.filter_upload_fields(Obj(0.03, "NED"), dict(fields)))
        out = rp.filter_upload_fields(Obj(0.03, None), dict(fields))
        self.assertEqual((out["redshift"], out["redshift_source"]), (0.2, "Prost"))
        self.assertNotIn("redshift_override", out)
        out = rp.filter_upload_fields(Obj(0.03, "TNS"), {"redshift": 0.2})
        self.assertEqual(out["redshift_source"], None)  # unsourced upload: provenance unknown
        self.assertNotIn("redshift", rp.filter_upload_fields(Obj(0.03, "TNS"), {"redshift": 0.03}))
        self.assertNotIn("redshift", rp.filter_upload_fields(Obj(0.03, None), {"redshift": None}))

    def test_best_redshift(self):
        host = mock.Mock(redshift=0.031, redshift_err=None, redshift_source="NED", redshift_override=None,
                         photo_z=0.2, photo_z_err=0.05, photo_z_source="SDSS")
        transient = mock.Mock(redshift=0.03, redshift_err=None, redshift_source="TNS", redshift_override=None,
                              host=host)
        self.assertEqual(rp.best_redshift(transient).origin, "host")
        transient.redshift_override = True
        self.assertEqual(rp.best_redshift(transient).origin, "transient")
        transient.redshift, host.redshift = None, None
        self.assertEqual(rp.best_redshift(transient).origin, "host photo-z")


@override_settings(NED_REQUEST_INTERVAL_SECONDS=0, NED_HOST_MATCH_ARCSEC=3.0)
class NEDClientTests(SimpleTestCase):
    def test_parse_and_filters(self):
        objects = [ned.NEDObject.from_row(r) for r in ned.parse_votable(NED_CONE)]
        self.assertEqual([o.name for o in objects if o.usable], ["SYNTH GAL A", "SYNTH GAL B"])
        self.assertAlmostEqual(objects[0].sep_arcmin, 0.006)

    def test_error_status_raises(self):
        with self.assertRaises(ned.NEDError):
            ned.parse_votable(NED_ERROR)

    def test_host_match_sends_degrees_and_reads_the_fiducial_error(self):
        session = FakeSession({"ConeSearchByPosition": NED_CONE, "RedshiftsOfObject": NED_REDSHIFTS})
        match = ned.host_match(150.0, 2.0, client=ned.NEDClient(session=session))
        self.assertEqual((match.name, match.z, match.z_err), ("SYNTH GAL A", 0.03, 0.0001))
        method, url, params = session.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/NED::API/ConeSearchByPosition"))
        self.assertTrue(params["LON"].endswith("d") and params["LAT"].endswith("d"))  # degrees, not hours
        self.assertEqual(params["Z_CONSTRAINT"], "Available")
        self.assertAlmostEqual(float(params["RADIUS"]), 0.05)  # 3 arcsec in arcmin

    def test_http_error_raises(self):
        with self.assertRaises(ned.NEDError):
            ned.NEDClient(session=FakeSession({})).cone_search(150.0, 2.0, 0.05)

    def test_select_host_ports_getneddata(self):
        objects = [ned.NEDObject.from_row(r) for r in ned.parse_votable(NED_CONE)]
        self.assertEqual(ned.select_host(objects).name, "SYNTH GAL A")
        far = [ned.NEDObject("FAR", 150.0, 2.0, 0.5, "SLS", "G", 3.0)]  # ~1 Mpc away at z=0.5
        self.assertIsNone(ned.select_host(far))


@override_settings(DESI_REQUEST_INTERVAL_SECONDS=0, DESI_TABLE="desi_dr1.zpix", DESI_MIN_DELTACHI2=25)
class DESIClientTests(SimpleTestCase):
    def test_query_and_nearest_match(self):
        session = FakeSession({"/tap/sync": DESI_CSV})
        match = desi.host_match(150.0, 2.0, client=desi.DESIClient(session=session), radius_arcsec=2.0)
        self.assertEqual((match.targetid, match.z, match.z_err), ("39000000000000001", 0.041, 0.00001))
        method, url, data = session.calls[0]
        self.assertEqual((method, data["LANG"], data["FORMAT"]), ("POST", "ADQL", "csv"))
        for clause in ("FROM desi_dr1.zpix", "zwarn = 0", "spectype = 'GALAXY'", "zcat_primary = 't'",
                       "deltachi2 >= 25", "q3c_radial_query"):
            self.assertIn(clause, data["QUERY"])

    def test_table_is_a_setting(self):
        with override_settings(DESI_TABLE="desi_dr2.zpix"):
            self.assertIn("FROM desi_dr2.zpix", desi.cone_query(1.0, 2.0, 1.0))
        with override_settings(DESI_TABLE="x; DROP TABLE y"):
            with self.assertRaises(desi.DESIError):
                desi.cone_query(1.0, 2.0, 1.0)

    def test_tap_error_raises(self):
        with self.assertRaises(desi.DESIError):
            desi.DESIClient(session=FakeSession({"/tap/sync": DESI_ERROR})).redshifts_near(1.0, 2.0, 1.0)


# --- database --------------------------------------------------------------------

@override_settings(API_SERVICE_USERNAMES=[SERVICE_USERNAME], REDSHIFT_CATALOG_SERVICES="ned,desi",
                   DESI_SOURCE_LABEL="DESI DR1", REDSHIFT_CATALOG_CRON_ENABLED=False)
class HostRedshiftTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user(SERVICE_USERNAME, is_staff=True)
        cls.transient = create_minimal_transient(cls.user, name="2026nds", ra=150.0, dec=2.0)

    def setUp(self):
        self.host = Host.objects.create(ra=150.0, dec=2.0, name="synthetic-host",
                                        created_by=self.user, modified_by=self.user)
        self.transient.host = self.host
        self.transient.save()

    def _ned(self, z=0.03, err=0.0001):
        return ned.NEDMatch("SYNTH GAL A", 150.0001, 2.0, z, err, 0.4)

    def _desi(self, z=0.041):
        return desi.DESIMatch("39000000000000001", 150.0001, 2.0, z, 0.00001, 120.0, 0.36)

    def test_ned_fills_host_with_source_and_run_and_is_idempotent(self):
        with _patched_matches():
            first = rc.update_host(self.host, dry_run=False, clients=_fake_clients(self._ned(), self._desi()))
            self.host.refresh_from_db()
            self.assertEqual((self.host.redshift, self.host.redshift_err, self.host.redshift_source),
                             (0.03, 0.0001, "NED"))
            self.assertEqual([r.action for r in first], ["updated", "skipped"])  # DESI ranks below NED
            again = rc.update_host(self.host, dry_run=False, clients=_fake_clients(self._ned(), self._desi()))
        self.assertEqual(again[0].action, "unchanged")
        runs = ExternalServiceRun.objects.filter(service__slug="ned-redshift", target_object_id=self.host.pk)
        self.assertEqual(runs.count(), 2)  # the change, then one re-used no-change run
        self.assertEqual(runs.first().transient, self.transient)

    def test_desi_fills_when_ned_has_nothing(self):
        with _patched_matches():
            rc.update_host(self.host, dry_run=False, clients=_fake_clients(None, self._desi()))
        self.host.refresh_from_db()
        self.assertEqual((self.host.redshift, self.host.redshift_source), (0.041, "DESI DR1"))

    def test_override_is_never_overwritten(self):
        rp.set_override(self.host, 0.05, 0.001)
        with _patched_matches():
            results = rc.update_host(self.host, dry_run=False, clients=_fake_clients(self._ned(), self._desi()))
        self.host.refresh_from_db()
        self.assertEqual((self.host.redshift, self.host.redshift_source, self.host.redshift_override),
                         (0.05, "manual", True))
        self.assertEqual({r.action for r in results}, {"skipped"})

    def test_dry_run_writes_nothing(self):
        with _patched_matches():
            results = rc.update_host(self.host, dry_run=True, clients=_fake_clients(self._ned(), None))
        self.assertIn("would set", results[0].summary())
        self.host.refresh_from_db()
        self.assertIsNone(self.host.redshift)
        self.assertFalse(ExternalServiceRun.objects.exists())

    def test_errors_are_recorded(self):
        with _patched_matches():
            results = rc.update_host(self.host, dry_run=False, clients=_fake_clients(ned_error="HTTP 503"))
        self.assertEqual(results[0].action, "error")
        self.assertTrue(ExternalServiceRun.objects.filter(service__slug="ned-redshift",
                                                          status=ExternalServiceRun.STATUS_FAILED).exists())

    def test_sweep_skips_recently_checked_hosts(self):
        with _patched_matches():
            summary = rc.sweep(clients=_fake_clients(self._ned(), None), budget_seconds=60)
            self.assertEqual((summary["checked"], summary["updated"]), (1, 1))
            summary = rc.sweep(clients=_fake_clients(self._ned(), None), budget_seconds=60)
        self.assertEqual(summary["checked"], 0)

    def test_backfill_command_dry_run(self):
        out = StringIO()
        with _patched_matches(), mock.patch.object(rc, "default_clients",
                                                   return_value=_fake_clients(self._ned(), None)):
            call_command("host_redshifts", stdout=out)
        self.assertIn("would set z None (no source) -> 0.03 (NED)", out.getvalue())
        self.host.refresh_from_db()
        self.assertIsNone(self.host.redshift)

    def test_override_command(self):
        call_command("redshift_override", "--transient", "2026nds", "--z", "0.0312", stdout=StringIO())
        self.transient.refresh_from_db()
        self.assertEqual((self.transient.redshift, self.transient.redshift_override), (0.0312, True))
        call_command("redshift_override", "--transient", "2026nds", "--clear", stdout=StringIO())
        self.transient.refresh_from_db()
        self.assertFalse(self.transient.redshift_override)

    def test_tns_resync_respects_override(self):
        rp.set_override(self.transient, 0.05)
        result = tns_resync.ResyncResult(name=self.transient.name)
        updates = tns_resync._field_changes(self.transient, {"redshift": 0.031}, result)
        self.assertNotIn("redshift", updates)
        self.transient.redshift_override = False
        self.transient.redshift_source = "TNS"
        updates = tns_resync._field_changes(self.transient, {"redshift": 0.031}, result)
        self.assertEqual(updates.get("redshift"), 0.031)

    def test_host_upload_keeps_higher_ranked_redshift(self):
        from YSE_App.data_utils import add_transient_host_util

        Host.objects.filter(pk=self.host.pk).update(redshift=0.03, redshift_source="NED")
        add_transient_host_util({"name": "synthetic-host", "ra": 150.0, "dec": 2.0, "redshift": 0.2,
                                 "redshift_source": "Prost"}, self.transient, self.user)
        self.host.refresh_from_db()
        self.assertEqual((self.host.redshift, self.host.redshift_source), (0.03, "NED"))

    def test_cron_is_registered_and_off_by_default(self):
        from YSE_App.data_ingest.Redshift_Catalogs import RedshiftCatalogSweep

        self.assertIn("YSE_App.data_ingest.Redshift_Catalogs.RedshiftCatalogSweep", settings.CRON_CLASSES)
        self.assertEqual(RedshiftCatalogSweep().do(), "disabled")
        with override_settings(REDSHIFT_CATALOG_CRON_ENABLED=True, JOB_RUNNER_INLINE=False):
            self.assertTrue(RedshiftCatalogSweep().do().startswith("queued job"))
            self.assertIn("already queued", RedshiftCatalogSweep().do())
            self.assertIsNotNone(rc.queue_host_lookup(self.host))
        self.assertEqual(Job.objects.filter(kind=rc.SWEEP_KIND).count(), 1)
        self.assertEqual(Job.objects.filter(kind=rc.LOOKUP_KIND).count(), 1)
