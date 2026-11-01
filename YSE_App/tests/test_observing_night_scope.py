"""Observing-night follow-ups scoped to the night, visibility on night/ToO
pages and target lists (#64), and yse_home upcoming nights from
ClassicalObservingDate (#65)."""

import datetime
from unittest import mock

from django.contrib.auth.models import Group
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.models import (
    ClassicalObservingDate,
    ClassicalResource,
    FollowupStatus,
    ToOResource,
    TransientFollowup,
)
from YSE_App.services.classical_nights import night_followups, upcoming_classical_nights
from YSE_App.tests.deploy_checklist_helpers import (
    create_instrument,
    create_principal_investigator,
    create_telescope,
    ensure_classical_night_type,
    ensure_observation_group,
    iers_offline,
    utc_days_from_now,
)
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    audit_fields,
    create_minimal_transient,
    create_test_user,
)

UTC = datetime.timezone.utc


def _no_finder():
    finder = mock.MagicMock()
    finder.return_value.finderchart_noview.return_value = ([], "finder.png")
    return mock.patch("YSE_App.view_utils.finder", finder)


class _NightFixture(TestCase):
    """A semester resource with two nights, a one-night follow-up on the
    first, and a group-restricted follow-up on the same night."""

    NIGHT_1 = datetime.datetime(2026, 3, 10, tzinfo=UTC)
    NIGHT_2 = datetime.datetime(2026, 3, 20, tzinfo=UTC)

    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("night_scope_staff", is_staff=True)
        cls.member = create_test_user("night_scope_member", is_staff=False)
        cls.outsider = create_test_user("night_scope_outsider", is_staff=False)
        cls.group = Group.objects.create(name="night-scope-group")
        public, _ = Group.objects.get_or_create(name=PUBLIC_COLLABORATION_GROUP_NAME)
        cls.member.groups.add(cls.group, public)
        cls.outsider.groups.add(public)

        audit = audit_fields(cls.staff)
        cls.telescope = create_telescope(cls.staff, "Night Scope Tel")
        night_type = ensure_classical_night_type(cls.staff)
        cls.resource = ClassicalResource.objects.create(
            telescope=cls.telescope,
            begin_date_valid=datetime.datetime(2026, 2, 1, tzinfo=UTC),
            end_date_valid=datetime.datetime(2026, 7, 31, tzinfo=UTC),
            **audit,
        )
        cls.night_1 = ClassicalObservingDate.objects.create(
            resource=cls.resource, night_type=night_type, obs_date=cls.NIGHT_1, **audit
        )
        cls.night_2 = ClassicalObservingDate.objects.create(
            resource=cls.resource, night_type=night_type, obs_date=cls.NIGHT_2, **audit
        )
        cls.status, _ = FollowupStatus.objects.get_or_create(name="Requested", defaults=audit)

        cls.open_sn = create_minimal_transient(cls.staff, name="nightscopeopen")
        cls.private_sn = create_minimal_transient(cls.staff, name="nightscopeprivate")
        for sn in (cls.open_sn, cls.private_sn):
            attach_synthetic_photometry(cls.staff, sn, n_points=2)

        window = dict(valid_start=cls.NIGHT_1, valid_stop=cls.NIGHT_1 + datetime.timedelta(days=1))
        cls.open_followup = TransientFollowup.objects.create(
            transient=cls.open_sn, status=cls.status, classical_resource=cls.resource,
            is_public=True, requested_by=cls.staff, **window, **audit,
        )
        cls.private_followup = TransientFollowup.objects.create(
            transient=cls.private_sn, status=cls.status, classical_resource=cls.resource,
            is_public=False, requested_by=cls.staff, **window, **audit,
        )
        cls.private_followup.groups.add(cls.group)

    def _client(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _get(self, user, name, night, **extra):
        kwargs = {
            "telescope": self.telescope.name.replace(" ", "_"),
            "obs_date": night.obs_date.strftime("%Y-%m-%d"),
            **extra,
        }
        with iers_offline(), _no_finder():
            response = self._client(user).get(reverse(name, kwargs=kwargs))
        self.assertEqual(response.status_code, 200)
        return response


class ObservingNightScopeTests(_NightFixture):
    def test_helper_selects_by_the_nights_obs_date(self):
        self.assertEqual(
            set(night_followups(self.night_1, self.staff)),
            {self.open_followup, self.private_followup},
        )
        self.assertEqual(list(night_followups(self.night_2, self.staff)), [])

    def test_one_night_followup_shows_on_its_night_only(self):
        first = self._get(self.staff, "observing_night", self.night_1, pi_name="None")
        self.assertContains(first, self.open_sn.name)
        second = self._get(self.staff, "observing_night", self.night_2, pi_name="None")
        self.assertNotContains(second, self.open_sn.name)

    def test_semester_long_followup_shows_on_every_night(self):
        TransientFollowup.objects.filter(pk=self.open_followup.pk).update(
            valid_start=self.resource.begin_date_valid, valid_stop=self.resource.end_date_valid
        )
        for night in (self.night_1, self.night_2):
            response = self._get(self.staff, "observing_night", night, pi_name="None")
            self.assertContains(response, self.open_sn.name)

    def test_download_target_list_matches_the_night(self):
        first = self._get(self.staff, "download_target_list", self.night_1).content.decode()
        self.assertIn(self.open_sn.name, first)
        second = self._get(self.staff, "download_target_list", self.night_2).content.decode()
        self.assertNotIn(self.open_sn.name, second)


class ObservingNightVisibilityTests(_NightFixture):
    def test_staff_sees_restricted_followup(self):
        response = self._get(self.staff, "observing_night", self.night_1, pi_name="None")
        self.assertContains(response, self.open_sn.name)
        self.assertContains(response, self.private_sn.name)

    def test_group_member_sees_restricted_followup(self):
        response = self._get(self.member, "observing_night", self.night_1, pi_name="None")
        self.assertContains(response, self.open_sn.name)
        self.assertContains(response, self.private_sn.name)

    def test_non_member_does_not_see_restricted_followup(self):
        response = self._get(self.outsider, "observing_night", self.night_1, pi_name="None")
        self.assertContains(response, self.open_sn.name)
        self.assertNotContains(response, self.private_sn.name)

    def test_target_lists_hide_restricted_followup_from_non_member(self):
        for name in ("download_target_list", "download_targets_and_finders"):
            with self.subTest(view=name):
                outsider = self._get(self.outsider, name, self.night_1).content.decode()
                self.assertIn(self.open_sn.name, outsider)
                self.assertNotIn(self.private_sn.name, outsider)
                staff = self._get(self.staff, name, self.night_1).content.decode()
                self.assertIn(self.private_sn.name, staff)

    def test_too_requests_hides_restricted_followup_from_non_member(self):
        audit = audit_fields(self.staff)
        too = ToOResource.objects.create(
            telescope=self.telescope,
            begin_date_valid=timezone.now() - datetime.timedelta(days=5),
            end_date_valid=timezone.now() + datetime.timedelta(days=5),
            **audit,
        )
        TransientFollowup.objects.filter(
            pk__in=[self.open_followup.pk, self.private_followup.pk]
        ).update(
            classical_resource=None, too_resource=too,
            valid_start=too.begin_date_valid, valid_stop=too.end_date_valid,
        )
        url = reverse(
            "too_requests",
            kwargs={"telescope": self.telescope.name.replace(" ", "_"), "pi_name": "None"},
        )
        with iers_offline():
            outsider = self._client(self.outsider).get(url)
            staff = self._client(self.staff).get(url)
        self.assertEqual(outsider.status_code, 200)
        self.assertContains(outsider, self.open_sn.name)
        self.assertNotContains(outsider, self.private_sn.name)
        self.assertContains(staff, self.private_sn.name)


class UpcomingClassicalNightsTests(TestCase):
    """yse_home "Next Telescope Nights": one row per ClassicalObservingDate (#65)."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("upcoming_nights_user", is_staff=False)
        cls.outsider = create_test_user("upcoming_nights_outsider", is_staff=False)
        cls.group = Group.objects.create(name="upcoming-nights-group")
        cls.user.groups.add(cls.group)
        audit = audit_fields(cls.user)

        # yse_home needs the Pan-STARRS1 survey stack and the transient statuses.
        ensure_observation_group(cls.user, "YSE")
        ps1 = create_telescope(cls.user, "Pan-STARRS1")
        create_instrument(cls.user, ps1, "GPC1", band_names=("g", "r"))
        create_minimal_transient(cls.user, name="upcomingnightsn")

        cls.pi = create_principal_investigator(cls.user, name="upcoming-pi")
        night_type = ensure_classical_night_type(cls.user)
        cls.semester_tel = create_telescope(cls.user, "Semester Tel")
        cls.late_tel = create_telescope(cls.user, "Late Tel")

        cls.semester = ClassicalResource.objects.create(
            telescope=cls.semester_tel, principal_investigator=cls.pi,
            begin_date_valid=utc_days_from_now(-60, hour=0),
            end_date_valid=utc_days_from_now(120, hour=0), **audit,
        )
        cls.semester.groups.add(cls.group)

        def night(resource, days):
            return ClassicalObservingDate.objects.create(
                resource=resource, night_type=night_type,
                obs_date=utc_days_from_now(days, hour=0), **audit,
            )

        cls.past = night(cls.semester, -3)
        cls.soon = night(cls.semester, 2)
        cls.later = night(cls.semester, 10)
        cls.far = night(cls.semester, 45)

        # Valid now, but its only night is beyond the window.
        cls.late = ClassicalResource.objects.create(
            telescope=cls.late_tel,
            begin_date_valid=utc_days_from_now(-10, hour=0),
            end_date_valid=utc_days_from_now(90, hour=0), **audit,
        )
        night(cls.late, 60)

    def _home(self, user):
        client = Client()
        client.force_login(user)
        with iers_offline():
            response = client.get(reverse("yse_home"))
        self.assertEqual(response.status_code, 200)
        return response

    def test_service_lists_only_nights_in_the_window(self):
        from YSE_App.data import ObservingResourceService

        nights = upcoming_classical_nights(
            ObservingResourceService.GetAuthorizedClassicalObservingDate_ByUser(self.user), 30
        )
        self.assertEqual(nights, [self.soon, self.later])

    def test_yse_home_lists_each_night_with_a_link(self):
        response = self._home(self.user)
        self.assertEqual(response.context["upcoming_observing_nights"], [self.soon, self.later])
        self.assertEqual(response.context["classical_days_ahead"], 30)
        body = response.content.decode()
        start = body.index('id="classical_resources"')
        table = body[start:body.index("</table>", start)]
        self.assertNotIn("Late Tel", table)
        self.assertIn(self.soon.obs_date.strftime("%Y-%m-%d"), table)
        self.assertIn(self.later.obs_date.strftime("%Y-%m-%d"), table)
        self.assertNotIn(self.far.obs_date.strftime("%Y-%m-%d"), table)
        self.assertIn(
            reverse("observing_night", kwargs={
                "telescope": "Semester_Tel",
                "obs_date": self.soon.obs_date.strftime("%Y-%m-%d"),
                "pi_name": "upcoming-pi",
            }),
            table,
        )

    def test_restricted_resource_hidden_from_non_member(self):
        response = self._home(self.outsider)
        self.assertEqual(response.context["upcoming_observing_nights"], [])

    @override_settings(YSE_HOME_CLASSICAL_DAYS_AHEAD=5)
    def test_days_ahead_setting_and_empty_state(self):
        response = self._home(self.user)
        self.assertEqual(response.context["upcoming_observing_nights"], [self.soon])
        self.assertEqual(response.context["classical_days_ahead"], 5)
        self.assertContains(
            self._home(self.outsider), "No telescope nights in the next 5 days"
        )
