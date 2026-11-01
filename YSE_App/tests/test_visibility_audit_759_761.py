"""Visibility audit (2026-10-21): the removed comments fragment (#759), the listing rule on
/get_transient/ and /get_transient_tags/ (#760), and per-recipient favorite notifications (#761).

Synthetic data only; no external calls.
"""

from __future__ import annotations

import base64
import datetime

from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.template import TemplateDoesNotExist
from django.template.loader import get_template
from django.test import Client, TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from YSE_App.jobs import run_pass
from YSE_App.models import (
    DataAccessRule,
    Job,
    Notification,
    TransientPhotData,
    TransientPhotometry,
    TransientTag,
)
from YSE_App.services import favorites as svc
from YSE_App.services import photstat
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    attach_synthetic_spectrum,
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

TEST_PASSWORD = "vis-audit-test-pass-761"
SERVICE = "vis761_service"


def _member(username, *groups):
    user = create_test_user(username, is_staff=False, is_superuser=False)
    user.groups.set(list(groups))
    return user


class CommentsFragmentRemovedTests(TestCase):
    """#759: the unscoped per-transient comments fragment is gone; the scoped panel stays."""

    def test_url_name_no_longer_exists(self):
        with self.assertRaises(NoReverseMatch):
            reverse("transient_detail_comments_fragment", args=[1])
        self.assertTrue(reverse("comments_fragment", kwargs={"slug": "x"}))

    def test_template_is_removed(self):
        with self.assertRaises(TemplateDoesNotExist):
            get_template("YSE_App/transient_detail_comments_list.html")

    def test_path_is_not_found(self):
        admin = create_test_user("vis759_admin", is_staff=True, is_superuser=True)
        transient = create_minimal_transient(admin, name="YSEvis759")
        client = Client()
        client.force_login(admin)
        response = client.get("/transient_detail/%d/comments_fragment/" % transient.id)
        self.assertEqual(response.status_code, 404)


@override_settings(ONBOARDING_GATE_ENABLED=False, DATA_SERVICE_READER_USERNAMES=[SERVICE])
class GetTransientListingTests(TestCase):
    """#760: a transient the user may not list answers like an unknown slug."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_test_user("vis760_admin", is_staff=True, is_superuser=True)
        cls.secret = Group.objects.create(name="vis760-secret")
        cls.member = _member("vis760_member", cls.secret)
        cls.outsider = _member("vis760_outsider")
        cls.staff = create_test_user("vis760_staff", is_staff=True, is_superuser=False)
        cls.service = User.objects.create(username=SERVICE, email="svc@example.com")
        cls.service.set_password(TEST_PASSWORD)
        cls.service.save()
        # Not TNS-named, with only group-restricted photometry: listed for the group only.
        cls.hidden = create_minimal_transient(cls.admin, name="YSEvis760", obs_group_name="vis760")
        attach_synthetic_photometry(cls.admin, cls.hidden, n_points=2).groups.add(cls.secret)
        # TNS-named: listed for everyone.
        cls.public = create_minimal_transient(cls.admin, name="2026vis", obs_group_name="vis760")
        cls.tag = TransientTag.objects.create(name="vis760-tag", **audit_fields(cls.admin))
        cls.hidden.tags.add(cls.tag)
        cls.public.tags.add(cls.tag)

    def get_transient(self, user, slug):
        client = Client()
        client.force_login(user)
        response = client.get(reverse("get_transient", kwargs={"slug": slug}))
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_member_sees_the_transient(self):
        body = self.get_transient(self.member, self.hidden.slug)
        self.assertEqual(body["transient"]["name"], "YSEvis760")

    def test_outsider_gets_the_unknown_slug_response(self):
        unknown = self.get_transient(self.outsider, "no-such-slug-760")
        self.assertEqual(unknown, {"transient": ""})
        self.assertEqual(self.get_transient(self.outsider, self.hidden.slug), unknown)
        self.assertEqual(self.get_transient(self.outsider, self.public.slug)["transient"]["name"], "2026vis")

    def test_staff_follow_the_listing_rule(self):
        self.assertEqual(self.get_transient(self.staff, self.hidden.slug), {"transient": ""})

    def test_service_reader_with_basic_auth_sees_everything(self):
        token = base64.b64encode(("%s:%s" % (SERVICE, TEST_PASSWORD)).encode()).decode()
        response = Client().get(reverse("get_transient", kwargs={"slug": self.hidden.slug}),
                                HTTP_AUTHORIZATION="Basic " + token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["transient"]["name"], "YSEvis760")

    def test_transient_tags_apply_the_listing_rule(self):
        def names(user):
            client = Client()
            client.force_login(user)
            response = client.get(reverse("get_transient_tags"), {"tagid": [self.tag.id]})
            self.assertEqual(response.status_code, 200)
            return sorted(t["name"] for t in response.json().values())

        self.assertEqual(names(self.member), ["2026vis", "YSEvis760"])
        self.assertEqual(names(self.outsider), ["2026vis"])
        self.assertEqual(names(self.staff), ["2026vis"])


class FavoritePhotometryVisibilityTests(TestCase):
    """#761: photometry events count and quote only points each recipient may see."""

    def setUp(self):
        cache.clear()
        self.admin = create_test_user("vis761_admin", is_staff=True, is_superuser=True)
        self.yse = Group.objects.create(name="vis761-yse")
        self.secret = Group.objects.create(name="vis761-secret")
        self.alice = _member("vis761_alice", self.yse)
        self.bob = _member("vis761_bob", self.yse)
        self.carol = _member("vis761_carol", self.secret)
        self.transient = create_minimal_transient(self.admin, name="2026fvp", obs_group_name="vis761")
        self.public_set = attach_synthetic_photometry(self.admin, self.transient, n_points=3)
        self.point = self.public_set.transientphotdata_set.first()
        photstat.recompute(self.transient.id)  # the stat row exists from now on

    def star(self, *users):
        for user in users:
            svc.add(user, self.transient)

    def activity(self, user):
        return list(Notification.objects.filter(recipient=user, kind=svc.KIND).order_by("id"))

    def restricted_set(self, *groups):
        header = TransientPhotometry.objects.create(
            transient=self.transient, instrument=self.public_set.instrument, obs_group=self.public_set.obs_group,
            **audit_fields(self.admin))
        header.groups.set(list(groups))
        return header

    def add_point(self, header, mag, minutes):
        return TransientPhotData.objects.create(
            photometry=header, band=self.point.band, mag=mag, mag_err=0.05,
            obs_date=timezone.now() + datetime.timedelta(minutes=minutes), **audit_fields(self.admin))

    def test_hidden_points_are_not_announced(self):
        self.star(self.alice, self.carol)
        secret = self.restricted_set(self.secret)
        with photstat.deferred_updates():
            self.add_point(secret, 16.5, 1)
            self.add_point(secret, 16.4, 2)
        self.assertEqual(self.activity(self.alice), [])
        [row] = self.activity(self.carol)
        self.assertIn("2 new photometry points, latest 16.40", row.text)

    def test_mixed_points_count_and_magnitude_per_recipient(self):
        self.star(self.alice, self.carol)
        secret = self.restricted_set(self.secret)
        with photstat.deferred_updates():
            self.add_point(self.public_set, 17.5, 1)
            self.add_point(secret, 16.0, 2)
        [alice_row] = self.activity(self.alice)
        self.assertIn("1 new photometry point, latest 17.50", alice_row.text)
        self.assertNotIn("16.00", alice_row.text)
        [carol_row] = self.activity(self.carol)
        self.assertIn("2 new photometry points, latest 16.00", carol_row.text)

    @override_settings(DATA_ACCESS_GRANTS_ENABLED=True)
    def test_per_point_rule_inside_one_set(self):
        """Access is per point: one set, one point hidden from alice by a rule."""
        self.star(self.alice, self.bob)
        shared = self.restricted_set(self.yse)
        with photstat.deferred_updates():
            self.add_point(shared, 17.0, 1)
            hidden = self.add_point(shared, 16.2, 2)
            DataAccessRule.objects.create(transient_phot_data=hidden, grantee_user=self.alice, effect="deny",
                                          granted_as="pi")
        [alice_row] = self.activity(self.alice)
        self.assertIn("1 new photometry point, latest 17.00", alice_row.text)
        self.assertNotIn("16.20", alice_row.text)
        [bob_row] = self.activity(self.bob)
        self.assertIn("2 new photometry points, latest 16.20", bob_row.text)


@override_settings(DATA_SERVICE_READER_USERNAMES=[SERVICE])
class FavoriteSpectrumVisibilityTests(TestCase):
    """#761: a new spectrum is announced, after its groups are set, only to who may see it."""

    def setUp(self):
        cache.clear()
        self.admin = create_test_user("vis761s_admin", is_staff=True, is_superuser=True)
        self.yse = Group.objects.create(name="vis761s-yse")
        self.secret = Group.objects.create(name="vis761s-secret")
        self.alice = _member("vis761s_alice", self.yse)
        self.carol = _member("vis761s_carol", self.secret)
        self.service = create_test_user(SERVICE, is_staff=False, is_superuser=False)
        self.transient = create_minimal_transient(self.admin, name="2026fvs", obs_group_name="vis761s")
        attach_synthetic_photometry(self.admin, self.transient, n_points=2)
        for user in (self.alice, self.carol, self.service):
            svc.add(user, self.transient)

    def activity(self, user):
        return list(Notification.objects.filter(recipient=user, kind=svc.KIND).order_by("id"))

    def run_spectrum_jobs(self):
        Job.objects.filter(kind=svc.SPECTRUM_JOB_KIND).update(
            run_after=timezone.now() - datetime.timedelta(seconds=1))
        run_pass(kinds=[svc.SPECTRUM_JOB_KIND])

    @override_settings(JOB_RUNNER_INLINE=True, NOTIFICATION_EMAIL_ENABLED=False)
    def test_restricted_spectrum_only_reaches_who_may_see_it(self):
        _og, instrument, _b = create_instrument_stack(self.admin, obs_group_name="vis761s-spec")
        spectrum = attach_synthetic_spectrum(self.admin, self.transient, instrument=instrument, obs_group=_og)
        # Never decided inline at post_save: the upload sets the groups afterwards.
        self.assertEqual(self.activity(self.alice), [])
        self.assertEqual(self.activity(self.carol), [])
        spectrum.groups.add(self.secret)
        self.run_spectrum_jobs()
        self.assertEqual(self.activity(self.alice), [])
        [carol_row] = self.activity(self.carol)
        self.assertIn("new spectrum from %s" % instrument.name, carol_row.text)
        self.assertEqual(len(self.activity(self.service)), 1)  # service readers see everything

    def test_public_spectrum_reaches_every_favoriter(self):
        attach_synthetic_spectrum(self.admin, self.transient)
        self.run_spectrum_jobs()
        for user in (self.alice, self.carol, self.service):
            self.assertEqual(len(self.activity(user)), 1, user.username)

    def test_deleted_spectrum_is_not_announced(self):
        spectrum = attach_synthetic_spectrum(self.admin, self.transient)
        spectrum.delete()
        self.run_spectrum_jobs()
        self.assertEqual(Notification.objects.filter(kind=svc.KIND).count(), 0)
