"""Background-job exports with no object cap (#855).

Over the cap, a download is queued as an ``exports.build`` job when ``BACKGROUND_EXPORTS_ENABLED`` is on
and refused as before when it is off; the job's file equals the interactive download; access is
evaluated as the requesting user when the job runs; only that user can download; retention cleanup;
notifications (mocked).

Synthetic data only: made-up users, groups, transients, photometry and spectra.
"""

from __future__ import annotations

import datetime
import io
import os
import shutil
import tempfile
import time
import zipfile
from unittest import mock
from urllib.parse import unquote_plus

from astropy.table import Table
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.http import QueryDict
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App import views as views_module
from YSE_App.jobs import run_pass
from YSE_App.models import TransientPhotData, TransientPhotometry
from YSE_App.models.job_models import Job
from YSE_App.services import background_exports as bx
from YSE_App.services import selections as selection_svc
from YSE_App.services.download_options import PhotometryOptions
from YSE_App.tests.fixtures_minimal import audit_fields
from YSE_App.tests.test_spectral_phase import PhaseFixture, manifest, mjd_date, preview_client

SEARCH = "name_contains=2099bx"


def body(response):
    return b"".join(response.streaming_content) if response.streaming else response.content


def members(data):
    archive = zipfile.ZipFile(io.BytesIO(data))
    return {name: archive.read(name) for name in archive.namelist()}


class ExportFixture(PhaseFixture):
    def setUp(self):
        self.media = tempfile.mkdtemp(prefix="yse-export-test-")
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        media = override_settings(MEDIA_ROOT=self.media)
        media.enable()
        self.addCleanup(media.disable)
        patcher = mock.patch("YSE_App.services.notify.notify")
        self.notify = patcher.start()
        self.addCleanup(patcher.stop)

        self.t1 = self.make_transient("2099bxa", disc_mjd=60000.0)
        self.t2 = self.make_transient("2099bxb", disc_mjd=60100.0)
        for transient, mjds in ((self.t1, (60005.0, 60012.0, 60030.0)), (self.t2, (60105.0,))):
            phot = TransientPhotometry.objects.create(
                transient=transient, instrument=self.instrument, obs_group=self.obs_group, **audit_fields(self.staff))
            for mjd in mjds:
                TransientPhotData.objects.create(photometry=phot, band=self.band, obs_date=mjd_date(mjd),
                                                 mag=18.0, mag_err=0.1, **audit_fields(self.staff))
        # Points and a spectrum only the private group may see.
        private = TransientPhotometry.objects.create(
            transient=self.t1, instrument=self.other_instrument, obs_group=self.obs_group, **audit_fields(self.staff))
        private.groups.add(self.group)
        TransientPhotData.objects.create(photometry=private, band=self.band, obs_date=mjd_date(60020.0),
                                         mag=17.5, mag_err=0.05, **audit_fields(self.staff))
        self.make_spectrum(self.t1, 60013.0)
        self.make_spectrum(self.t2, 60110.0)
        self.make_spectrum(self.t1, 60015.0, groups=[self.group])

    def run_jobs(self):
        return run_pass(kinds=[bx.KIND])

    def export(self, user, **kwargs):
        kwargs.setdefault("source", "search")
        kwargs.setdefault("params", SEARCH)
        job = bx.request_export(user, **kwargs)
        self.run_jobs()
        job.refresh_from_db()
        return job

    def stored(self, job):
        self.assertEqual(job.status, Job.DONE, job.error)
        with default_storage.open(job.result["file"], "rb") as fh:
            return fh.read()

    def over_cap(self):
        return mock.patch.object(views_module, "SEARCH_BULK_PHOTOMETRY_MAX_ROWS", 1)


@override_settings(BACKGROUND_EXPORTS_ENABLED=True)
class QueueTests(ExportFixture):
    def get(self, name, query="", user=None):
        data = QueryDict(query, mutable=True)
        data["params"] = SEARCH
        return preview_client(user or self.member).get(reverse(name), data)

    def test_over_cap_queues_a_job(self):
        for name, data in (("search_download_photometry", "photometry"), ("search_download_spectra", "spectra")):
            with self.over_cap():
                response = self.get(name, "format=csv&mjd_min=60010")
            self.assertEqual(response.status_code, 302)
            self.assertIn("runs in the background", unquote_plus(response["Location"]))
            job = Job.objects.filter(kind=bx.KIND).latest("id")
            self.assertEqual(job.created_by, self.member)
            self.assertEqual(job.status, Job.QUEUED)
            self.assertEqual(job.payload["data"], data)
            self.assertEqual(job.payload["source"], "search")
            self.assertEqual(QueryDict(job.payload["params"])["name_contains"], "2099bx")
            self.assertEqual(QueryDict(job.payload["options"])["mjd_min"], "60010")
            self.assertNotIn("format", QueryDict(job.payload["options"]))
        self.assertEqual(Job.objects.filter(kind=bx.KIND).count(), 2)

    def test_under_cap_downloads_directly(self):
        response = self.get("search_download_photometry")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Job.objects.filter(kind=bx.KIND).exists())

    def test_selection_over_cap_queues(self):
        selection = selection_svc.create_selection(self.member, "Big", search_params=SEARCH)
        url = reverse("selection_download_photometry", kwargs={"slug": selection.slug})
        with self.over_cap():
            response = preview_client(self.member).get(url, {"format": "ecsv"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("selection_detail", kwargs={"slug": selection.slug}))
        job = Job.objects.get(kind=bx.KIND)
        self.assertEqual((job.payload["source"], job.payload["slug"], job.payload["format"]),
                         ("selection", selection.slug, "ecsv"))

    def test_bulk_action_over_cap_queues_for_preview_users(self):
        client = preview_client(self.member)
        with self.over_cap():
            response = client.post(reverse("search_bulk_action"),
                                   {"action": "photometry", "select_all": "1", "params": SEARCH})
        self.assertEqual(response.status_code, 302)
        self.assertIn("runs in the background", unquote_plus(response["Location"]))
        job = Job.objects.get(kind=bx.KIND)
        self.assertEqual((job.payload["source"], job.payload["order_by"]), ("search", "-disc_date"))

        ids = [str(self.t1.pk), str(self.t2.pk)]
        with self.over_cap():
            client.post(reverse("search_bulk_action"), {"action": "photometry", "transient_id": ids})
        job = Job.objects.filter(kind=bx.KIND).latest("id")
        self.assertEqual(sorted(job.payload["ids"]), sorted(int(i) for i in ids))
        self.run_jobs()
        job.refresh_from_db()
        expected = views_module.bulk_photometry_zip_response(self.member, [self.t2, self.t1])
        self.assertEqual(members(self.stored(job)), members(expected.content))

        # Preview off: refused as before.
        with self.over_cap():
            response = preview_client(self.member, preview=False).post(
                reverse("search_bulk_action"), {"action": "photometry", "transient_id": ids})
        self.assertIn("Too many rows", unquote_plus(response["Location"]))

    def test_bad_format_goes_back(self):
        response = self.get("search_download_photometry", "format=parquet")
        self.assertEqual(response.status_code, 302)
        self.assertIn("Unknown download format", unquote_plus(response["Location"]))


@override_settings(BACKGROUND_EXPORTS_ENABLED=False)
class DisabledTests(ExportFixture):
    def test_over_cap_is_refused_as_today(self):
        client = preview_client(self.member)
        selection = selection_svc.create_selection(self.member, "Off", search_params=SEARCH)
        with self.over_cap():
            for name in ("search_download_photometry", "search_download_spectra"):
                response = client.get(reverse(name), {"params": SEARCH})
                self.assertEqual(response.status_code, 302)
                self.assertIn("Too many transients for a download (limit 1); narrow the search first.",
                              unquote_plus(response["Location"]))
            response = client.get(reverse("selection_download_photometry", kwargs={"slug": selection.slug}))
            self.assertEqual(response.status_code, 302)
            response = client.post(reverse("search_bulk_action"),
                                   {"action": "photometry", "select_all": "1", "params": SEARCH})
            self.assertIn("Too many rows for this action (limit 1); narrow the search first.",
                          unquote_plus(response["Location"]))
        self.assertFalse(Job.objects.filter(kind=bx.KIND).exists())

    def test_pages_are_404_and_menu_text_unchanged(self):
        client = preview_client(self.member)
        self.assertEqual(client.get(reverse("export_list")).status_code, 404)
        self.assertEqual(client.get(reverse("export_download", kwargs={"job_id": 1})).status_code, 404)
        html = client.get("/search/?%s" % SEARCH).content.decode()
        self.assertIn("At most 300 transients.", html)
        self.assertNotIn("background job", html)


@override_settings(BACKGROUND_EXPORTS_ENABLED=True)
class OutputTests(ExportFixture):
    """The job's file equals the interactive download for the same small request."""

    def interactive(self, name, query, user=None):
        data = QueryDict(query, mutable=True)
        data["params"] = SEARCH
        response = preview_client(user or self.member).get(reverse(name), data)
        self.assertEqual(response.status_code, 200)
        return body(response)

    def test_photometry_csv(self):
        for options in ("", "mjd_min=60010&band=%s" % self.band.name):
            expected = self.interactive("search_download_photometry", "format=csv&" + options)
            job = self.export(self.member, data="photometry", fmt="csv", options=options)
            self.assertEqual(members(self.stored(job)), members(expected))
        self.assertEqual(sorted(members(expected)), ["2099bxa.csv", "2099bxb.csv", "README.txt"])

    def test_photometry_snana_equals_per_object_download(self):
        expected = self.interactive("search_download_photometry", "format=snana")
        job = self.export(self.member, data="photometry", fmt="snana")
        files = members(self.stored(job))
        self.assertEqual(files, members(expected))
        client = Client()
        client.force_login(self.member)
        for transient in (self.t1, self.t2):
            page = body(client.get(reverse("download_photometry", kwargs={"slug": transient.slug})))
            self.assertEqual(files["%s_data.snana.txt" % transient.slug], page)

    def test_photometry_ecsv(self):
        expected = self.interactive("search_download_photometry", "format=ecsv")
        job = self.export(self.member, data="photometry", fmt="ecsv")
        data = self.stored(job)
        self.assertEqual(data, expected)
        table = Table.read(io.StringIO(data.decode()), format="ascii.ecsv")
        self.assertEqual(len(table), 5)  # 4 points of 2099bxa (one private), 1 of 2099bxb
        self.assertEqual(sorted(set(str(v) for v in table["transient"])), ["2099bxa", "2099bxb"])
        csv_rows = sum(content.decode().count("\n6") for name, content in members(
            views_module.bulk_photometry_zip_response(self.member, [self.t1, self.t2]).content).items()
            if name.endswith(".csv"))
        self.assertEqual(len(table), csv_rows)

    def test_spectra(self):
        expected = self.interactive("search_download_spectra", "phase_ref=discovery")
        job = self.export(self.member, data="spectra", fmt="csv", options="phase_ref=discovery")
        self.assertEqual(members(self.stored(job)), members(expected))
        self.assertEqual(len(manifest(zipfile.ZipFile(io.BytesIO(expected)))), 3)


@override_settings(BACKGROUND_EXPORTS_ENABLED=True)
class AccessTests(ExportFixture):
    def test_points_and_spectra_as_the_requester(self):
        outsider = members(self.stored(self.export(self.outsider, data="photometry", fmt="csv")))
        member = members(self.stored(self.export(self.member, data="photometry", fmt="csv")))
        self.assertEqual(outsider["2099bxa.csv"].decode().count("\n6"), 3)
        self.assertEqual(member["2099bxa.csv"].decode().count("\n6"), 4)
        self.assertNotIn(b"17.500", outsider["2099bxa.csv"])
        spectra = self.stored(self.export(self.outsider, data="spectra", fmt="csv"))
        rows = manifest(zipfile.ZipFile(io.BytesIO(spectra)))
        self.assertEqual(len(rows), 2)  # the group-only spectrum is not the outsider's

    def test_access_is_decided_when_the_job_runs(self):
        job = bx.request_export(self.member, data="photometry", fmt="csv", source="search", params=SEARCH)
        self.member.groups.remove(self.group)
        self.run_jobs()
        job.refresh_from_db()
        self.assertEqual(members(self.stored(job))["2099bxa.csv"].decode().count("\n6"), 3)

    def test_selection_no_longer_visible_fails_with_a_notice(self):
        selection = selection_svc.create_selection(self.member, "Gone", search_params=SEARCH)
        job = bx.request_export(self.outsider, data="photometry", fmt="csv", source="selection",
                                slug=selection.slug)
        self.run_jobs()
        job.refresh_from_db()
        self.assertEqual(job.status, Job.FAILED)
        self.assertEqual(job.attempts, 1)
        (args, kwargs), = self.notify.call_args_list
        self.assertEqual(args[0], [self.outsider])
        self.assertIn("could not be built", args[1])


@override_settings(BACKGROUND_EXPORTS_ENABLED=True)
class DownloadTests(ExportFixture):
    def setUp(self):
        super().setUp()
        self.job = self.export(self.member, data="photometry", fmt="csv")
        self.url = reverse("export_download", kwargs={"job_id": self.job.pk})

    def test_only_the_owner_can_download(self):
        client = Client()
        client.force_login(self.member)
        response = client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body(response), self.stored(self.job))
        self.assertIn("attachment", response["Content-Disposition"])
        for other in (self.outsider, self.staff):
            client.force_login(other)
            self.assertEqual(client.get(self.url).status_code, 404)
        self.assertEqual(Client().get(self.url).status_code, 302)  # anonymous: sign in

    def test_list_page_and_jobs_page(self):
        client = Client()
        client.force_login(self.member)
        html = client.get(reverse("export_list")).content.decode()
        self.assertIn(self.url, html)
        client.force_login(self.outsider)
        self.assertNotIn(self.url, client.get(reverse("export_list")).content.decode())
        own = self.export(self.staff, data="spectra", fmt="csv")
        client.force_login(self.staff)
        html = client.get(reverse("jobs_status")).content.decode()
        self.assertIn('id="jobs-my-exports"', html)
        self.assertIn(reverse("export_download", kwargs={"job_id": own.pk}), html)
        self.assertNotIn(self.url, html)
        with override_settings(BACKGROUND_EXPORTS_ENABLED=False):
            self.assertNotIn('id="jobs-my-exports"', client.get(reverse("jobs_status")).content.decode())

    def test_notification_on_success(self):
        (args, kwargs), = self.notify.call_args_list
        self.assertEqual(args[0], [self.member])
        self.assertIn("is ready", args[1])
        self.assertEqual(kwargs["kind"], "job_result")
        self.assertEqual(kwargs["url"], reverse("export_list"))
        self.assertEqual(self.job.result["transients"], 2)


@override_settings(BACKGROUND_EXPORTS_ENABLED=True, EXPORT_RETENTION_DAYS=7)
class RetentionTests(ExportFixture):
    def test_cleanup(self):
        old = self.export(self.member, data="photometry", fmt="csv")
        new = self.export(self.member, data="photometry", fmt="ecsv")
        Job.objects.filter(pk=old.pk).update(finished_at=timezone.now() - datetime.timedelta(days=8))
        old.refresh_from_db()
        self.assertIsNone(bx.export_file(old))  # expired: no longer downloadable even before cleanup

        root = os.path.join(self.media, bx.EXPORT_DIR)
        orphan_old = os.path.join(root, "999998-aaaa")
        orphan_new = os.path.join(root, "999999-bbbb")
        for path in (orphan_old, orphan_new):
            os.makedirs(path)
            with open(os.path.join(path, "x.zip"), "wb") as fh:
                fh.write(b"synthetic")
        stamp = time.time() - 9 * 86400
        os.utime(os.path.join(orphan_old, "x.zip"), (stamp, stamp))

        out = io.StringIO()
        call_command("prune_exports", "--dry-run", stdout=out)
        self.assertIn("would delete 2", out.getvalue())
        self.assertTrue(default_storage.exists(old.result["file"]))

        call_command("prune_exports", stdout=io.StringIO())
        self.assertFalse(default_storage.exists(old.result["file"]))
        self.assertFalse(os.path.exists(orphan_old))
        self.assertTrue(os.path.exists(orphan_new))
        self.assertTrue(default_storage.exists(new.result["file"]))
        self.assertIsNotNone(bx.export_file(new))

    def test_each_job_prunes_first(self):
        old = self.export(self.member, data="photometry", fmt="csv")
        Job.objects.filter(pk=old.pk).update(finished_at=timezone.now() - datetime.timedelta(days=8))
        self.export(self.member, data="photometry", fmt="snana")
        self.assertFalse(default_storage.exists(old.result["file"]))


class OptionsTests(ExportFixture):
    def test_snana_keep_matches_csv_options(self):
        options = PhotometryOptions.from_params(QueryDict("mjd_min=60010&mjd_max=60020"))
        keep = bx.photometry_keep(options, self.t1)
        self.assertTrue(keep({"obs_date": mjd_date(60012.0), "band": "x - %s" % self.band.name,
                              "data_quality": []}))
        self.assertFalse(keep({"obs_date": mjd_date(60030.0), "band": "x - %s" % self.band.name,
                               "data_quality": []}))
        self.assertIsNone(bx.photometry_keep(PhotometryOptions(), self.t1))
