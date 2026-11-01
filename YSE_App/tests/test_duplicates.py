"""Duplicate transients are reported, and merged only on confirmation (#752, #753, #754).

Synthetic names, positions and users only; no network.
"""

from __future__ import annotations

import datetime
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from YSE_App.models import (AlternateTransientNames, Transient, TransientPhotometry, TransientSpectrum,
                            TransientTag)
from YSE_App.models.notification_models import Notification
from YSE_App.services import duplicates
from YSE_App.tests.fixtures_minimal import (attach_synthetic_photometry, attach_synthetic_spectrum,
                                            create_minimal_transient, create_test_user,
                                            ensure_transient_statuses)

SERVICE_USERNAME = "dup_cron"


class DuplicateTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cron = create_test_user(SERVICE_USERNAME, is_staff=True)
        cls.admin = create_test_user("dup_admin", is_staff=True, is_superuser=True)
        ensure_transient_statuses(cls.cron)

    def _same_name_pair(self, name="2026dupa"):
        keep = create_minimal_transient(self.cron, name=name, ra=40.0, dec=-5.0)
        dup = Transient.objects.create(name=name, ra=40.0, dec=-5.0, status=keep.status,
                                       obs_group=keep.obs_group, created_by=self.cron, modified_by=self.cron)
        dup.refresh_from_db()
        self.assertNotEqual(dup.slug, dup.name)  # AutoSlugField appended a suffix
        return dup, keep

    def _position_pair(self):
        disc = timezone.now() - datetime.timedelta(days=10)
        keep = create_minimal_transient(self.cron, name="2026posk", ra=120.0, dec=10.0)
        internal = create_minimal_transient(self.cron, name="1dabcde", ra=120.0001, dec=10.0001)
        Transient.objects.filter(pk__in=(keep.pk, internal.pk)).update(disc_date=disc)
        return Transient.objects.get(pk=internal.pk), Transient.objects.get(pk=keep.pk)

    def _admin_notes(self):
        return Notification.objects.filter(recipient=self.admin, subject=duplicates.SUBJECT)


class DetectionAndReportTests(DuplicateTestBase):
    def test_finds_both_kinds_of_pair(self):
        dup, keep = self._same_name_pair()
        internal, named = self._position_pair()
        pairs = {(p.duplicate.pk, p.keep.pk, p.reason) for p in duplicates.find_candidates()}
        self.assertEqual(pairs, {(dup.pk, keep.pk, duplicates.SAME_NAME),
                                 (internal.pk, named.pk, duplicates.POSITION)})

    def test_far_apart_transients_are_not_candidates(self):
        disc = timezone.now() - datetime.timedelta(days=10)
        a = create_minimal_transient(self.cron, name="1dfaraa", ra=120.0, dec=10.0)
        b = create_minimal_transient(self.cron, name="2026far", ra=121.0, dec=10.0)
        Transient.objects.filter(pk__in=(a.pk, b.pk)).update(disc_date=disc)
        self.assertEqual(duplicates.find_candidates(), [])

    def test_report_only_by_default_changes_nothing_and_reports_once(self):
        dup, keep = self._same_name_pair()
        internal, named = self._position_pair()
        before = set(Transient.objects.values_list("pk", flat=True))
        result = duplicates.run_check()
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["merged"], 0)
        self.assertEqual(set(Transient.objects.values_list("pk", flat=True)), before)
        self.assertEqual(self._admin_notes().count(), 2)
        self.assertIn("transient_duplicates merge %s %s --yes" % (dup.pk, keep.pk),
                      " ".join(self._admin_notes().values_list("text", flat=True)))
        duplicates.run_check()  # the next run does not repeat the same report
        self.assertEqual(self._admin_notes().count(), 2)
        # non-superusers are not on the default list
        self.assertFalse(Notification.objects.filter(recipient=self.cron, subject=duplicates.SUBJECT).exists())

    @override_settings(DUPLICATE_REPORT_USERNAMES=SERVICE_USERNAME)
    def test_recipients_setting(self):
        self._same_name_pair()
        duplicates.run_check()
        self.assertTrue(Notification.objects.filter(recipient=self.cron, subject=duplicates.SUBJECT).exists())
        self.assertFalse(self._admin_notes().exists())

    def test_a_failing_notification_queue_does_not_break_the_check(self):
        self._same_name_pair()
        with mock.patch("YSE_App.services.notify.notify", side_effect=RuntimeError("queue down")):
            self.assertEqual(duplicates.run_check()["reported"], 0)

    def test_failure_report_names_the_job(self):
        duplicates.report_failure(ValueError("synthetic"))
        note = Notification.objects.get(recipient=self.admin, subject=duplicates.FAILURE_SUBJECT)
        self.assertIn("CheckDuplicates", note.text)
        self.assertIn("ValueError: synthetic", note.text)


class MergeTests(DuplicateTestBase):
    def test_merge_moves_data_keeps_name_and_deletes_duplicate(self):
        dup, keep = self._same_name_pair()
        Transient.objects.filter(pk=dup.pk).update(name="1dmerged")
        dup.refresh_from_db()
        phot = attach_synthetic_photometry(self.cron, dup, n_points=3)
        spec = attach_synthetic_spectrum(self.cron, dup)
        tag, _ = TransientTag.objects.get_or_create(name="synthetic-dup-tag",
                                                    defaults={"created_by": self.cron, "modified_by": self.cron})
        dup.tags.add(tag)
        counts = duplicates.merge(dup, keep)
        self.assertEqual((counts["photometry"], counts["spectra"], counts["tags"]), (1, 1, 1))
        self.assertFalse(Transient.objects.filter(pk=dup.pk).exists())
        self.assertEqual(TransientPhotometry.objects.get(pk=phot.pk).transient_id, keep.pk)
        self.assertEqual(TransientSpectrum.objects.get(pk=spec.pk).transient_id, keep.pk)
        self.assertTrue(keep.tags.filter(pk=tag.pk).exists())
        self.assertTrue(AlternateTransientNames.objects.filter(transient=keep, name="1dmerged").exists())

    def test_merge_is_all_or_nothing(self):
        dup, keep = self._same_name_pair()
        phot = attach_synthetic_photometry(self.cron, dup, n_points=2)
        with mock.patch.object(Transient, "delete", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):
                duplicates.merge(dup, keep)
        self.assertTrue(Transient.objects.filter(pk=dup.pk).exists())
        self.assertEqual(TransientPhotometry.objects.get(pk=phot.pk).transient_id, dup.pk)

    def test_merge_into_itself_is_refused(self):
        keep = create_minimal_transient(self.cron, name="2026self")
        with self.assertRaises(ValueError):
            duplicates.merge(keep, keep)

    @override_settings(DUPLICATE_AUTO_MERGE=True)
    def test_auto_merge_flag_merges_and_survives_a_bad_pair(self):
        dup, keep = self._same_name_pair()
        internal, named = self._position_pair()
        real_merge = duplicates.merge

        def flaky(duplicate, kept, **kw):
            if duplicate.pk == internal.pk:
                raise RuntimeError("synthetic failure")
            return real_merge(duplicate, kept, **kw)

        with mock.patch.object(duplicates, "merge", side_effect=flaky):
            result = duplicates.run_check()
        self.assertEqual((result["merged"], result["failed"]), (1, 1))
        self.assertFalse(Transient.objects.filter(pk=dup.pk).exists())
        self.assertTrue(Transient.objects.filter(pk=internal.pk).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.admin,
                                                    subject=duplicates.FAILURE_SUBJECT).exists())


class CommandTests(DuplicateTestBase):
    def _call(self, *args):
        out = StringIO()
        call_command("transient_duplicates", *args, stdout=out)
        return out.getvalue()

    def test_list_and_dry_run_change_nothing_then_yes_merges(self):
        dup, keep = self._same_name_pair()
        attach_synthetic_photometry(self.cron, dup, n_points=2)
        self.assertIn("1 candidate pair(s)", self._call("list"))
        out = self._call("merge", str(dup.pk), str(keep.pk))
        self.assertIn("dry run", out)
        self.assertTrue(Transient.objects.filter(pk=dup.pk).exists())
        out = self._call("merge", str(dup.pk), keep.slug, "--yes")
        self.assertIn("photometry=1", out)
        self.assertFalse(Transient.objects.filter(pk=dup.pk).exists())

    def test_ambiguous_name_needs_an_id(self):
        from django.core.management.base import CommandError

        dup, keep = self._same_name_pair()
        with self.assertRaises(CommandError):
            self._call("merge", "nonexistent-synthetic", str(keep.pk))
        with self.assertRaises(CommandError):
            self._call("merge", str(keep.pk), str(keep.pk))


@override_settings(API_SERVICE_USERNAMES=[SERVICE_USERNAME])
class UploadTests(DuplicateTestBase):
    def test_upload_never_deletes_same_name_transients(self):
        from YSE_App.data_utils import add_transient_payload

        dup, keep = self._same_name_pair("2026upld")
        attach_synthetic_photometry(self.cron, dup, n_points=2)
        doc = {"2026upld": {"name": "2026upld", "mw_ebv": 0.04}, "noupdatestatus": True}
        response = add_transient_payload(doc, self.cron)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Transient.objects.filter(pk=dup.pk).exists())
        self.assertTrue(TransientPhotometry.objects.filter(transient=dup).exists())
        keep.refresh_from_db()
        dup.refresh_from_db()
        self.assertAlmostEqual(keep.mw_ebv, 0.04)  # the oldest one is updated
        self.assertIsNone(dup.mw_ebv)
        self.assertTrue(self._admin_notes().filter(text__contains="(upload)").exists())


class CronTests(DuplicateTestBase):
    def _cron(self):
        from YSE_App.tests.test_cron_smoke import _load_cron, _sandbox

        cls, skip, failure = _load_cron("YSE_App.data_ingest.QUB_data.CheckDuplicates")
        if skip:
            self.skipTest(skip)
        self.assertIsNone(failure)
        return cls, _sandbox

    def test_error_path_reports_instead_of_crashing(self):
        cls, sandbox = self._cron()
        with sandbox(), mock.patch.object(duplicates, "run_check", side_effect=ValueError("synthetic")):
            cls().do()  # the old handler raised NameError here
        self.assertTrue(Notification.objects.filter(recipient=self.admin,
                                                    subject=duplicates.FAILURE_SUBJECT).exists())

    def test_cron_reports_without_deleting(self):
        cls, sandbox = self._cron()
        dup, keep = self._same_name_pair()
        with sandbox():
            cls().do()
        self.assertTrue(Transient.objects.filter(pk=dup.pk).exists())
        self.assertEqual(self._admin_notes().count(), 1)
