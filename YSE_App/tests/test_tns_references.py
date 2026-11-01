"""#645: TNS photometry in its own header, TNS report ids in the reference, and the backfill."""

from __future__ import annotations

from io import StringIO

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.common.data_references import tns_reference_with_reports, tns_report_id
from YSE_App.common.tns_photometry_map import TNS_OBS_GROUP_NAME
from YSE_App.data_ingest.TNS_uploads import processTNS
from YSE_App.data_utils import add_transient_phot_util
from YSE_App.models import TransientPhotData, TransientPhotometry, TransientSpectrum
from YSE_App.services import data_sharing
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    attach_synthetic_spectrum,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
    ensure_transient_statuses,
)


def _tns_reply(with_ids=True):
    def point(inst, filt, mag, date, rid):
        p = {
            "instrument": {"name": inst}, "filters": {"name": filt}, "flux_unit": {"name": "ABMag"},
            "flux": mag, "fluxerr": 0.1, "obsdate": date, "observer": "someone",
        }
        if with_ids:
            p["report_id"] = rid
        return p

    return {
        "objname": "2026tref", "name_prefix": "AT",
        "photometry": [
            point("ZTF-Cam", "r", 18.5, "2026-06-01 12:00:00", 111),
            point("ZTF-Cam", "r", 18.6, "2026-06-02 12:00:00", 112),
            point("ATLAS-05", "orange-ATLAS", 19.0, "2026-06-02 08:00:00", 222),
        ],
        "spectra": [],
    }


class TnsReportIdTests(TestCase):
    def test_report_id_and_reference(self):
        self.assertEqual(tns_report_id({"report_id": 5}, ("report_id",)), "5")
        self.assertEqual(tns_report_id({"photometry_id": "7"}, ("report_id", "photometry_id")), "7")
        self.assertEqual(tns_report_id({}, ("report_id",)), "")
        self.assertEqual(tns_report_id(None, ("report_id",)), "")
        jd = {"objname": "2026xyz", "name_prefix": "SN"}
        self.assertEqual(tns_reference_with_reports(jd, ["1", "2", "1", ""]), "TNS SN 2026xyz (report 1, 2)")
        self.assertEqual(tns_reference_with_reports(jd, []), "TNS SN 2026xyz")
        self.assertEqual(tns_reference_with_reports({}, ["1"]), "")

    def test_long_reference_keeps_the_ids(self):
        out = tns_reference_with_reports({"objname": "x" * 600}, ["42"])
        self.assertLessEqual(len(out), 512)
        self.assertTrue(out.endswith("(report 42)"))


class TnsPhotometryHeaderTests(TestCase):
    def test_tns_points_use_the_tns_header_with_report_ids(self):
        proc = processTNS()
        proc.clobber = True
        phot, *_ = proc.getTNSPhotometry(_tns_reply())
        headers = [b for b in phot.values() if isinstance(b, dict) and "photdata" in b]
        self.assertEqual(len(headers), 2)
        by_inst = {h["instrument"]: h for h in headers}
        for h in headers:
            self.assertEqual(h["obs_group"], TNS_OBS_GROUP_NAME)
        self.assertEqual(by_inst["ZTF-Cam"]["reference"], "TNS AT 2026tref (report 111, 112)")
        self.assertEqual(by_inst["ATLAS-05"]["reference"], "TNS AT 2026tref (report 222)")

    def test_missing_report_ids_are_tolerated(self):
        proc = processTNS()
        proc.clobber = True
        phot, *_ = proc.getTNSPhotometry(_tns_reply(with_ids=False))
        for h in (b for b in phot.values() if isinstance(b, dict) and "photdata" in b):
            self.assertEqual(h["reference"], "TNS AT 2026tref")


class TnsAndForcedAreDistinctTests(TestCase):
    def test_tns_and_forced_photometry_get_separate_headers(self):
        admin = create_test_user("tns_ref_sep", is_staff=True)
        ensure_transient_statuses(admin)
        Group.objects.get_or_create(name=PUBLIC_COLLABORATION_GROUP_NAME)
        transient = create_minimal_transient(admin, name="2026tref", obs_group_name="sep-survey")
        obs_group, instrument, band = create_instrument_stack(admin, obs_group_name="ZTF")
        forced = attach_synthetic_photometry(admin, transient, obs_group=obs_group, instrument=instrument, band=band)

        photdict = {
            "mjdmatchmin": 0.01, "clobber": True,
            0: {
                "instrument": instrument.name, "obs_group": TNS_OBS_GROUP_NAME,
                "groups": [PUBLIC_COLLABORATION_GROUP_NAME], "reference": "TNS AT 2026tref (report 111)",
                "photdata": {"pt1": {
                    "obs_date": "2026-06-01T12:00:00", "band": band.name, "mag": 18.5, "mag_err": 0.1,
                    "flux": None, "flux_err": None, "forced": None, "diffim": None, "flux_zero_point": None,
                    "data_quality": 0, "discovery_point": 1,
                }},
            },
        }
        _, entries = add_transient_phot_util(photdict, transient, admin, do_photdata=False)
        TransientPhotometry.objects.bulk_create(entries)
        add_transient_phot_util(photdict, transient, admin, do_photdata=True)

        headers = TransientPhotometry.objects.filter(transient=transient)
        self.assertEqual(headers.count(), 2)
        tns = headers.get(obs_group__name=TNS_OBS_GROUP_NAME)
        self.assertEqual(tns.reference, "TNS AT 2026tref (report 111)")
        self.assertEqual(set(tns.groups.values_list("name", flat=True)), {PUBLIC_COLLABORATION_GROUP_NAME})
        self.assertEqual(tns.transientphotdata_set.count(), 1)
        forced.refresh_from_db()
        self.assertFalse(forced.reference)
        self.assertEqual(forced.transientphotdata_set.count(), 12)
        self.assertTrue(data_sharing.is_public_dataset(forced))  # no groups still counts as public


class MarkTnsImportsBackfillTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_test_user("tns_backfill", is_staff=True)
        ensure_transient_statuses(cls.admin)
        cls.tns_transient = create_minimal_transient(cls.admin, name="2026bfil", obs_group_name="bf-survey")
        cls.other_transient = create_minimal_transient(cls.admin, name="not-a-tns-name", obs_group_name="bf-survey")
        og, inst, band = create_instrument_stack(cls.admin, obs_group_name="ZTF")
        cls.forced = attach_synthetic_photometry(cls.admin, cls.tns_transient, obs_group=og, instrument=inst, band=band)
        TransientPhotData.objects.filter(photometry=cls.forced).update(forced=True)
        og2, inst2, band2 = create_instrument_stack(cls.admin, obs_group_name="Pan-STARRS1")
        cls.old_tns = attach_synthetic_photometry(cls.admin, cls.tns_transient, obs_group=og2, instrument=inst2, band=band2)
        cls.spec = attach_synthetic_spectrum(cls.admin, cls.tns_transient)
        cls.unrelated = attach_synthetic_photometry(cls.admin, cls.other_transient)

    def _run(self, *args):
        out = StringIO()
        call_command("mark_tns_imports_public", *args, stdout=out)
        return out.getvalue()

    def _state(self):
        return (
            list(TransientPhotometry.objects.values_list("id", "reference").order_by("id")),
            list(TransientPhotometry.groups.through.objects.values_list("transientphotometry_id", "group_id")),
            list(TransientSpectrum.objects.values_list("id", "reference").order_by("id")),
            Group.objects.filter(name=PUBLIC_COLLABORATION_GROUP_NAME).count(),
        )

    def test_dry_run_changes_nothing(self):
        before = self._state()
        out = self._run()
        self.assertIn("Dry run", out)
        self.assertEqual(self._state(), before)
        self.assertEqual(self._state(), before)
        self._run("--dry-run")
        self.assertEqual(self._state(), before)

    def test_apply_sets_references_and_public(self):
        self._run("--apply")
        public = Group.objects.get(name=PUBLIC_COLLABORATION_GROUP_NAME)
        for row in (self.forced, self.old_tns):
            self.assertIn(public, row.groups.all())
        self.assertIn(public, self.spec.groups.all())
        self.old_tns.refresh_from_db()
        self.spec.refresh_from_db()
        self.forced.refresh_from_db()
        self.unrelated.refresh_from_db()
        self.assertEqual(self.old_tns.reference, "TNS 2026bfil")
        self.assertEqual(self.spec.reference, "TNS 2026bfil")
        self.assertFalse(self.forced.reference)  # shared header with forced points: left alone
        self.assertFalse(self.unrelated.reference)
        # idempotent
        after = self._state()
        self._run("--apply")
        self.assertEqual(self._state(), after)

    def test_existing_reference_and_restricted_rows_are_kept(self):
        self.old_tns.reference = "2020ApJ...1..1X"
        self.old_tns.save(update_fields=["reference"])
        restricted = Group.objects.create(name="team-x")
        self.spec.groups.add(restricted)
        self._run("--apply")
        self.old_tns.refresh_from_db()
        self.spec.refresh_from_db()
        self.assertEqual(self.old_tns.reference, "2020ApJ...1..1X")
        self.assertFalse(self.spec.reference)
