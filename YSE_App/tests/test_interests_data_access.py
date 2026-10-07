"""Transient interests (#288: #289, #290) and data access requests (#291: #292, #293)."""

from __future__ import annotations

from django.contrib.auth.models import Group
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from rest_framework.exceptions import PermissionDenied, ValidationError

from YSE_App.data import PhotometryService, SpectraService
from YSE_App.models import (
    DataAccessRequest,
    Job,
    Log,
    Notification,
    NotificationPreference,
    SourceInterest,
    TransientInterest,
    TransientPhotometry,
    TransientSpectrum,
)
from YSE_App.models.notification_models import kind_group
from YSE_App.services import data_access as dar
from YSE_App.services import interests as svc
from YSE_App.services import notify as notify_svc
from YSE_App.services.visibility import user_can_view_transient
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    attach_synthetic_spectrum,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
EMAIL_ON = dict(NOTIFICATION_EMAIL_ENABLED=True, EMAIL_BACKEND=LOCMEM,
                NOTIFICATION_BASE_URL="https://ziggy.example/yse/", SLACK_ENABLED=False)


def _member(username, *groups, staff=False, email=None):
    user = create_test_user(username, is_staff=staff, is_superuser=False, email=email or "%s@example.com" % username)
    user.groups.set([g for g in groups])
    return user


def _restricted(user, transient, group, *, spectrum=False, obs_group=None, instrument=None, band=None):
    if spectrum:
        row = attach_synthetic_spectrum(user, transient, obs_group=obs_group, instrument=instrument)
    else:
        row = attach_synthetic_photometry(user, transient, obs_group=obs_group, instrument=instrument, band=band,
                                          n_points=3)
    row.groups.add(group)
    return row


class InterestBase(TestCase):
    def setUp(self):
        self.yse = Group.objects.create(name="YSE")
        self.ucsc = Group.objects.create(name="UCSC")
        self.admin = create_test_user("int_admin", is_staff=True, is_superuser=True)
        self.alice = _member("alice", self.yse)
        self.bob = _member("bob", self.yse, self.ucsc)
        self.carol = _member("carol", self.ucsc)
        self.transient = create_minimal_transient(self.admin, name="2026int", obs_group_name="int-group")
        # Public photometry so every collaborator may see (and comment on) the transient.
        attach_synthetic_photometry(self.admin, self.transient, n_points=3)


class InterestServiceTests(InterestBase):
    def test_alias_and_defaults(self):
        self.assertIs(SourceInterest, TransientInterest)
        interest = svc.register_interest(self.transient, self.alice, "Nebular spectra", comment=False,
                                         notify_others=False)
        self.assertEqual(interest.status, TransientInterest.STATUS_PLANNED)
        self.assertEqual(interest.role, TransientInterest.ROLE_LEAD)
        self.assertTrue(interest.is_open)
        self.assertEqual(interest.created_by, self.alice)
        self.assertIn("Nebular spectra", str(interest))

    def test_register_posts_comment_with_group_audience(self):
        interest = svc.register_interest(self.transient, self.bob, "UCSC host paper", group=self.ucsc,
                                         description="Host galaxy study")
        log = Log.objects.filter(transient=self.transient, comment__contains="registered an interest").get()
        self.assertEqual(log.created_by, self.bob)
        self.assertIn("UCSC host paper", log.comment)
        self.assertIn("bob", log.comment)
        self.assertFalse(log.is_public)
        self.assertEqual(list(log.groups.values_list("name", flat=True)), ["UCSC"])
        self.assertEqual(interest.group, self.ucsc)
        # the comment shows up in the comments panel for a UCSC member and not for a YSE-only member
        from YSE_App.services.comments import transient_comment_queryset

        self.assertIn(log, list(transient_comment_queryset(self.transient.id, user=self.carol)))
        self.assertNotIn(log, list(transient_comment_queryset(self.transient.id, user=self.alice)))

    def test_register_without_group_uses_default_audience(self):
        svc.register_interest(self.transient, self.alice, "Light-curve paper")
        log = Log.objects.filter(transient=self.transient, comment__contains="registered an interest").get()
        # alice's only collaboration group is YSE (plus Public via the signal) -> private to her groups
        names = set(log.groups.values_list("name", flat=True))
        self.assertTrue(log.is_public or "YSE" in names)

    def test_uniqueness_and_revive(self):
        svc.register_interest(self.transient, self.alice, "Paper A", comment=False, notify_others=False)
        with self.assertRaises(ValidationError):
            svc.register_interest(self.transient, self.alice, "Paper A", comment=False, notify_others=False)
        # same title by someone else is fine
        svc.register_interest(self.transient, self.bob, "Paper A", comment=False, notify_others=False)
        # withdrawn -> registering again revives the row instead of failing
        interest = TransientInterest.objects.get(user=self.alice, title="Paper A")
        svc.update_interest_status(interest, self.alice, TransientInterest.STATUS_WITHDRAWN, comment=False)
        revived = svc.register_interest(self.transient, self.alice, "Paper A", comment=False, notify_others=False)
        self.assertEqual(revived.pk, interest.pk)
        self.assertEqual(revived.status, TransientInterest.STATUS_PLANNED)
        self.assertEqual(TransientInterest.objects.filter(user=self.alice, title="Paper A").count(), 1)

    def test_validation_and_permissions(self):
        with self.assertRaises(ValidationError):
            svc.register_interest(self.transient, self.alice, "   ", comment=False)
        with self.assertRaises(PermissionDenied):
            svc.register_interest(self.transient, self.alice, "Not my group", group=self.ucsc, comment=False)
        with self.assertRaises(ValidationError):
            svc.register_interest(self.transient, self.alice, "Bad role", role="pi", comment=False)
        interest = svc.register_interest(self.transient, self.alice, "Mine", comment=False, notify_others=False)
        with self.assertRaises(PermissionDenied):
            svc.update_interest_status(interest, self.bob, TransientInterest.STATUS_WITHDRAWN)
        # staff may
        svc.update_interest_status(interest, self.admin, TransientInterest.STATUS_IN_PROGRESS, comment=False)
        interest.refresh_from_db()
        self.assertEqual(interest.status, TransientInterest.STATUS_IN_PROGRESS)
        with self.assertRaises(ValidationError):
            svc.update_interest_status(interest, self.alice, "done")

    def test_withdraw_and_publish_post_comments(self):
        interest = svc.register_interest(self.transient, self.alice, "Paper B", comment=False, notify_others=False)
        svc.update_interest_status(interest, self.alice, TransientInterest.STATUS_PUBLISHED, doi="10.1000/xyz")
        self.assertTrue(Log.objects.filter(transient=self.transient, comment__contains="published: Paper B (10.1000/xyz)").exists())
        svc.update_interest_status(interest, self.alice, TransientInterest.STATUS_WITHDRAWN)
        self.assertTrue(Log.objects.filter(transient=self.transient, comment__contains="withdrew the interest: Paper B").exists())
        # in_progress is silent
        n = Log.objects.filter(transient=self.transient).count()
        svc.update_interest_status(interest, self.alice, TransientInterest.STATUS_IN_PROGRESS)
        self.assertEqual(Log.objects.filter(transient=self.transient).count(), n)

    def test_other_holders_are_notified(self):
        self.assertEqual(kind_group("interest"), "collaboration")
        svc.register_interest(self.transient, self.alice, "First paper", comment=False)
        self.assertEqual(Notification.objects.count(), 0, "nobody else to tell yet")
        svc.register_interest(self.transient, self.bob, "Second paper", comment=False)
        rows = Notification.objects.filter(kind="interest")
        self.assertEqual([n.recipient for n in rows], [self.alice])
        self.assertIn("Second paper", rows[0].text)
        self.assertEqual(rows[0].transient, self.transient)
        self.assertEqual(rows[0].url, "/transient_detail/%s/" % self.transient.slug)
        # the actor is never notified; withdrawn holders are not either
        svc.update_interest_status(TransientInterest.objects.get(title="First paper"), self.alice,
                                   TransientInterest.STATUS_WITHDRAWN, comment=False)
        svc.register_interest(self.transient, self.carol, "Third paper", comment=False)
        self.assertEqual(set(Notification.objects.filter(kind="interest", text__contains="Third").values_list(
            "recipient__username", flat=True)), {"bob"})

    def test_queryset_helpers(self):
        a = svc.register_interest(self.transient, self.alice, "A", comment=False, notify_others=False)
        svc.register_interest(self.transient, self.bob, "B", comment=False, notify_others=False)
        svc.update_interest_status(a, self.alice, TransientInterest.STATUS_WITHDRAWN, comment=False)
        self.assertEqual([i.title for i in svc.interest_queryset(self.transient.id)], ["B"])
        self.assertEqual({i.title for i in svc.interest_queryset(self.transient.id, include_withdrawn=True)}, {"A", "B"})
        self.assertEqual([i.title for i in svc.interests_for_user(self.alice)], ["A"])
        self.assertEqual([i.title for i in svc.interests_for_user(self.alice, include_withdrawn=False)], [])
        self.assertEqual(svc.open_interest_counts([self.transient.id]), {self.transient.id: 1})


class InterestPageTests(InterestBase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.alice)
        self.detail = reverse("transient_detail", kwargs={"slug": self.transient.slug})

    def test_panel_renders_empty_and_with_rows(self):
        response = self.client.get(self.detail)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="yse-interests-section"')
        self.assertContains(response, "Nobody has registered an interest")
        self.assertContains(response, 'data-count="0"')
        self.assertContains(response, 'id="yse-interest-register-btn"')
        a = svc.register_interest(self.transient, self.alice, "Alice paper", comment=False, notify_others=False)
        svc.register_interest(self.transient, self.bob, "Bob paper", group=self.ucsc, comment=False, notify_others=False)
        w = svc.register_interest(self.transient, self.carol, "Gone paper", comment=False, notify_others=False)
        svc.update_interest_status(w, self.carol, TransientInterest.STATUS_WITHDRAWN, comment=False)
        response = self.client.get(self.detail)
        self.assertContains(response, 'data-count="2"')
        self.assertContains(response, "Alice paper")
        self.assertContains(response, "Bob paper")
        self.assertContains(response, "(UCSC)")
        self.assertNotContains(response, "Gone paper", msg_prefix="withdrawn rows hidden by default")
        # only the owner gets the status form for their row
        self.assertContains(response, reverse("interest_status", kwargs={"interest_id": a.pk}))
        self.assertNotContains(response, reverse("interest_status", kwargs={"interest_id":
                                                                            TransientInterest.objects.get(title="Bob paper").pk}))
        response = self.client.get(self.detail + "?interests=all")
        self.assertContains(response, "Gone paper")

    def test_register_and_status_forms(self):
        response = self.client.post(reverse("interest_register", kwargs={"transient_id": self.transient.pk}),
                                    {"title": "Form paper", "group": self.yse.pk, "role": "coauthor",
                                     "description": "via the form"})
        self.assertEqual(response.status_code, 302)
        interest = TransientInterest.objects.get(title="Form paper")
        self.assertEqual(interest.user, self.alice)
        self.assertEqual(interest.group, self.yse)
        self.assertEqual(interest.role, "coauthor")
        self.assertTrue(Log.objects.filter(comment__contains="registered an interest: Form paper").exists())
        response = self.client.get(self.detail)
        self.assertContains(response, "Interest registered: Form paper.")
        # duplicate -> error message, no second row
        response = self.client.post(reverse("interest_register", kwargs={"transient_id": self.transient.pk}),
                                    {"title": "Form paper"}, follow=True)
        self.assertContains(response, "Interest not registered")
        self.assertEqual(TransientInterest.objects.filter(title="Form paper").count(), 1)
        # status change with next=
        response = self.client.post(reverse("interest_status", kwargs={"interest_id": interest.pk}),
                                    {"status": "published", "doi": "10.1/abc", "next": reverse("my_interests")})
        self.assertRedirects(response, reverse("my_interests"), fetch_redirect_response=False)
        interest.refresh_from_db()
        self.assertEqual(interest.status, "published")
        self.assertEqual(interest.doi, "10.1/abc")
        # someone else cannot change it
        other = Client()
        other.force_login(self.bob)
        response = other.post(reverse("interest_status", kwargs={"interest_id": interest.pk}), {"status": "withdrawn"},
                              follow=True)
        self.assertContains(response, "Interest not updated")
        interest.refresh_from_db()
        self.assertEqual(interest.status, "published")

    def test_my_interests_page(self):
        response = self.client.get(reverse("my_interests"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You have not registered an interest yet")
        a = svc.register_interest(self.transient, self.alice, "Alice paper", comment=False, notify_others=False)
        svc.update_interest_status(a, self.alice, TransientInterest.STATUS_WITHDRAWN, comment=False)
        svc.register_interest(self.transient, self.alice, "Alice paper 2", comment=False, notify_others=False)
        svc.register_interest(self.transient, self.bob, "Bob paper", comment=False, notify_others=False)
        response = self.client.get(reverse("my_interests"))
        self.assertContains(response, "Alice paper 2")
        self.assertNotContains(response, "Bob paper")
        self.assertNotContains(response, 'data-status="withdrawn"')
        response = self.client.get(reverse("my_interests") + "?show=all")
        self.assertContains(response, 'data-status="withdrawn"')
        # link in the user menu
        self.assertContains(response, reverse("my_interests"))
        self.assertContains(response, reverse("data_access_requests"))

    def test_anonymous_redirected(self):
        response = Client().get(reverse("my_interests"))
        self.assertEqual(response.status_code, 302)
        response = Client().post(reverse("interest_register", kwargs={"transient_id": self.transient.pk}), {"title": "x"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(TransientInterest.objects.exists())


class InterestApiTests(InterestBase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.alice)

    def test_list_create_update(self):
        response = self.client.post("/api/transientinterests/", {
            "transient": self.transient.pk, "title": "API paper", "group": self.yse.pk, "role": "lead",
        })
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["user"], "alice")
        self.assertEqual(data["group_name"], "YSE")
        self.assertEqual(data["status"], "planned")
        self.assertTrue(Log.objects.filter(comment__contains="registered an interest: API paper").exists())
        w = svc.register_interest(self.transient, self.bob, "Withdrawn", comment=False, notify_others=False)
        svc.update_interest_status(w, self.bob, TransientInterest.STATUS_WITHDRAWN, comment=False)
        response = self.client.get("/api/transientinterests/?transient=%s" % self.transient.pk)
        titles = [row["title"] for row in response.json()["results"]] if "results" in response.json() else \
            [row["title"] for row in response.json()]
        self.assertEqual(titles, ["API paper"])
        response = self.client.get("/api/transientinterests/?transient=%s&include_withdrawn=1" % self.transient.pk)
        body = response.json()
        rows = body["results"] if isinstance(body, dict) else body
        self.assertEqual({r["title"] for r in rows}, {"API paper", "Withdrawn"})
        response = self.client.get("/api/transientinterests/?mine=1")
        body = response.json()
        rows = body["results"] if isinstance(body, dict) else body
        self.assertEqual([r["title"] for r in rows], ["API paper"])
        # status via PATCH posts the comment
        response = self.client.patch("/api/transientinterests/%s/" % data["id"],
                                     data='{"status": "published", "doi": "10.2/z"}', content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(Log.objects.filter(comment__contains="published: API paper (10.2/z)").exists())
        # bob cannot edit alice's
        other = Client()
        other.force_login(self.bob)
        response = other.patch("/api/transientinterests/%s/" % data["id"], data='{"status": "withdrawn"}',
                               content_type="application/json")
        self.assertEqual(response.status_code, 403)
        # validation errors are 400
        response = self.client.post("/api/transientinterests/", {"transient": self.transient.pk, "title": "API paper"})
        self.assertEqual(response.status_code, 400)

    def test_anonymous_denied(self):
        response = Client().get("/api/transientinterests/")
        self.assertIn(response.status_code, (401, 403))


class DataAccessBase(TestCase):
    def setUp(self):
        self.owners = Group.objects.create(name="DEBASS")
        self.mine = Group.objects.create(name="UCSC")
        self.other = Group.objects.create(name="YSE")
        self.admin = create_test_user("dar_admin", is_staff=True, is_superuser=True)
        self.owner = _member("owner", self.owners)
        self.owner2 = _member("owner2", self.owners)
        self.requester = _member("req", self.mine)
        self.outsider = _member("outsider", self.other)
        self.transient = create_minimal_transient(self.admin, name="2026dar", obs_group_name="dar-group")
        self.obs_group, self.instrument, self.band = create_instrument_stack(self.admin, obs_group_name="dar-group")
        # one public photometry set so the requester can open the transient at all
        self.public_phot = attach_synthetic_photometry(self.admin, self.transient, obs_group=self.obs_group,
                                                       instrument=self.instrument, band=self.band, n_points=3)
        self.phot = _restricted(self.admin, self.transient, self.owners, obs_group=self.obs_group,
                                instrument=self.instrument, band=self.band)
        self.spec = _restricted(self.admin, self.transient, self.owners, spectrum=True, obs_group=self.obs_group,
                                instrument=self.instrument)
        self.spec2 = _restricted(self.admin, self.transient, self.owners, spectrum=True, obs_group=self.obs_group,
                                 instrument=self.instrument)

    def _visible_spectra(self, user):
        return set(SpectraService.GetAuthorizedTransientSpectrum_ByUser_ByTransient(user, self.transient.id)
                   .values_list("pk", flat=True))

    def _visible_phot(self, user):
        return set(PhotometryService.GetAuthorizedTransientPhotometry_ByUser_ByTransient(user, self.transient.id)
                   .values_list("pk", flat=True))


class DataAccessServiceTests(DataAccessBase):
    def test_count_hidden_and_summary(self):
        counts = dar.count_hidden(self.transient.id, self.requester)
        self.assertEqual(counts, {"photometry": {"DEBASS": 1}, "spectrum": {"DEBASS": 2}})
        self.assertEqual(dar.count_hidden(self.transient.id, self.owner), {"photometry": {}, "spectrum": {}})
        # staff are not special here: the data services filter by group name, so the page hides the rows from them too
        self.assertEqual(dar.count_hidden(self.transient.id, self.admin), counts)
        rows = dar.hidden_summary(self.transient, self.requester)
        self.assertEqual([(r["kind"], r["group"].name, r["count"], r["kind_label"], r["can_request"]) for r in rows],
                         [("photometry", "DEBASS", 1, "photometry set", True), ("spectrum", "DEBASS", 2, "spectra", True)])
        self.assertIsNone(rows[0]["request"])
        self.assertEqual(dar.hidden_summary(self.transient, self.requester, kinds=("spectrum",))[0]["kind"], "spectrum")
        self.assertEqual(dar.hidden_summary(self.transient, self.owner), [])
        self.assertEqual(len(dar.hidden_summary(self.transient, self.admin)), 2)
        # a user with only the Public group cannot be granted into anything
        lonely = _member("lonely")
        self.assertEqual(dar.requestable_groups(lonely), [])
        self.assertFalse(dar.hidden_summary(self.transient, lonely)[0]["can_request"])
        self.assertEqual([g.name for g in dar.requestable_groups(self.requester)], ["UCSC"])

    def test_request_creates_row_and_notifies_owners(self):
        self.assertEqual(kind_group("data_access"), "collaboration")
        req = dar.request_access(self.requester, self.transient, "spectrum", self.owners, message="For a host paper")
        self.assertEqual(req.status, "pending")
        self.assertEqual(req.target_group, self.mine, "single requestable group chosen automatically")
        self.assertEqual(req.requester, req.created_by)
        rows = Notification.objects.filter(kind="data_access").order_by("recipient__username")
        self.assertEqual([n.recipient.username for n in rows], ["owner", "owner2"])
        self.assertIn("req asks DEBASS for access to 2 restricted spectra on 2026dar", rows[0].text)
        self.assertIn("For a host paper", rows[0].text)
        self.assertIn("/data_access_requests/?request=%s" % req.pk, rows[0].text)
        self.assertEqual(rows[0].url, "/data_access_requests/?request=%s" % req.pk)
        # collaboration kind: email on by default -> a delivery job per owner with an email address
        self.assertEqual(Job.objects.filter(kind=notify_svc.DELIVER_KIND).count(), 0,
                         "email is off on this test server (NOTIFICATION_EMAIL_ENABLED False)")
        # summary now shows the pending request
        row = dar.hidden_summary(self.transient, self.requester, kinds=("spectrum",))[0]
        self.assertEqual(row["request"], req)
        # duplicate pending -> error
        with self.assertRaises(ValidationError):
            dar.request_access(self.requester, self.transient, "spectrum", self.owners)

    @override_settings(**EMAIL_ON)
    def test_request_enqueues_email_when_enabled(self):
        dar.request_access(self.requester, self.transient, "photometry", self.owners)
        self.assertEqual(Job.objects.filter(kind=notify_svc.DELIVER_KIND).count(), 2)
        job = Job.objects.filter(kind=notify_svc.DELIVER_KIND).first()
        self.assertEqual(job.payload["channels"], ["email"])

    def test_request_validation(self):
        with self.assertRaises(ValidationError):
            dar.request_access(self.requester, self.transient, "images", self.owners)
        with self.assertRaises(ValidationError):  # owner group holds nothing hidden for the owner
            dar.request_access(self.owner, self.transient, "spectrum", self.owners)
        with self.assertRaises(ValidationError):  # staff with no collaboration group: nothing to grant into
            dar.request_access(self.admin, self.transient, "spectrum", self.owners)
        with self.assertRaises(ValidationError):  # wrong owner group
            dar.request_access(self.requester, self.transient, "spectrum", self.other)
        with self.assertRaises(PermissionDenied):  # target must be one of the requester's non-Public groups
            dar.request_access(self.requester, self.transient, "spectrum", self.owners, target_group=self.other)
        public = Group.objects.get(name="Public")
        with self.assertRaises(PermissionDenied):
            dar.request_access(self.requester, self.transient, "spectrum", self.owners, target_group=public)
        lonely = _member("lonely2")
        with self.assertRaises(ValidationError):
            dar.request_access(lonely, self.transient, "spectrum", self.owners)
        # a specific dataset id that is hidden works; one that is not raises
        req = dar.request_access(self.requester, self.transient, "spectrum", self.owners, dataset_id=self.spec.pk)
        self.assertEqual(req.dataset_id, self.spec.pk)
        with self.assertRaises(ValidationError):
            dar.request_access(self.requester, self.transient, "photometry", self.owners, dataset_id=self.public_phot.pk)

    def test_decide_permissions(self):
        req = dar.request_access(self.requester, self.transient, "spectrum", self.owners)
        for user in (self.requester, self.outsider):
            with self.assertRaises(PermissionDenied):
                dar.decide(req, user, True)
        self.assertTrue(dar.user_can_decide(self.owner, req))
        self.assertTrue(dar.user_can_decide(self.admin, req))
        self.assertEqual(list(dar.requests_to_decide(self.owner)), [req])
        self.assertEqual(list(dar.requests_to_decide(self.outsider)), [])
        self.assertEqual(list(dar.requests_to_decide(self.admin)), [req])
        self.assertEqual(dar.pending_count_for(self.owner), 1)
        self.assertEqual(dar.pending_count_for(self.requester), 0)

    def test_accept_grants_group_through_existing_checks(self):
        self.assertEqual(self._visible_spectra(self.requester), set())
        self.assertEqual(self._visible_phot(self.requester), {self.public_phot.pk})
        req = dar.request_access(self.requester, self.transient, "spectrum", self.owners)
        Notification.objects.all().delete()
        dar.decide(req, self.owner, True, note="welcome")
        req.refresh_from_db()
        self.assertEqual(req.status, "accepted")
        self.assertEqual(req.decided_by, self.owner)
        self.assertIsNotNone(req.decided_at)
        self.assertEqual(sorted(req.granted_dataset_ids), sorted([self.spec.pk, self.spec2.pk]))
        self.assertEqual(req.granted_count, 2)
        # the requester's group is on both spectra; the owner group is still there; photometry untouched
        for spec in (self.spec, self.spec2):
            self.assertEqual(set(spec.groups.values_list("name", flat=True)), {"DEBASS", "UCSC"})
        self.assertEqual(set(self.phot.groups.values_list("name", flat=True)), {"DEBASS"})
        # existing access helpers now include the spectra for the requester; not for the outsider
        self.assertEqual(self._visible_spectra(self.requester), {self.spec.pk, self.spec2.pk})
        self.assertEqual(self._visible_phot(self.requester), {self.public_phot.pk})
        self.assertEqual(self._visible_spectra(self.outsider), set())
        self.assertTrue(user_can_view_transient(self.requester, self.transient.id))
        # nothing left to request for spectra
        self.assertEqual(dar.count_hidden(self.transient.id, self.requester)["spectrum"], {})
        # requester notified
        note = Notification.objects.get(kind="data_access")
        self.assertEqual(note.recipient, self.requester)
        self.assertIn("DEBASS accepted your request: UCSC now sees 2 restricted spectra", note.text)
        self.assertIn("welcome", note.text)
        self.assertEqual(note.url, "/transient_detail/%s/" % self.transient.slug)
        # cannot decide twice
        with self.assertRaises(ValidationError):
            dar.decide(req, self.owner, False)

    def test_accept_single_dataset_and_idempotent_group_add(self):
        self.spec.groups.add(self.mine)  # already shared by hand
        req = dar.request_access(self.requester, self.transient, "spectrum", self.owners, dataset_id=self.spec2.pk)
        dar.decide(req, self.owner2, True)
        req.refresh_from_db()
        self.assertEqual(req.granted_dataset_ids, [self.spec2.pk])
        self.assertEqual(self.spec2.groups.filter(name="UCSC").count(), 1)

    def test_decline_changes_nothing_and_notifies(self):
        req = dar.request_access(self.requester, self.transient, "photometry", self.owners)
        Notification.objects.all().delete()
        dar.decide(req, self.owner, False, note="embargo until 2027")
        req.refresh_from_db()
        self.assertEqual(req.status, "declined")
        self.assertEqual(req.note, "embargo until 2027")
        self.assertIsNone(req.granted_dataset_ids)
        self.assertEqual(set(self.phot.groups.values_list("name", flat=True)), {"DEBASS"})
        self.assertEqual(self._visible_phot(self.requester), {self.public_phot.pk})
        note = Notification.objects.get(kind="data_access")
        self.assertEqual(note.recipient, self.requester)
        self.assertIn("declined", note.text)
        self.assertIn("embargo until 2027", note.text)
        # a declined request can be repeated
        again = dar.request_access(self.requester, self.transient, "photometry", self.owners)
        self.assertNotEqual(again.pk, req.pk)
        row = dar.hidden_summary(self.transient, self.requester, kinds=("photometry",))[0]
        self.assertEqual(row["request"], again, "latest request wins")

    def test_notify_respects_preferences(self):
        pref, _ = NotificationPreference.objects.get_or_create(user=self.owner2)
        pref.in_app = False
        pref.save()
        dar.request_access(self.requester, self.transient, "spectrum", self.owners)
        self.assertEqual(set(Notification.objects.values_list("recipient__username", flat=True)), {"owner"})


class DataAccessPageTests(DataAccessBase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.requester)
        self.detail = reverse("transient_detail", kwargs={"slug": self.transient.slug})
        self.spectra_fragment = reverse("transient_detail_spectra_tab_fragment", kwargs={"transient_id": self.transient.pk})
        self.phot_fragment = reverse("transient_detail_photometry_fragment", kwargs={"transient_id": self.transient.pk})

    def test_hint_shown_only_to_users_who_cannot_see(self):
        response = self.client.get(self.spectra_fragment)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2 spectra")
        self.assertContains(response, "restricted to <strong>DEBASS</strong>")
        self.assertContains(response, "Request access")
        self.assertContains(response, 'data-kind="spectrum"')
        self.assertContains(response, "No authorized spectra for this transient.")
        # nothing about the hidden rows leaks
        self.assertNotContains(response, self.instrument.name)
        response = self.client.get(self.phot_fragment)
        self.assertContains(response, "1 photometry set")
        self.assertContains(response, 'data-kind="photometry"')
        # summary tab carries the compact hint and the "Request access on the ... tab" links
        response = self.client.get(self.detail)
        self.assertContains(response, "yse-data-access-hint")
        self.assertContains(response, "Request access on the Spectra tab")
        # the owner sees the data and no hint
        c = Client()
        c.force_login(self.owner)
        response = c.get(self.spectra_fragment)
        self.assertNotContains(response, "yse-data-access-hint")
        self.assertContains(response, "Download spectra")
        # staff outside the group are hidden from too (the data services filter by group name) and,
        # without a collaboration group of their own, are told so instead of offered a request form
        c = Client()
        c.force_login(self.admin)
        response = c.get(self.spectra_fragment)
        self.assertContains(response, "yse-data-access-hint")
        self.assertContains(response, "You belong to no collaboration group")
        self.assertNotContains(response, "yse-data-access-request-btn")

    def test_request_form_then_pending_then_accept_reveals_data(self):
        response = self.client.post(reverse("data_access_request_create", kwargs={"transient_id": self.transient.pk}),
                                    {"kind": "spectrum", "owner_group": self.owners.pk, "target_group": self.mine.pk,
                                     "message": "please"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].endswith("#spectra_tab"))
        req = DataAccessRequest.objects.get()
        self.assertEqual(req.message, "please")
        response = self.client.get(self.detail)
        self.assertContains(response, "Access request #%d sent to DEBASS" % req.pk)
        response = self.client.get(self.spectra_fragment)
        self.assertContains(response, "Access request pending")
        self.assertContains(response, 'data-status="pending"')
        self.assertNotContains(response, "yse-data-access-request-btn")
        # requester's own inbox
        response = self.client.get(reverse("data_access_requests") + "?box=mine")
        self.assertContains(response, 'data-request-id="%d"' % req.pk)
        self.assertNotContains(response, "Accept</button>")
        # owner decides on the page
        owner = Client()
        owner.force_login(self.owner)
        response = owner.get(reverse("data_access_requests"))
        self.assertContains(response, "1 to decide")
        self.assertContains(response, 'data-request-id="%d"' % req.pk)
        self.assertContains(response, "Accept")
        # the user-menu badge is filled after load from the JSON endpoint (no query on page render)
        self.assertContains(response, 'id="yse-dar-menu-badge"')
        response = owner.get(reverse("data_access_pending_count"))
        self.assertEqual(response.json(), {"pending": 1})
        self.assertEqual(self.client.get(reverse("data_access_pending_count")).json(), {"pending": 0})
        response = owner.post(reverse("data_access_decide", kwargs={"request_id": req.pk}),
                              {"decision": "accept", "note": "ok"}, follow=True)
        self.assertContains(response, "Request #%d accepted: UCSC now sees 2 spectrum datasets" % req.pk)
        self.assertContains(response, "Nothing to decide")
        self.assertEqual(owner.get(reverse("data_access_pending_count")).json(), {"pending": 0},
                         "cached badge count invalidated by the decision")
        response = owner.get(reverse("data_access_requests") + "?box=decided")
        self.assertContains(response, "2 datasets shared")
        # acceptance criterion of #292: the spectra now appear in the fragment for the requester, nothing else changed
        response = self.client.get(self.spectra_fragment)
        self.assertNotContains(response, "yse-data-access-hint")
        self.assertContains(response, "Download spectra")
        self.assertContains(response, self.instrument.name, count=2)

    def test_decline_and_permissions_on_page(self):
        req = dar.request_access(self.requester, self.transient, "photometry", self.owners)
        outsider = Client()
        outsider.force_login(self.outsider)
        response = outsider.get(reverse("data_access_requests"))
        self.assertContains(response, "Nothing to decide")
        response = outsider.post(reverse("data_access_decide", kwargs={"request_id": req.pk}), {"decision": "accept"},
                                 follow=True)
        self.assertContains(response, "not decided")
        req.refresh_from_db()
        self.assertEqual(req.status, "pending")
        owner = Client()
        owner.force_login(self.owner)
        response = owner.post(reverse("data_access_decide", kwargs={"request_id": req.pk}),
                              {"decision": "decline", "note": "no"}, follow=True)
        self.assertContains(response, "Request #%d declined." % req.pk)
        req.refresh_from_db()
        self.assertEqual(req.status, "declined")
        response = self.client.get(self.phot_fragment)
        self.assertContains(response, "Declined")
        self.assertContains(response, "Request again")
        response = owner.post(reverse("data_access_decide", kwargs={"request_id": req.pk}), {"decision": "maybe"},
                              follow=True)
        self.assertContains(response, "Choose accept or decline.")

    def test_admin_pages_load(self):
        req = dar.request_access(self.requester, self.transient, "photometry", self.owners)
        interest = TransientInterest.objects.create(transient=self.transient, user=self.owner, title="x",
                                                    created_by=self.owner, modified_by=self.owner)
        admin = Client()
        admin.force_login(self.admin)
        for url in (reverse("admin:YSE_App_dataaccessrequest_changelist"),
                    reverse("admin:YSE_App_dataaccessrequest_change", args=[req.pk]),
                    reverse("admin:YSE_App_transientinterest_changelist"),
                    reverse("admin:YSE_App_transientinterest_change", args=[interest.pk])):
            self.assertEqual(admin.get(url).status_code, 200, url)


class DataAccessApiTests(DataAccessBase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.requester)

    def _rows(self, response):
        body = response.json()
        return body["results"] if isinstance(body, dict) and "results" in body else body

    def test_create_list_decide(self):
        response = self.client.post("/api/dataaccessrequests/", {
            "transient": self.transient.pk, "dataset_kind": "spectrum", "owner_group": self.owners.pk,
            "message": "api",
        })
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["target_group_name"], "UCSC")
        self.assertEqual(data["requester"], "req")
        # requester cannot accept
        response = self.client.post("/api/dataaccessrequests/%s/accept/" % data["id"])
        self.assertEqual(response.status_code, 403)
        # outsider cannot even see it
        outsider = Client()
        outsider.force_login(self.outsider)
        self.assertEqual(self._rows(outsider.get("/api/dataaccessrequests/")), [])
        self.assertEqual(outsider.get("/api/dataaccessrequests/%s/" % data["id"]).status_code, 404)
        # owner lists inbox and accepts
        owner = Client()
        owner.force_login(self.owner)
        rows = self._rows(owner.get("/api/dataaccessrequests/?box=inbox&status=pending"))
        self.assertEqual([r["id"] for r in rows], [data["id"]])
        response = owner.post("/api/dataaccessrequests/%s/accept/" % data["id"], {"note": "fine"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "accepted")
        self.assertEqual(response.json()["granted_count"], 2)
        self.assertEqual(response.json()["note"], "fine")
        self.assertEqual(set(self.spec.groups.values_list("name", flat=True)), {"DEBASS", "UCSC"})
        # second decision -> 400
        response = owner.post("/api/dataaccessrequests/%s/decline/" % data["id"])
        self.assertEqual(response.status_code, 400)
        # requester sees it under mine
        rows = self._rows(self.client.get("/api/dataaccessrequests/?box=mine"))
        self.assertEqual(rows[0]["status"], "accepted")
        # validation
        response = self.client.post("/api/dataaccessrequests/", {
            "transient": self.transient.pk, "dataset_kind": "spectrum", "owner_group": self.owners.pk,
        })
        self.assertEqual(response.status_code, 400, "nothing hidden any more")
