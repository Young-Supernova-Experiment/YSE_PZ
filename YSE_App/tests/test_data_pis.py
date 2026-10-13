"""PIs on private photometry and spectra (#446): setting them, notifications, deciders, display, migration.

Synthetic data only (users, groups and PIs are made up here).
"""

from __future__ import annotations

import datetime
import importlib
import json
from io import StringIO

from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection, models
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.models import (
    Allocation,
    DataAccessRequest,
    Notification,
    PrincipalInvestigator,
    TransientPhotometry,
    TransientSpectrum,
)
from YSE_App.services import data_access as dar
from YSE_App.services import data_pis
from YSE_App.services import interests as svc
from YSE_App.tests.deploy_checklist_helpers import (
    CHECKLIST_PASSWORD,
    basic_auth_header,
    create_instrument,
    create_telescope,
    ensure_observation_group,
)
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    attach_synthetic_spectrum,
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

PI_EMAIL = "pi-contact@example.com"


def _member(username, *groups, staff=False):
    user = create_test_user(username, is_staff=staff, is_superuser=False, email="%s@example.com" % username)
    user.groups.set(list(groups))
    return user


def _pi(author, name, user=None, **extra):
    return PrincipalInvestigator.objects.create(
        name=name, email=PI_EMAIL, phone="000", institution="Synthetic U", user=user, **audit_fields(author), **extra,
    )


class PiBase(TestCase):
    def setUp(self):
        self.owners = Group.objects.create(name="PIGROUP")
        self.mine = Group.objects.create(name="REQGROUP")
        self.admin = create_test_user("pi_admin", is_staff=True, is_superuser=True)
        self.member = _member("pi_member", self.owners)
        self.requester = _member("pi_req", self.mine)
        self.outsider = _member("pi_outsider")
        # the PI's account is in neither group: PIs are deciders by the data link alone
        self.pi_user = _member("pi_account")
        self.pi_user2 = _member("pi_account2")
        self.pi = _pi(self.admin, "Synthetic PI One", user=self.pi_user)
        self.pi2 = _pi(self.admin, "Synthetic PI Two", user=self.pi_user2)
        self.pi_unlinked = _pi(self.admin, "Synthetic PI Unlinked")
        self.transient = create_minimal_transient(self.admin, name="2026pi", obs_group_name="pi-group")
        self.obs_group, self.instrument, self.band = create_instrument_stack(self.admin, obs_group_name="pi-group")
        attach_synthetic_photometry(self.admin, self.transient, obs_group=self.obs_group, instrument=self.instrument,
                                    band=self.band, n_points=3)  # public, so everyone can open the transient
        self.spec = attach_synthetic_spectrum(self.admin, self.transient, obs_group=self.obs_group,
                                              instrument=self.instrument)
        self.spec.groups.add(self.owners)
        self.phot = attach_synthetic_photometry(self.admin, self.transient, obs_group=self.obs_group,
                                                instrument=self.instrument, band=self.band, n_points=2)
        self.phot.groups.add(self.owners)


class SettingPisTests(PiBase):
    def test_several_pis_and_linked_accounts(self):
        self.spec.pis.add(self.pi, self.pi2, self.pi_unlinked)
        self.assertEqual([p.name for p in data_pis.pis_for(self.spec)],
                         ["Synthetic PI One", "Synthetic PI Two", "Synthetic PI Unlinked"])
        # only linked, active accounts can be notified or decide
        self.assertEqual(data_pis.pi_users_for(self.spec), [self.pi_user, self.pi_user2])
        self.pi_user2.is_active = False
        self.pi_user2.save()
        self.assertEqual(data_pis.pi_users_for([self.spec, self.phot]), [self.pi_user])
        self.assertEqual(data_pis.pis_for(TransientSpectrum()), [])
        # the program hook adds nothing yet
        self.assertEqual(data_pis._program_pis(self.spec), [])

    def test_resolve_and_apply_upload_pis(self):
        pis, unknown = data_pis.resolve_pis("%d, %d,999999" % (self.pi.pk, self.pi2.pk))
        self.assertEqual({p.pk for p in pis}, {self.pi.pk, self.pi2.pk})
        self.assertEqual(unknown, ["999999"])
        self.assertEqual(data_pis.resolve_pis(None), ([], []))
        self.assertEqual(data_pis.resolve_pis([self.pi.pk, str(self.pi.pk)])[0], [self.pi])
        self.assertEqual(data_pis.resolve_pis(["Someone"])[1], ["Someone"], "names are not guessed")
        self.assertEqual(data_pis.apply_upload_pis(self.phot, [self.pi.pk]), [])
        self.assertEqual(list(self.phot.pis.all()), [self.pi])
        data_pis.apply_upload_pis(self.phot, [self.pi2.pk])
        self.assertEqual(set(self.phot.pis.all()), {self.pi, self.pi2}, "uploads add, never remove")

    def test_user_can_edit_pis(self):
        self.assertTrue(data_pis.user_can_edit_pis(self.member, self.spec))
        self.assertTrue(data_pis.user_can_edit_pis(self.admin, self.spec))
        self.assertFalse(data_pis.user_can_edit_pis(self.requester, self.spec))
        self.assertFalse(data_pis.user_can_edit_pis(self.pi_user, self.spec))


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class UploadPisTests(TestCase):
    def setUp(self):
        self.user = create_test_user("pi_uploader", is_staff=True)
        self.user.set_password(CHECKLIST_PASSWORD)
        self.user.save()
        self.transient = create_minimal_transient(self.user, name="2026piup", obs_group_name="piup-group")
        self.obs_group, self.instrument, self.band = create_instrument_stack(self.user, obs_group_name="piup-group")
        telescope = create_telescope(self.user, "PiUploadTel")
        self.ucsc = ensure_observation_group(self.user, "UCSC")  # the upload form's obs-group whitelist
        self.kast, _ = create_instrument(self.user, telescope, "KAST")
        self.pi = _pi(self.user, "Upload PI A")
        self.pi2 = _pi(self.user, "Upload PI B")
        self.client = Client()
        self.client.force_login(self.user)

    def test_upload_spectrum_form_sets_pis(self):
        response = self.client.get(reverse("upload_spectrum"))
        self.assertContains(response, 'name="pis"')
        payload = {
            "transient": self.transient.id, "ra": self.transient.ra, "dec": self.transient.dec,
            "spec_phase": "", "obs_date": "2026-09-01T03:00", "obs_group": self.ucsc.id,
            "instrument": self.kast.id, "data_quality": "", "pis": [self.pi.pk, self.pi2.pk],
            "filename": SimpleUploadedFile("spec.txt", b"4000 1.0 0.1\n4010 1.1 0.1\n"),
        }
        response = self.client.post(reverse("upload_spectrum"), payload)
        self.assertEqual(response.status_code, 302, getattr(response, "content", b""))
        spec = TransientSpectrum.objects.get(transient=self.transient, instrument=self.kast)
        self.assertEqual(set(spec.pis.all()), {self.pi, self.pi2})

    def test_add_transient_spec_api_accepts_pis(self):
        payload = {
            "header": {
                "instrument": self.instrument.name, "obs_group": self.obs_group.name, "groups": "",
                "data_quality": None, "obs_date": "2026-09-02T00:00:00", "ra": self.transient.ra,
                "dec": self.transient.dec, "rlap": None, "redshift": None, "redshift_err": None,
                "redshift_quality": None, "spectrum_notes": "pi upload", "spec_phase": None, "clobber": False,
                "pis": [self.pi.pk],
            },
            "transient": {"name": self.transient.name},
            "0": {"wavelength": 5000.0, "flux": 1.0, "flux_err": 0.1},
        }
        response = self.client.post(
            "/add_transient_spec/", data=json.dumps(payload), content_type="application/json",
            HTTP_AUTHORIZATION=basic_auth_header(self.user.username, CHECKLIST_PASSWORD),
        )
        self.assertEqual(response.status_code, 200, response.content)
        spec = TransientSpectrum.objects.get(transient=self.transient, spectrum_notes="pi upload")
        self.assertEqual(list(spec.pis.all()), [self.pi])

    def test_pi_api_does_not_expose_account_link(self):
        account = create_test_user("pi_linked_account")
        self.pi.user = account
        self.pi.save()
        response = self.client.get("/api/principalinvestigators/%d/" % self.pi.pk)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("user", response.json())


class PiNotificationTests(PiBase):
    def test_access_request_notifies_pis_and_group(self):
        self.spec.pis.add(self.pi, self.pi_unlinked)
        req = dar.request_access(self.requester, self.transient, "spectrum", self.owners)
        recipients = set(Notification.objects.filter(kind="data_access").values_list("recipient__username", flat=True))
        self.assertEqual(recipients, {"pi_account", "pi_member"})
        # the PI's inbox lists the request even though the PI is not in the owner group
        self.assertEqual(list(dar.requests_to_decide(self.pi_user)), [req])
        self.assertEqual(list(dar.requests_to_decide(self.pi_user2)), [])
        self.assertEqual(dar.pending_count_for(self.pi_user), 1)

    def test_request_for_one_dataset_reaches_only_its_pis(self):
        spec2 = attach_synthetic_spectrum(self.admin, self.transient, obs_group=self.obs_group,
                                          instrument=self.instrument)
        spec2.groups.add(self.owners)
        self.spec.pis.add(self.pi)
        spec2.pis.add(self.pi2)
        req = dar.request_access(self.requester, self.transient, "spectrum", self.owners, dataset_id=spec2.pk)
        recipients = set(Notification.objects.filter(kind="data_access").values_list("recipient__username", flat=True))
        self.assertEqual(recipients, {"pi_account2", "pi_member"})
        self.assertEqual(list(dar.requests_to_decide(self.pi_user2)), [req])
        self.assertEqual(list(dar.requests_to_decide(self.pi_user)), [])

    def test_public_interest_with_request_notifies_pis_once_and_files_requests(self):
        self.spec.pis.add(self.pi)
        self.phot.pis.add(self.pi2)
        interest = svc.register_interest(self.transient, self.requester, "Nebular paper", comment=False,
                                         notify_others=False, notify_pis=True, request_data=True)
        reqs = DataAccessRequest.objects.filter(requester=self.requester).order_by("dataset_kind")
        self.assertEqual([(r.dataset_kind, r.owner_group.name, r.target_group.name) for r in reqs],
                         [("photometry", "PIGROUP", "REQGROUP"), ("spectrum", "PIGROUP", "REQGROUP")])
        self.assertEqual(len(interest.pi_outcome["requests"]), 2)
        # each PI gets exactly one data-access notification (with the inbox link), and no second "interest" one
        pi_notes = Notification.objects.filter(recipient=self.pi_user)
        self.assertEqual(pi_notes.count(), 1)
        self.assertEqual(pi_notes.get().kind, "data_access")
        self.assertIn("/data_access_requests/?request=", pi_notes.get().url)
        self.assertEqual(Notification.objects.filter(recipient=self.pi_user2).count(), 1)
        # the PI shares: the spectrum appears for the requester
        spec_req = reqs.get(dataset_kind="spectrum")
        self.assertTrue(dar.user_can_decide(self.pi_user, spec_req))
        dar.decide(spec_req, self.pi_user, True)
        self.assertTrue(self.spec.groups.filter(pk=self.mine.pk).exists())

    def test_public_interest_without_request_tells_pis(self):
        self.spec.pis.add(self.pi)
        svc.register_interest(self.transient, self.requester, "Light curve paper", comment=False,
                              notify_others=False, notify_pis=True)
        self.assertFalse(DataAccessRequest.objects.exists())
        note = Notification.objects.get(recipient=self.pi_user)
        self.assertEqual(note.kind, "interest")
        self.assertIn("Light curve paper", note.text)
        self.assertFalse(Notification.objects.filter(recipient=self.pi_user2).exists())

    def test_private_interest_notifies_no_pi(self):
        self.spec.pis.add(self.pi)
        svc.register_interest(self.transient, self.requester, "Quiet paper", comment=False, notify_others=False)
        svc.register_interest(self.transient, self.requester, "Quiet paper 2", comment=False, notify_others=False,
                              request_data=True)  # request_data alone does nothing without notify_pis
        self.assertFalse(Notification.objects.filter(recipient=self.pi_user).exists())
        self.assertFalse(DataAccessRequest.objects.exists())

    def test_interest_form_and_api_flags(self):
        self.spec.pis.add(self.pi)
        c = Client()
        c.force_login(self.requester)
        response = c.post(reverse("interest_register", kwargs={"transient_id": self.transient.pk}),
                          {"title": "Form paper", "notify_pis": "1", "request_data": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DataAccessRequest.objects.filter(requester=self.requester).count(), 2)
        self.assertTrue(Notification.objects.filter(recipient=self.pi_user, kind="data_access").exists())
        Notification.objects.all().delete()
        response = c.post("/api/transientinterests/", data=json.dumps({
            "transient": self.transient.pk, "title": "API paper", "notify_pis": True,
        }), content_type="application/json")
        self.assertIn(response.status_code, (200, 201), response.content)
        self.assertTrue(Notification.objects.filter(recipient=self.pi_user, kind="interest").exists())


class DecidersTests(PiBase):
    def test_deciders_for_includes_pis_then_members(self):
        self.spec.pis.add(self.pi, self.pi_unlinked)
        req = dar.request_access(self.requester, self.transient, "spectrum", self.owners, notify_owners=False)
        self.assertEqual(dar.named_deciders_for(req), [self.pi_user])
        self.assertEqual(dar.deciders_for(req), [self.pi_user, self.member])
        # the group form keeps the pre-#446 behaviour (members only)
        self.assertEqual(dar.deciders_for(self.owners), [self.member])
        # the #447 extension point is empty for now
        self.assertEqual(dar.designated_sharers_for(req, [self.spec]), [])

    def test_pi_can_decide_and_others_cannot(self):
        self.spec.pis.add(self.pi)
        req = dar.request_access(self.requester, self.transient, "spectrum", self.owners, notify_owners=False)
        self.assertTrue(dar.user_can_decide(self.pi_user, req))
        self.assertTrue(dar.user_can_decide(self.member, req), "group members still decide")
        self.assertFalse(dar.user_can_decide(self.pi_user2, req), "a PI of other data cannot")
        self.assertFalse(dar.user_can_decide(self.outsider, req))
        # a PI of the photometry is not a decider for the spectra
        self.phot.pis.add(self.pi2)
        self.assertFalse(dar.user_can_decide(self.pi_user2, req))
        self.assertEqual(list(dar.requests_to_decide(self.pi_user2)), [])

    def test_pi_decides_from_the_inbox_page(self):
        self.spec.pis.add(self.pi)
        req = dar.request_access(self.requester, self.transient, "spectrum", self.owners, notify_owners=False)
        c = Client()
        c.force_login(self.pi_user)
        response = c.get(reverse("data_access_requests"))
        self.assertContains(response, self.transient.name)
        response = c.post(reverse("data_access_decide", kwargs={"request_id": req.pk}), {"decision": "accept"})
        self.assertEqual(response.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.status, DataAccessRequest.STATUS_ACCEPTED)
        self.assertEqual(req.decided_by, self.pi_user)


class PiDisplayTests(PiBase):
    def setUp(self):
        super().setUp()
        self.spec.pis.add(self.pi)
        self.phot.pis.add(self.pi2)
        self.spectra_fragment = reverse("transient_detail_spectra_tab_fragment",
                                        kwargs={"transient_id": self.transient.pk})
        self.phot_fragment = reverse("transient_detail_photometry_fragment",
                                     kwargs={"transient_id": self.transient.pk})

    def _get(self, user, url):
        c = Client()
        c.force_login(user)
        response = c.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_hidden_summary_carries_pi_names(self):
        rows = dar.hidden_summary(self.transient, self.requester)
        self.assertEqual({r["kind"]: r["pis"] for r in rows},
                         {"spectrum": ["Synthetic PI One"], "photometry": ["Synthetic PI Two"]})
        self.assertEqual(dar.hidden_summary(self.transient, self.member), [])

    def test_member_sees_pi_names_on_the_tables(self):
        html = self._get(self.member, self.spectra_fragment)
        self.assertIn("Synthetic PI One", html)
        self.assertNotIn("yse-data-access-hint", html)
        html = self._get(self.member, self.phot_fragment)
        self.assertIn("Synthetic PI Two", html)

    def test_requester_sees_pi_names_only_in_the_hint(self):
        html = self._get(self.requester, self.spectra_fragment)
        self.assertIn("yse-data-access-hint", html)
        self.assertIn("PI: Synthetic PI One", html)
        self.assertIn("No authorized spectra for this transient.", html)
        html = self._get(self.requester, self.phot_fragment)
        self.assertIn("PI: Synthetic PI Two", html)

    def test_no_pi_account_details_are_shown(self):
        for user in (self.member, self.requester):
            for url in (self.spectra_fragment, self.phot_fragment):
                html = self._get(user, url)
                self.assertNotIn(PI_EMAIL, html)
                self.assertNotIn(self.pi_user.username, html)
                self.assertNotIn(self.pi_user.email, html)

    def test_public_data_pis_shown_without_hint(self):
        public = attach_synthetic_spectrum(self.admin, self.transient, obs_group=self.obs_group,
                                           instrument=self.instrument)
        public.pis.add(self.pi_unlinked)
        html = self._get(self.outsider, self.spectra_fragment)
        self.assertIn('<td class="yse-data-pis">Synthetic PI Unlinked</td>', html)
        # the restricted spectrum is not on the table; its PI is named only in the "restricted" hint
        self.assertNotIn('<td class="yse-data-pis">Synthetic PI One</td>', html)
        self.assertIn("PI: Synthetic PI One", html)


class BackfillTests(PiBase):
    def test_obs_group_and_allocation_backfill_dry_run_then_apply(self):
        self.pi.obs_group.add(self.obs_group)
        out = StringIO()
        call_command("backfill_data_pis", stdout=out)
        self.assertIn("dry run", out.getvalue())
        self.assertFalse(self.spec.pis.exists())
        plan = data_pis.backfill_plan()
        self.assertEqual(plan["counts"]["assigned_obs_group"], 2)  # the restricted spectrum and photometry set
        # an allocation on the spectrum's instrument covering its date wins for spectra
        now = timezone.now()
        Allocation.objects.create(
            name="Synthetic allocation", telescope=self.instrument.telescope, instrument=self.instrument,
            principal_investigator=self.pi2, start_date=self.spec.obs_date - datetime.timedelta(days=1),
            end_date=max(self.spec.obs_date, now) + datetime.timedelta(days=1), **audit_fields(self.admin),
        )
        out = StringIO()
        call_command("backfill_data_pis", "--apply", stdout=out)
        self.assertIn("written: 2", out.getvalue())
        self.assertEqual(list(self.spec.pis.all()), [self.pi2])
        self.assertEqual(list(self.phot.pis.all()), [self.pi])
        # a second run has nothing left to do and never removes PIs
        self.assertEqual(data_pis.backfill_plan()["assign"], [])

    def test_ambiguous_obs_group_is_skipped(self):
        self.pi.obs_group.add(self.obs_group)
        self.pi2.obs_group.add(self.obs_group)
        plan = data_pis.backfill_plan()
        self.assertEqual(plan["assign"], [])
        self.assertEqual(plan["counts"]["ambiguous"], 2)
        self.assertEqual(len(data_pis.backfill_plan(max_pis=2)["assign"]), 2)


class MigrationSafetyTests(TestCase):
    """0035 must be safe on the shared YSE_test DB: nullable columns or new M2M tables only."""

    def test_operations_are_nullable_or_m2m(self):
        migration = importlib.import_module("YSE_App.migrations.0035_data_pis").Migration
        self.assertIn(("YSE_App", "0034_seed_group_profiles"), migration.dependencies)
        self.assertTrue(migration.operations)
        for op in migration.operations:
            self.assertEqual(type(op).__name__, "AddField", op)
            field = op.field
            self.assertTrue(isinstance(field, models.ManyToManyField) or field.null, op.name)

    def test_database_column_is_nullable(self):
        table = PrincipalInvestigator._meta.db_table
        with connection.cursor() as cursor:
            columns = {c.name: c for c in connection.introspection.get_table_description(cursor, table)}
        self.assertIn("user_id", columns)
        self.assertTrue(columns["user_id"].null_ok)
        for model in (TransientPhotometry, TransientSpectrum):
            through = model._meta.get_field("pis").remote_field.through._meta.db_table
            self.assertIn(through, connection.introspection.table_names())

    def test_insert_without_new_fields_still_works(self):
        author = create_test_user("pi_mig_author")
        pi = PrincipalInvestigator.objects.create(name="No account", email=PI_EMAIL, phone="0",
                                                  institution="X", **audit_fields(author))
        self.assertIsNone(pi.user_id)
