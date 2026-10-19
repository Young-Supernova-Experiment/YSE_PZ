"""The change log (#737): one entry per real change from every automatic writer, none for no-ops.

Fake clients only (no network); synthetic names, positions and users.
"""

from __future__ import annotations

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db.models import Q
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.models import ExternalServiceRun, Host, TransientClass, ValueChange
from YSE_App.services import change_log, enrichment, prost_hosts, redshift_catalogs as rc
from YSE_App.services import redshift_precedence as rp, tns_resync
from YSE_App.tests.fixtures_minimal import create_minimal_transient, create_test_user, ensure_transient_statuses
from YSE_App.tests.test_enrichment import _prost_table
from YSE_App.tests.test_redshift_catalogs import _fake_clients, _patched_matches
from YSE_App.tests.test_tns_resync import FakeTNS, _object

SERVICE_USERNAME = "changelog_cron"
READER_USERNAME = "changelog_reader"


class ChangeLogTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cron = create_test_user(SERVICE_USERNAME, is_staff=True)
        ensure_transient_statuses(cls.cron)
        for name in ("SN Ia", "SN II"):
            TransientClass.objects.get_or_create(name=name, defaults={"created_by": cls.cron,
                                                                      "modified_by": cls.cron})

    def setUp(self):
        self.t0 = timezone.now()
        self.transient = create_minimal_transient(self.cron, name="2026vca", ra=150.0, dec=2.0)
        self.host = Host.objects.create(ra=150.0, dec=2.0, name="synthetic-host",
                                        created_by=self.cron, modified_by=self.cron)
        self.transient.host = self.host
        self.transient.save()

    def assertTimestamped(self, change):
        self.assertTrue(self.t0 <= change.created_at <= timezone.now())


@override_settings(API_SERVICE_USERNAMES=[SERVICE_USERNAME], REDSHIFT_CATALOG_SERVICES="ned,desi",
                   DESI_SOURCE_LABEL="DESI DR1", REDSHIFT_CATALOG_CRON_ENABLED=False,
                   SHARING_TNS_REQUEST_INTERVAL_SECONDS=0, UPLOAD_GROUP_CHECKS_ENFORCED=True)
class WriterTests(ChangeLogTestBase):
    def _ned(self, z=0.03):
        from YSE_App.services import ned

        return ned.NEDMatch("SYNTH GAL A", 150.0001, 2.0, z, 0.0001, 0.4)

    def test_ned_records_one_entry_linked_to_its_run_then_none(self):
        with _patched_matches():
            rc.update_host(self.host, dry_run=False, clients=_fake_clients(self._ned(), None))
            rc.update_host(self.host, dry_run=False, clients=_fake_clients(self._ned(), None))
        change = ValueChange.objects.get()
        self.assertEqual((change.host_id, change.transient_id, change.field, change.old_value, change.new_value,
                          change.source, change.service, change.actor_id, change.restricted),
                         (self.host.pk, None, "redshift", None, "0.03", "NED", "ned-redshift", None, False))
        self.assertEqual(change.run, ExternalServiceRun.objects.filter(service__slug="ned-redshift")
                         .order_by("pk").first())
        self.assertTimestamped(change)
        # a higher-ranked replacement is one more entry, with the old value and source
        with _patched_matches():
            rc.update_host(self.host, dry_run=False, clients=_fake_clients(self._ned(0.031), None))
        latest = ValueChange.objects.order_by("-pk").first()
        self.assertEqual((latest.old_value, latest.old_source, latest.new_value), ("0.03", "NED", "0.031"))
        self.assertEqual(ValueChange.objects.count(), 2)

    def test_dry_run_records_nothing(self):
        with _patched_matches():
            rc.update_host(self.host, dry_run=True, clients=_fake_clients(self._ned(), None))
        self.assertFalse(ValueChange.objects.exists())

    def test_tns_resync_records_each_changed_field_once(self):
        reply = _object("2026vca", cls="SN Ia", z=0.031)
        tns_resync.resync_object(self.transient, dry_run=False, client=FakeTNS({"2026vca": reply}))
        changes = {c.field: c for c in ValueChange.objects.filter(transient=self.transient)}
        self.assertEqual(set(changes), {"TNS_spec_class", "best_spec_class", "redshift"})
        run = ExternalServiceRun.objects.get(service__slug="tns", transient=self.transient)
        for change in changes.values():
            self.assertEqual((change.service, change.run, change.actor_id), ("tns", run, None))
            self.assertTimestamped(change)
        self.assertEqual(changes["TNS_spec_class"].category, ValueChange.CATEGORY_CLASSIFICATION)
        self.assertEqual((changes["redshift"].new_value, changes["redshift"].source), ("0.031", "TNS"))
        tns_resync.resync_object(self.transient, dry_run=False, client=FakeTNS({"2026vca": reply}))
        self.assertEqual(ValueChange.objects.count(), 3)  # the same reply changes nothing
        tns_resync.resync_object(self.transient, dry_run=False,
                                 client=FakeTNS({"2026vca": _object("2026vca", cls="SN II", z=0.031)}))
        self.assertEqual(sorted(ValueChange.objects.filter(new_value="SN II").values_list("field", flat=True)),
                         ["TNS_spec_class", "best_spec_class"])

    def test_prost_records_host_link_and_redshift(self):
        t = create_minimal_transient(self.cron, name="2026vcp", ra=10.0, dec=1.0)
        with mock.patch.object(prost_hosts, "associate", return_value=_prost_table("2026vcp")):
            enrichment.run(enrichment.get("prost"), [t], dry_run=False, user=self.cron)
            enrichment.run(enrichment.get("prost"), [t], dry_run=False, user=self.cron)  # has a host now
        t.refresh_from_db()
        link = ValueChange.objects.get(transient=t)
        self.assertEqual((link.field, link.old_value, link.category, link.service),
                         ("host", None, ValueChange.CATEGORY_HOST, "prost-host"))
        self.assertIn("SYNTH GAL P", link.new_value)
        z = ValueChange.objects.get(host=t.host)
        self.assertEqual((z.field, z.new_value, z.source), ("redshift", "0.042", "Prost"))
        run = ExternalServiceRun.objects.filter(service__slug="prost-host", transient=t).order_by("pk").first()
        self.assertEqual((link.run, z.run), (run, run))

    def test_photo_z_services_record_changes_only(self):
        from YSE_App.data_ingest import Photo_Z, SDSS_Photo_Z

        with mock.patch.object(Photo_Z, "predict_hosts", return_value={self.host.pk: (0.12, 0.02)}):
            enrichment.run(enrichment.get("photoz-ml"), [self.host], dry_run=False, user=self.cron)
            summary = enrichment.run(enrichment.get("photoz-ml"), [Host.objects.get(pk=self.host.pk)],
                                     dry_run=False, user=self.cron)
        self.assertEqual(summary.counts, {"unchanged": 1})
        with mock.patch.object(SDSS_Photo_Z, "fetch_photoz", return_value={self.host.pk: (0.2, 0.05)}):
            enrichment.run(enrichment.get("sdss-photoz"), [Host.objects.get(pk=self.host.pk)], dry_run=False,
                           user=self.cron)
            enrichment.run(enrichment.get("sdss-photoz"), [Host.objects.get(pk=self.host.pk)], dry_run=False,
                           user=self.cron)
        changes = list(ValueChange.objects.filter(host=self.host).order_by("pk"))
        self.assertEqual([(c.field, c.new_value, c.service) for c in changes],
                         [("photo_z_internal", "0.12", "photoz-ml"), ("photo_z", "0.2", "sdss-photoz")])
        self.assertEqual(changes[0].run.service.slug, "photoz-ml")
        self.assertEqual(changes[1].run.service.slug, "sdss-photoz")

    def test_hermes_style_writer_records_through_apply_redshift(self):
        rp.apply_redshift(self.transient, 0.05, None, rp.SOURCE_HERMES)
        rp.apply_redshift(self.transient, 0.05, None, rp.SOURCE_HERMES)  # unchanged: no write
        change = ValueChange.objects.get()
        self.assertEqual((change.transient_id, change.service, change.source), (self.transient.pk, "Hermes",
                                                                                 "Hermes"))

    def test_override_set_and_clear(self):
        user = create_test_user("changelog_editor", is_staff=False)
        rp.set_override(self.transient, 0.0312, 0.0005, actor=user)
        rp.set_override(self.transient, 0.0312, 0.0005, actor=user)  # same: nothing new
        rp.clear_override(self.transient, actor=user)
        rp.clear_override(self.transient, actor=user)  # already clear: nothing new
        changes = list(ValueChange.objects.order_by("pk"))
        self.assertEqual([(c.field, c.note, c.category, c.actor, c.restricted) for c in changes],
                         [("redshift", "override set", "override", user, True),
                          ("redshift_override", "override cleared", "override", user, True)])
        self.assertEqual(change_log.as_dict(changes[0])["actor"], "changelog_editor")

    def test_override_command(self):
        call_command("redshift_override", "--transient", "2026vca", "--z", "0.0312", stdout=StringIO())
        call_command("redshift_override", "--transient", "2026vca", "--clear", stdout=StringIO())
        self.assertEqual(list(ValueChange.objects.order_by("pk").values_list("service", "note")),
                         [(change_log.SERVICE_COMMAND, "override set"),
                          (change_log.SERVICE_COMMAND, "override cleared")])

    def test_api_serializer_override(self):
        from YSE_App.serializers.transient_serializers import TransientSerializer

        user = create_test_user("changelog_api", is_staff=False)
        request = RequestFactory().patch("/api/transients/%d/" % self.transient.pk)
        request.user = user
        serializer = TransientSerializer(self.transient, context={"request": request})
        serializer.update(self.transient, {"redshift": 0.05, "redshift_source": "manual", "redshift_override": True})
        serializer.update(self.transient, {"disc_date": None})  # no redshift change
        serializer.update(self.transient, {"redshift_override": False})
        changes = list(ValueChange.objects.order_by("pk"))
        self.assertEqual([(c.transient_id, c.service, c.actor, c.note) for c in changes],
                         [(self.transient.pk, "api", user, "override set"),
                          (self.transient.pk, "api", user, "override cleared")])
        self.assertEqual(changes[0].new_value, "0.05")


@override_settings(DATA_SERVICE_READER_USERNAMES=[READER_USERNAME], API_SERVICE_USERNAMES=[])
class VisibilityTests(ChangeLogTestBase):
    def setUp(self):
        super().setUp()
        self.outsider = create_test_user("changelog_outsider", is_staff=False)
        self.reader = create_test_user(READER_USERNAME, is_staff=False)
        rp.apply_redshift(self.host, 0.03, None, rp.SOURCE_NED, service="ned-redshift")  # public catalogue
        rp.set_override(self.transient, 0.031, None, actor=self.reader)  # by hand: restricted

    def _api(self, user, transient):
        self.client.force_login(user)
        return self.client.get(reverse("api-transient-history", args=[transient.pk]))

    def test_public_entries_follow_listing_restricted_need_data_access(self):
        data = self._api(self.outsider, self.transient).json()
        self.assertEqual([r["field"] for r in data["results"]], ["redshift"])
        self.assertEqual(data["results"][0]["source"], "NED")
        data = self._api(self.reader, self.transient).json()
        self.assertEqual({(r["target"], r["service"]) for r in data["results"]},
                         {("host", "ned-redshift"), ("transient", "manual")})
        self.assertEqual({r["actor"] for r in data["results"]}, {"system", READER_USERNAME})

    def test_unlisted_transient_is_404(self):
        private = create_minimal_transient(self.cron, name="private-candidate-1")
        rp.apply_redshift(private, 0.1, None, rp.SOURCE_NED, service="ned-redshift")
        self.assertEqual(self._api(self.outsider, private).status_code, 404)
        self.assertEqual(self._api(self.reader, private).status_code, 200)

    def test_history_fragment(self):
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("transient_detail_history_fragment", args=[self.transient.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ned-redshift")
        self.assertNotContains(response, ">override set<")
        self.client.force_login(self.reader)
        response = self.client.get(reverse("transient_detail_history_fragment", args=[self.transient.pk]))
        self.assertContains(response, ">override set<")

    def test_api_needs_login(self):
        response = self.client.get(reverse("api-transient-history", args=[self.transient.pk]))
        self.assertIn(response.status_code, (401, 403))

    def test_detail_page_defers_the_tab(self):
        from django.template.loader import get_template

        source = get_template("YSE_App/transient_detail.html").template.source
        self.assertIn("transient_detail_history_fragment", source)
        self.assertIn('id="history_tab_content"', source)


@override_settings(API_SERVICE_USERNAMES=[SERVICE_USERNAME], REDSHIFT_CATALOG_CRON_ENABLED=False)
class UploadAndOtherWriterTests(ChangeLogTestBase):
    """Follow-up to #737: the upload endpoints, the TNS cron's Prost hosts, error-only photo-z, Hermes, API edits."""

    def _upload(self, entry, **flags):
        from YSE_App.data_utils import add_transient_payload

        doc = {"2026vca": dict({"name": "2026vca"}, **entry), "noupdatestatus": True}
        doc.update(flags)
        response = add_transient_payload(doc, self.uploader)
        self.assertEqual(response.status_code, 200)

    def setUp(self):
        super().setUp()
        self.uploader = create_test_user("changelog_uploader", is_staff=False)

    def test_upload_redshift_and_classification_with_uploader_as_actor(self):
        self._upload({"redshift": 0.05, "redshift_source": "Sherlock"})
        self._upload({"redshift": 0.05, "redshift_source": "Sherlock"})  # same: nothing new
        change = ValueChange.objects.get()
        self.assertEqual((change.transient_id, change.field, change.new_value, change.source, change.service,
                          change.actor, change.restricted),
                         (self.transient.pk, "redshift", "0.05", "Sherlock", "upload", self.uploader, False))
        self._upload({"TNS_spec_class": "SN Ia"}, TNS=True)
        self._upload({"best_spec_class": "SN II"})
        tns, ours = ValueChange.objects.filter(category="classification").order_by("pk")
        self.assertEqual((tns.field, tns.source, tns.restricted), ("TNS_spec_class", "TNS", False))
        self.assertEqual((ours.field, ours.new_value, ours.source, ours.restricted),
                         ("best_spec_class", "SN II", "", True))  # no source: restricted

    def test_tns_cron_host_link_photo_z_and_error_only_change(self):
        from YSE_App.data_utils import add_transient_host_util

        t = create_minimal_transient(self.cron, name="2026vch", ra=10.0, dec=1.0)
        add_transient_host_util({"name": "SYNTH GAL Q", "ra": 10.0, "dec": 1.0, "photo_z_internal": 0.2,
                                 "photo_z_err_internal": 0.05}, t, self.cron, source_hint="Prost")
        add_transient_host_util({"name": "SYNTH GAL Q", "ra": 10.0, "dec": 1.0, "photo_z_internal": 0.2,
                                 "photo_z_err_internal": 0.05}, t, self.cron, source_hint="Prost")  # no change
        add_transient_host_util({"name": "SYNTH GAL Q", "ra": 10.0, "dec": 1.0, "photo_z_internal": 0.2,
                                 "photo_z_err_internal": 0.04}, t, self.cron, source_hint="Prost")
        t.refresh_from_db()
        rows = list(ValueChange.objects.filter(Q(transient=t) | Q(host=t.host)).order_by("pk"))
        self.assertEqual([(r.field, r.old_value, r.new_value, r.source, r.actor) for r in rows],
                         [("photo_z_internal", None, "0.2", "Prost", self.cron),
                          ("host", None, "SYNTH GAL Q (host %d)" % t.host_id, "Prost", self.cron),
                          ("photo_z_err_internal", "0.05", "0.04", "Prost", self.cron)])
        self.assertEqual(rows[2].note, "uncertainty only")

    def test_photo_z_error_only_changes_are_recorded(self):
        from YSE_App.data_ingest import Photo_Z, SDSS_Photo_Z

        for err in (0.02, 0.03):
            with mock.patch.object(Photo_Z, "predict_hosts", return_value={self.host.pk: (0.12, err)}):
                enrichment.run(enrichment.get("photoz-ml"), [Host.objects.get(pk=self.host.pk)], dry_run=False,
                               user=self.cron)
        for err in (0.05, 0.05, 0.06):
            with mock.patch.object(SDSS_Photo_Z, "fetch_photoz", return_value={self.host.pk: (0.2, err)}):
                enrichment.run(enrichment.get("sdss-photoz"), [Host.objects.get(pk=self.host.pk)],
                               dry_run=False, user=self.cron)
        self.assertEqual(list(ValueChange.objects.order_by("pk").values_list("field", "new_value")),
                         [("photo_z_internal", "0.12"), ("photo_z_err_internal", "0.03"),
                          ("photo_z", "0.2"), ("photo_z_err", "0.06")])

    def test_hermes_classification(self):
        from YSE_App.feeds import hermes as hermes_mod
        from YSE_App.feeds.base import FeedMessage

        message = FeedMessage(feed="hermes", message_id="m1", kind="classification", object_id="2026vca",
                              classification="SN Ia")
        hermes_mod.HermesFeed().apply_classification(message, self.transient, self.cron)
        hermes_mod.HermesFeed().apply_classification(message, self.transient, self.cron)
        change = ValueChange.objects.get()
        self.assertEqual((change.field, change.new_value, change.service, change.source, change.actor_id),
                         ("best_spec_class", "SN Ia", "hermes", "Hermes", None))

    def test_api_classification_and_host_edits(self):
        from YSE_App.serializers.transient_serializers import TransientSerializer

        request = RequestFactory().patch("/api/transients/%d/" % self.transient.pk)
        request.user = self.uploader
        serializer = TransientSerializer(self.transient, context={"request": request})
        serializer.update(self.transient, {"best_spec_class": TransientClass.objects.get(name="SN II"),
                                           "TNS_spec_class": "SN II", "host": None})
        rows = {r.field: r for r in ValueChange.objects.all()}
        self.assertEqual(set(rows), {"best_spec_class", "TNS_spec_class", "host"})
        self.assertEqual(rows["host"].new_value, None)
        self.assertTrue(all(r.actor == self.uploader and r.restricted for r in rows.values()))
