"""Creating a Transient must not reach out to HEASARC or anywhere else (#239)."""

from unittest import mock

from django.test import TestCase

from YSE_App.data_ingest import Apply_Tags
from YSE_App.models import Transient, TransientTag, transient_models
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_minimal_transient,
    create_test_user,
)


class TransientSaveNoNetworkTests(TestCase):
    def test_create_and_save_make_no_http_call(self):
        user = create_test_user("no_network_user")
        with mock.patch("requests.get") as get, mock.patch("requests.post") as post, \
                mock.patch("requests.Session.request") as session_request:
            transient = create_minimal_transient(user, name="no-network-sn")
            transient.dec = 21.0
            transient.save()
        get.assert_not_called()
        post.assert_not_called()
        session_request.assert_not_called()
        self.assertTrue(Transient.objects.filter(name="no-network-sn").exists())

    def test_post_save_handler_does_not_call_footprint_lookups(self):
        names = set(transient_models.execute_after_save.__code__.co_names)
        for name in ("tess_obs", "thacher_transient_search", "IsK2Pixel", "print"):
            self.assertNotIn(
                name, names,
                "%s is back in the Transient post_save handler; footprint tagging belongs in Apply_Tags" % name,
            )


class ApplyTagsCronTests(TestCase):
    """The cron keeps the behaviour the post_save handler used to have."""

    def setUp(self):
        self.user = create_test_user("apply_tags_user")
        audit = audit_fields(self.user)
        TransientTag.objects.get_or_create(name="TESS", defaults=audit)
        TransientTag.objects.get_or_create(name="Thacher", defaults=audit)
        self.transient = create_minimal_transient(self.user, name="apply-tags-sn")

    def test_tess_and_thacher_tags_applied_when_lookups_hit(self):
        with mock.patch.object(Apply_Tags, "tess_obs", return_value=True) as tess, \
                mock.patch.object(Apply_Tags, "thacher_transient_search", return_value=True):
            Apply_Tags.Tags().do()
        tess.assert_called_once()
        names = set(self.transient.tags.values_list("name", flat=True))
        self.assertEqual(names, {"TESS", "Thacher"})

    def test_no_tags_when_lookups_miss(self):
        with mock.patch.object(Apply_Tags, "tess_obs", return_value=False), \
                mock.patch.object(Apply_Tags, "thacher_transient_search", return_value=False):
            Apply_Tags.Tags().do()
        self.assertEqual(self.transient.tags.count(), 0)

    def test_missing_tag_row_is_logged_not_fatal(self):
        TransientTag.objects.filter(name="TESS").delete()
        with mock.patch.object(Apply_Tags, "tess_obs", return_value=True), \
                mock.patch.object(Apply_Tags, "thacher_transient_search", return_value=False), \
                self.assertLogs("YSE_App.data_ingest.Apply_Tags", level="WARNING") as logs:
            Apply_Tags.apply_footprint_tags(self.transient)
        self.assertTrue(any("TESS" in line for line in logs.output))
        self.assertEqual(self.transient.tags.count(), 0)
