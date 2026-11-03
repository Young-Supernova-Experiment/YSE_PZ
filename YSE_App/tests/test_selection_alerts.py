"""Alerts on Selections (#844): triggers, the owner-only team rule, recipient visibility, dedup,
digests, the volume estimate, the off switch, the pages and the ON DELETE rules of 0072.

Synthetic data only: made-up users, teams, transients, spectra and photometry. Nothing is sent:
deliveries are Notification rows and queued jobs, and the Slack client is mocked.
"""

import datetime
import importlib
from unittest import mock

from django.apps import apps
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.db import connection, models
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.common import fk_on_delete
from YSE_App.models import (
    Notification,
    Selection,
    SelectionAlert,
    SelectionAlertSnapshot,
    Transient,
    TransientClass,
    TransientPhotData,
    TransientSpectrum,
)
from YSE_App.models.group_models import GroupProfile
from YSE_App.models.job_models import Job
from YSE_App.services import selection_alerts as svc
from YSE_App.services import selections as selection_svc
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    attach_synthetic_spectrum,
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

alert_migration = importlib.import_module("YSE_App.migrations.0072_selection_alerts_fk_on_delete")
PREVIEW_COOKIE = "yse-ui-preview"
T0 = timezone.now().replace(microsecond=0)


def preview_client(user):
    client = Client()
    client.force_login(user)
    client.cookies[PREVIEW_COOKIE] = "1"
    return client


def alerts_for(user):
    return Notification.objects.filter(recipient=user, kind=svc.NOTIFICATION_KIND).order_by("pk")


class AlertFixture(TestCase):
    """A team with an owner, a deputy and a member, an outsider, and listed (TNS-style) transients."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = create_test_user("alr_owner", is_staff=False)
        cls.deputy = create_test_user("alr_deputy", is_staff=False)
        cls.member = create_test_user("alr_member", is_staff=False)
        cls.outsider = create_test_user("alr_outsider", is_staff=False)
        cls.team = Group.objects.create(name="alr-team")
        cls.secret = Group.objects.create(name="alr-secret")
        for user in (cls.owner, cls.deputy, cls.member):
            user.groups.add(cls.team)
        cls.owner.groups.add(cls.secret)
        profile, _ = GroupProfile.objects.get_or_create(group=cls.team)  # a signal may have made it
        profile.owners.add(cls.owner)
        profile.deputies.add(cls.deputy)
        for name in ("2099alra", "2099alrb"):  # TNS-style names: listed for everyone
            create_minimal_transient(cls.owner, name=name)
        cls.selection = selection_svc.create_selection(
            cls.owner, "Alert sel", visibility=Selection.VISIBILITY_TEAM, team=cls.team,
            search_params="name_contains=2099alr")

    def setUp(self):
        super().setUp()
        # Nothing leaves the test: every notification channel is a mock (jobs are not run anyway).
        from YSE_App.services import notify

        senders = mock.patch.dict(notify.SENDERS, {channel: mock.Mock() for channel in notify.SENDERS})
        senders.start()
        self.addCleanup(senders.stop)

    def alert(self, user=None, selection=None, **values):
        values.setdefault("frequency", SelectionAlert.FREQUENCY_IMMEDIATE)
        return svc.create_alert(user or self.owner, selection or self.selection, **values)

    def run_at(self, minutes=0):
        return svc.run_alerts(now=T0 + datetime.timedelta(minutes=minutes), force=True)

    def transient(self, name):
        return create_minimal_transient(self.owner, name=name)

    def hidden_photometry(self, transient):
        photometry = attach_synthetic_photometry(self.owner, transient, n_points=2)
        photometry.groups.add(self.secret)
        return photometry


class TriggerTests(AlertFixture):
    def test_new_match(self):
        self.alert(on_new_match=True)
        result = self.run_at(0)
        self.assertEqual(result["baselines"], 1)
        self.assertFalse(alerts_for(self.owner).exists())  # the baseline announces nothing
        self.transient("2099alrc")
        self.run_at(15)
        notes = list(alerts_for(self.owner))
        self.assertEqual(len(notes), 1)
        self.assertIn("2099alrc", notes[0].text)
        self.assertIn("new match", notes[0].text)
        self.assertNotIn("2099alra", notes[0].text)

    def test_re_running_sends_nothing_new(self):
        self.alert(on_new_match=True)
        self.run_at(0)
        self.transient("2099alrc")
        self.run_at(15)
        for minutes in (16, 17, 30):
            self.run_at(minutes)
        self.assertEqual(alerts_for(self.owner).count(), 1)

    def test_new_spectrum(self):
        self.alert(on_new_match=False, on_new_spectrum=True)
        self.run_at(0)
        target = Transient.objects.get(name="2099alra")
        attach_synthetic_spectrum(self.owner, target)
        self.run_at(15)
        notes = list(alerts_for(self.owner))
        self.assertEqual(len(notes), 1)
        self.assertIn("2099alra", notes[0].text)
        self.assertIn("1 new spectrum", notes[0].text)

    def test_class_change(self):
        target = Transient.objects.get(name="2099alra")
        attach_synthetic_spectrum(self.owner, target)  # its data is visible, so is its class
        ia = TransientClass.objects.create(name="SN Ia-synthetic", **audit_fields(self.owner))
        self.alert(on_new_match=False, on_class_change=True)
        self.run_at(0)
        target.best_spec_class = ia
        target.save()
        self.run_at(15)
        notes = list(alerts_for(self.owner))
        self.assertEqual(len(notes), 1)
        self.assertIn("class SN Ia-synthetic (was unclassified)", notes[0].text)

    def _photometry(self, name):
        target = Transient.objects.get(name=name)
        photometry = attach_synthetic_photometry(self.owner, target, n_points=3)  # 18.0 to 18.1 mag
        return target, photometry

    def _point(self, photometry, mag, minutes):
        band = TransientPhotData.objects.filter(photometry=photometry).first().band
        return TransientPhotData.objects.create(
            photometry=photometry, band=band, obs_date=T0 + datetime.timedelta(minutes=minutes), mag=mag,
            mag_err=0.05, **audit_fields(self.owner))

    def test_crosses_a_magnitude(self):
        _target, photometry = self._photometry("2099alra")
        self.alert(on_new_match=False, mag_threshold=17.5)
        self.run_at(0)
        self._point(photometry, 17.8, 5)  # brighter, but not past 17.5
        self.run_at(15)
        self.assertFalse(alerts_for(self.owner).exists())
        self._point(photometry, 17.2, 20)
        self.run_at(30)
        notes = list(alerts_for(self.owner))
        self.assertEqual(len(notes), 1)
        self.assertIn("17.20 mag, brighter than 17.5", notes[0].text)
        self._point(photometry, 17.0, 35)  # still brighter: no second crossing
        self.run_at(45)
        self.assertEqual(alerts_for(self.owner).count(), 1)

    def test_fades_below_a_magnitude(self):
        _target, photometry = self._photometry("2099alrb")
        self.alert(on_new_match=False, mag_threshold=19.0, mag_direction=SelectionAlert.FAINTER)
        self.run_at(0)
        self._point(photometry, 19.4, 5)
        self.run_at(15)
        self.assertIn("fainter than 19", alerts_for(self.owner).get().text)

    def test_needs_a_trigger(self):
        with self.assertRaises(svc.AlertError):
            self.alert(on_new_match=False)
        with self.assertRaises(svc.AlertError):
            self.alert(on_new_match=False, mag_threshold="bright")


class TeamOwnerRuleTests(AlertFixture):
    def test_only_owners_may_alert_a_team_or_its_channel(self):
        for user in (self.member, self.deputy, self.outsider):
            for target in (SelectionAlert.TARGET_TEAM, SelectionAlert.TARGET_SLACK):
                with self.subTest(user=user.username, target=target), self.assertRaises(svc.AlertAccessError):
                    self.alert(user=user, target=target, team=self.team, slack_channel="C0123ABCD")
        self.assertFalse(SelectionAlert.objects.exists())
        alert = self.alert(target=SelectionAlert.TARGET_TEAM, team=self.team)
        self.assertEqual(alert.team, self.team)
        with self.assertRaises(svc.AlertError):
            self.alert(target=SelectionAlert.TARGET_SLACK, team=self.team, slack_channel="not a channel")

    def test_anyone_may_alert_themselves(self):
        alert = self.alert(user=self.member, target=SelectionAlert.TARGET_ME)
        self.assertEqual((alert.owner, alert.team), (self.member, None))

    def test_a_team_alert_reaches_every_member(self):
        self.alert(target=SelectionAlert.TARGET_TEAM, team=self.team)
        self.run_at(0)
        self.transient("2099alrc")
        self.run_at(15)
        for user in (self.owner, self.deputy, self.member):
            self.assertEqual(alerts_for(user).count(), 1, user.username)
        self.assertFalse(alerts_for(self.outsider).exists())

    def test_the_rule_is_checked_again_on_every_run(self):
        alert = self.alert(target=SelectionAlert.TARGET_TEAM, team=self.team)
        self.run_at(0)
        self.team.group_profile.owners.remove(self.owner)
        self.transient("2099alrc")
        self.run_at(15)
        self.assertFalse(Notification.objects.filter(kind=svc.NOTIFICATION_KIND).exists())
        alert.refresh_from_db()
        self.assertIn("owners", alert.last_error)

    def test_editing_is_checked_too(self):
        alert = self.alert(user=self.member)
        with self.assertRaises(svc.AlertAccessError):
            svc.update_alert(self.member, alert, target=SelectionAlert.TARGET_TEAM, team=self.team)
        with self.assertRaises(svc.AlertAccessError):
            svc.update_alert(self.owner, alert, label="not mine")
        with self.assertRaises(svc.AlertAccessError):
            svc.delete_alert(self.owner, alert)

    def test_the_page_refuses_a_team_alert_from_a_member(self):
        client = preview_client(self.member)
        url = reverse("selection_alert_new", kwargs={"slug": self.selection.slug})
        response = client.post(url, {"on_new_match": "1", "frequency": "daily", "target": "team",
                                     "team": str(self.team.pk), "confirm": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Only the team&#x27;s owners")
        self.assertFalse(SelectionAlert.objects.exists())


@mock.patch("YSE_App.integrations.slack.client.chat_post_message", return_value={"ok": True})
class VisibilityTests(AlertFixture):
    def test_objects_are_evaluated_as_each_recipient(self, _slack):
        self.alert(target=SelectionAlert.TARGET_TEAM, team=self.team)
        self.run_at(0)
        hidden = self.transient("2099alrhidden")  # not a TNS-style name: listed only with visible data
        self.hidden_photometry(hidden)
        self.run_at(15)
        self.assertIn("2099alrhidden", alerts_for(self.owner).get().text)
        for user in (self.member, self.deputy):
            self.assertFalse(alerts_for(user).exists(), user.username)

    def test_spectra_are_checked_per_point(self, _slack):
        self.alert(on_new_match=False, on_new_spectrum=True, target=SelectionAlert.TARGET_TEAM, team=self.team)
        self.run_at(0)
        spectrum = attach_synthetic_spectrum(self.owner, Transient.objects.get(name="2099alra"))
        spectrum.groups.add(self.secret)
        self.run_at(15)
        self.assertIn("new spectrum", alerts_for(self.owner).get().text)
        self.assertFalse(alerts_for(self.member).exists())

    def test_a_private_selection_tells_nobody_else(self, _slack):
        private = selection_svc.create_selection(self.owner, "Mine only", search_params="name_contains=2099alr")
        self.alert(selection=private, target=SelectionAlert.TARGET_TEAM, team=self.team)
        self.run_at(0)
        self.transient("2099alrc")
        self.run_at(15)
        self.assertEqual(alerts_for(self.owner).count(), 1)
        self.assertFalse(alerts_for(self.member).exists())

    def test_a_slack_channel_gets_only_what_every_member_sees(self, _slack):
        self.alert(target=SelectionAlert.TARGET_SLACK, team=self.team, slack_channel="C0123ABCD")
        self.run_at(0)
        hidden = self.transient("2099alrhidden")
        self.hidden_photometry(hidden)
        self.run_at(15)
        self.assertFalse(Job.objects.filter(kind=svc.SLACK_KIND).exists())
        self.transient("2099alrc")
        self.run_at(30)
        job = Job.objects.get(kind=svc.SLACK_KIND)
        self.assertEqual(job.payload["channel"], "C0123ABCD")
        self.assertIn("2099alrc", job.payload["text"])
        self.assertNotIn("2099alrhidden", job.payload["text"])
        self.assertFalse(Notification.objects.filter(kind=svc.NOTIFICATION_KIND).exists())

    def test_a_pending_event_is_dropped_when_access_goes(self, _slack):
        self.alert(user=self.member, frequency=SelectionAlert.FREQUENCY_DAILY)
        self.run_at(0)
        target = self.transient("2099alrgone")
        photometry = attach_synthetic_photometry(self.owner, target, n_points=2)  # visible to everyone
        self.run_at(15)  # found, waits for the digest
        photometry.groups.add(self.secret)  # now hidden from the member
        self.run_at(60 * 24 + 5)
        self.assertFalse(alerts_for(self.member).exists())

    def test_the_slack_job_posts_through_the_mocked_client(self, slack):
        with override_settings(SLACK_BOT_TOKEN="xoxb-synthetic", NOTIFICATION_SLACK_ENABLED=True):
            result = svc.post_slack_job({"channel": "C0123ABCD", "text": "hello"})
        self.assertEqual(result, {"channel": "C0123ABCD"})
        slack.assert_called_once_with("C0123ABCD", "hello")


class DedupAndDigestTests(AlertFixture):
    def test_an_object_matching_several_alerts_is_sent_once(self):
        other = selection_svc.create_selection(self.owner, "Second sel", search_params="name_contains=2099alrc")
        self.alert(label="First alert")
        self.alert(selection=other, label="Second alert")
        self.run_at(0)
        self.transient("2099alrc")
        self.run_at(15)
        note = alerts_for(self.owner).get()
        self.assertEqual(note.text.count("2099alrc"), 1)
        self.assertIn("First alert", note.text)
        self.assertIn("Second alert", note.text)

    def test_hourly_digest_batches_events(self):
        self.alert(frequency=SelectionAlert.FREQUENCY_HOURLY)
        self.run_at(0)
        self.transient("2099alrc")
        self.run_at(15)
        self.transient("2099alrd")
        self.run_at(30)
        self.assertFalse(alerts_for(self.owner).exists())
        snapshot = SelectionAlertSnapshot.objects.get(recipient=self.owner)
        self.assertEqual(len(snapshot.pending), 2)
        self.run_at(61)
        note = alerts_for(self.owner).get()
        self.assertIn("2099alrc", note.text)
        self.assertIn("2099alrd", note.text)
        self.run_at(75)
        self.assertEqual(alerts_for(self.owner).count(), 1)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.pending, [])

    def test_daily_digest_waits_a_day(self):
        self.alert(frequency=SelectionAlert.FREQUENCY_DAILY)
        self.run_at(0)
        self.transient("2099alrc")
        self.run_at(60 * 6)
        self.assertFalse(alerts_for(self.owner).exists())
        self.run_at(60 * 24)
        self.assertEqual(alerts_for(self.owner).count(), 1)

    def test_default_frequency_is_daily(self):
        alert = svc.create_alert(self.owner, self.selection)
        self.assertEqual(alert.frequency, SelectionAlert.FREQUENCY_DAILY)


class VolumeEstimateTests(AlertFixture):
    def test_counts_the_last_30_days(self):
        now = timezone.now()
        old = Transient.objects.get(name="2099alrb")
        Transient.objects.filter(pk=old.pk).update(created_date=now - datetime.timedelta(days=40))
        Transient.objects.filter(name="2099alra").update(created_date=now - datetime.timedelta(days=3))
        fresh = Transient.objects.get(name="2099alra")
        attach_synthetic_spectrum(self.owner, fresh)
        stale = attach_synthetic_spectrum(self.owner, old)
        TransientSpectrum.objects.filter(pk=stale.pk).update(created_date=now - datetime.timedelta(days=45))
        hidden = attach_synthetic_spectrum(self.owner, fresh)
        hidden.groups.add(self.secret)

        estimate = svc.estimate_volume(self.selection, self.member, on_new_match=True, on_new_spectrum=True,
                                       frequency=SelectionAlert.FREQUENCY_DAILY, now=now)
        self.assertEqual(estimate["matches"], 2)
        self.assertEqual(estimate["events"]["new_match"], 1)
        self.assertEqual(estimate["events"]["new_spectrum"], 1)  # the hidden one is not counted
        self.assertEqual(estimate["total"], 2)
        self.assertEqual(estimate["messages"], 2)  # 3 days ago and today
        owner_estimate = svc.estimate_volume(self.selection, self.owner, on_new_match=False, on_new_spectrum=True,
                                             now=now)
        self.assertEqual(owner_estimate["events"]["new_spectrum"], 2)

    def test_counts_magnitude_crossings(self):
        now = timezone.now()
        target = Transient.objects.get(name="2099alra")
        _group, _instrument, band = create_instrument_stack(self.owner, obs_group_name="alr-est")
        photometry = attach_synthetic_photometry(self.owner, target, n_points=1)
        for days, mag in ((20, 18.4), (10, 17.6), (5, 17.9), (2, 17.4)):
            TransientPhotData.objects.create(photometry=photometry, band=band, mag=mag, mag_err=0.05,
                                             obs_date=now - datetime.timedelta(days=days), **audit_fields(self.owner))
        estimate = svc.estimate_volume(self.selection, self.owner, on_new_match=False, mag_threshold=17.8,
                                       frequency=SelectionAlert.FREQUENCY_IMMEDIATE, now=now)
        self.assertEqual(estimate["events"]["mag_cross"], 2)  # 18.4 -> 17.6 and 17.9 -> 17.4

    def test_the_page_shows_the_estimate_before_saving(self):
        client = preview_client(self.owner)
        url = reverse("selection_alert_new", kwargs={"slug": self.selection.slug})
        data = {"on_new_match": "1", "frequency": "daily", "target": "me"}
        response = client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="selection-alert-estimate"')
        self.assertContains(response, 'id="selection-alert-save"')
        self.assertFalse(SelectionAlert.objects.exists())
        response = client.post(url, dict(data, confirm="1"))
        self.assertRedirects(response, reverse("selection_alert_list"), fetch_redirect_response=False)
        alert = SelectionAlert.objects.get()
        self.assertEqual((alert.owner, alert.target, alert.frequency), (self.owner, "me", "daily"))


class DisabledByDefaultTests(AlertFixture):
    def test_the_setting_defaults_to_off(self):
        self.assertFalse(settings.SELECTION_ALERTS_ENABLED)
        self.assertFalse(svc.alerts_enabled())
        self.assertIn("YSE_App.data_ingest.Selection_Alerts.SelectionAlerts", settings.CRON_CLASSES)

    def test_the_pass_does_nothing_while_off(self):
        self.alert()
        self.transient("2099alrc")
        self.assertEqual(svc.run_alerts(), {"disabled": True})
        self.assertEqual(svc.run_alerts_job({}), {"disabled": True})
        self.assertFalse(SelectionAlertSnapshot.objects.exists())
        self.assertFalse(Notification.objects.filter(kind=svc.NOTIFICATION_KIND).exists())
        self.assertIsNone(SelectionAlert.objects.get().last_run_at)

    def test_the_cron_queues_nothing_while_off(self):
        from YSE_App.data_ingest.Selection_Alerts import SelectionAlerts

        self.assertIn("disabled", SelectionAlerts().do())
        self.assertFalse(Job.objects.filter(kind=svc.RUN_KIND).exists())

    @override_settings(SELECTION_ALERTS_ENABLED=True)
    def test_the_cron_queues_one_pass_when_on(self):
        from YSE_App.data_ingest.Selection_Alerts import SelectionAlerts

        SelectionAlerts().do()
        SelectionAlerts().do()
        self.assertEqual(Job.objects.filter(kind=svc.RUN_KIND).count(), 1)


class PageTests(AlertFixture):
    def test_pages_are_preview_only(self):
        alert = self.alert()
        client = Client()
        client.force_login(self.owner)
        for url in (reverse("selection_alert_new", kwargs={"slug": self.selection.slug}),
                    reverse("selection_alert_list"),
                    reverse("selection_alert_edit", kwargs={"pk": alert.pk})):
            self.assertEqual(client.get(url).status_code, 404, url)
        self.assertEqual(client.post(reverse("selection_alert_delete", kwargs={"pk": alert.pk})).status_code, 404)
        self.assertTrue(SelectionAlert.objects.filter(pk=alert.pk).exists())

    def test_alert_me_list_edit_delete(self):
        client = preview_client(self.owner)
        detail = client.get(reverse("selection_detail", kwargs={"slug": self.selection.slug})).content.decode()
        self.assertIn('id="selection-alert-me"', detail)
        alert = self.alert(label="Young things")
        listing = client.get(reverse("selection_alert_list"))
        self.assertContains(listing, "Young things")
        self.assertContains(listing, 'id="selection-alerts-off"')
        edit_url = reverse("selection_alert_edit", kwargs={"pk": alert.pk})
        self.assertEqual(client.get(edit_url).status_code, 200)
        response = client.post(edit_url, {"on_new_match": "1", "on_new_spectrum": "1", "frequency": "hourly",
                                          "target": "me", "label": "Renamed", "confirm": "1"})
        self.assertEqual(response.status_code, 302)
        alert.refresh_from_db()
        self.assertEqual((alert.label, alert.frequency, alert.on_new_spectrum), ("Renamed", "hourly", True))
        client.post(reverse("selection_alert_delete", kwargs={"pk": alert.pk}))
        self.assertFalse(SelectionAlert.objects.filter(pk=alert.pk).exists())
        self.assertTrue(Selection.objects.filter(pk=self.selection.pk).exists())

    def test_other_peoples_alerts_and_hidden_selections_are_404(self):
        alert = self.alert()
        client = preview_client(self.outsider)
        self.assertEqual(client.get(reverse("selection_alert_edit", kwargs={"pk": alert.pk})).status_code, 404)
        self.assertEqual(client.post(reverse("selection_alert_delete", kwargs={"pk": alert.pk})).status_code, 404)
        self.assertEqual(client.get(reverse("selection_alert_new", kwargs={"slug": self.selection.slug})).status_code,
                         404)
        self.assertTrue(SelectionAlert.objects.filter(pk=alert.pk).exists())


class ForeignKeyListTests(SimpleTestCase):
    def test_every_foreign_key_of_the_new_tables_is_listed_with_its_rule(self):
        rules = {models.CASCADE: fk_on_delete.CASCADE, models.SET_NULL: fk_on_delete.SET_NULL}
        expected = set()
        for name in ("SelectionAlert", "SelectionAlertSnapshot"):
            model = apps.get_model("YSE_App", name)
            expected |= {
                (model._meta.db_table, f.column, f.related_model._meta.db_table, rules[f.remote_field.on_delete])
                for f in model._meta.local_concrete_fields if isinstance(f, models.ForeignKey)
            }
        self.assertEqual(set(alert_migration.FOREIGN_KEYS), expected)


NEW_TABLES = {"YSE_App_selection", "YSE_App_selectionalert", "YSE_App_selectionalertsnapshot"}


def _raw(statement, params):
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def _delete_known_children(parent_table, parent_id):
    """What develop's Django cascade deletes before the parent: every child row except the new tables'."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                       "WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME = %s", [parent_table])
        for table, column in [row for row in cursor.fetchall() if row[0] not in NEW_TABLES]:
            cursor.execute(f"DELETE FROM `{table}` WHERE `{column}` = %s", [parent_id])


class RawDeleteTests(TestCase):
    """develop's deletes, as raw SQL, after migration 0072: MySQL cascades the alert rows."""

    def setUp(self):
        if connection.vendor != "mysql":
            self.skipTest("ON DELETE rules are MySQL constraints")
        self.owner = User.objects.create(username="alr-fk-owner")
        self.recipient = User.objects.create(username="alr-fk-recipient")
        self.team = Group.objects.create(name="alr-fk-team")
        self.selection = Selection.objects.create(name="fk", slug="alr-fk", owner=self.owner, kind="search")

    def _alert(self, team=None):
        alert = SelectionAlert.objects.create(selection=self.selection, owner=self.owner, team=team,
                                              target="team" if team else "me")
        snapshot = SelectionAlertSnapshot.objects.create(alert=alert, recipient=self.recipient, state={}, pending=[])
        return alert, snapshot

    def test_one_constraint_per_column_with_the_model_rule(self):
        for table, column, _, rule in alert_migration.FOREIGN_KEYS:
            with self.subTest(table=table, column=column):
                rows = fk_on_delete.current_constraints(connection, table, column)
                self.assertEqual([row[3] for row in rows], [rule])

    def test_owner_delete_cascades_through_the_selection(self):
        alert, snapshot = self._alert()
        _delete_known_children("auth_user", self.owner.id)
        _raw("DELETE FROM auth_user WHERE id = %s", [self.owner.id])
        self.assertFalse(Selection.objects.filter(pk=self.selection.pk).exists())
        self.assertFalse(SelectionAlert.objects.filter(pk=alert.pk).exists())
        self.assertFalse(SelectionAlertSnapshot.objects.filter(pk=snapshot.pk).exists())

    def test_team_delete_cascades(self):
        alert, snapshot = self._alert(team=self.team)
        _delete_known_children("auth_group", self.team.id)
        _raw("DELETE FROM auth_group WHERE id = %s", [self.team.id])
        self.assertFalse(SelectionAlert.objects.filter(pk=alert.pk).exists())
        self.assertFalse(SelectionAlertSnapshot.objects.filter(pk=snapshot.pk).exists())

    def test_recipient_delete_cascades_the_snapshot_only(self):
        alert, snapshot = self._alert()
        _delete_known_children("auth_user", self.recipient.id)
        _raw("DELETE FROM auth_user WHERE id = %s", [self.recipient.id])
        self.assertFalse(SelectionAlertSnapshot.objects.filter(pk=snapshot.pk).exists())
        self.assertTrue(SelectionAlert.objects.filter(pk=alert.pk).exists())
