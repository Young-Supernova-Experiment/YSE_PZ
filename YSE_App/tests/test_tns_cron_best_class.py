"""The legacy TNS crons follow the re-sync rule for ``best_spec_class`` (#755).

The upload is built by ``processTNS.getTNSData`` exactly as the crons build it and sent
through ``add_transient_payload`` (the body of ``/add_transient/``). Synthetic objects;
nothing contacts TNS.
"""

from __future__ import annotations

from astropy.coordinates import SkyCoord
import astropy.units as u
from django.test import SimpleTestCase, TestCase

from YSE_App.data_ingest.TNS_uploads import processTNS
from YSE_App.data_utils import add_transient_payload
from YSE_App.models import ObservationGroup, Transient, TransientClass
from YSE_App.services import tns_resync
from YSE_App.tests.fixtures_minimal import (
    create_minimal_transient,
    create_test_user,
    ensure_transient_statuses,
)


def _tns_object(name, cls):
    return {
        "objname": name, "discoverydate": "2026-05-30 10:00:00", "redshift": None,
        "object_type": {"name": cls}, "reporting_group": {"group_name": "ZTF"}, "internal_names": "",
    }


class BestClassRuleTests(SimpleTestCase):
    def test_rule(self):
        allowed = tns_resync.tns_may_set_best_class
        self.assertTrue(allowed("", "", "SN Ia"))             # we have none
        self.assertTrue(allowed("SN Ia", "SN Ia", "SN II"))   # ours is the previous TNS class
        self.assertFalse(allowed("SN II", "", "SN Ia"))       # ours is our own
        self.assertFalse(allowed("SN II", "SN Ia", "SN Ia"))  # TNS unchanged, ours differs
        self.assertFalse(allowed("SN Ia", "", "SN Ia"))       # already the same
        self.assertFalse(allowed("", "", ""))                 # TNS has no class


class TNSCronBestClassTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("tns_cron_class_test", is_superuser=True)
        ensure_transient_statuses(cls.user)
        audit = {"created_by": cls.user, "modified_by": cls.user}
        for name in ("Unknown", "ZTF"):
            ObservationGroup.objects.get_or_create(name=name, defaults=audit)
        cls.classes = {}
        for name in ("Unknown", "SN Ia", "SN II"):
            cls.classes[name], _ = TransientClass.objects.get_or_create(name=name, defaults=audit)

    def _transient(self, name, *, ours=None, previous=None):
        transient = create_minimal_transient(self.user, name=name, obs_group_name="ZTF")
        Transient.objects.filter(pk=transient.pk).update(
            best_spec_class=self.classes[ours] if ours else None, TNS_spec_class=previous)
        return transient

    def _upload(self, transient, tns_class):
        proc = processTNS()
        proc.status = "New"
        proc.fixture_skip_ps = True
        proc.fixture_skip_archival = True
        sc = SkyCoord(transient.ra, transient.dec, unit=u.deg)
        doc = proc.getTNSData(_tns_object(transient.name, tns_class), transient.name, sc, None)
        self.assertEqual(doc["best_spec_class"], tns_class)  # what the cron sends
        response = add_transient_payload({transient.name: doc, "noupdatestatus": True, "TNS": True}, self.user)
        self.assertEqual(response.status_code, 200, response.content)
        transient.refresh_from_db()
        return transient

    def test_our_own_class_is_kept(self):
        t = self._upload(self._transient("2026cba", ours="SN II"), "SN Ia")
        self.assertEqual(t.best_spec_class.name, "SN II")
        self.assertEqual(t.TNS_spec_class, "SN Ia")

    def test_empty_class_is_filled_from_tns(self):
        t = self._upload(self._transient("2026cbb"), "SN Ia")
        self.assertEqual(t.best_spec_class.name, "SN Ia")
        self.assertEqual(t.TNS_spec_class, "SN Ia")

    def test_previous_tns_class_follows_tns(self):
        t = self._upload(self._transient("2026cbc", ours="SN Ia", previous="SN Ia"), "SN II")
        self.assertEqual(t.best_spec_class.name, "SN II")
        self.assertEqual(t.TNS_spec_class, "SN II")

    def test_unknown_tns_class_does_not_become_unknown(self):
        t = self._upload(self._transient("2026cbd"), "SN Unlisted")
        self.assertIsNone(t.best_spec_class)
        self.assertEqual(t.TNS_spec_class, "SN Unlisted")
