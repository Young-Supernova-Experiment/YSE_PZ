"""Classical follow-up night pulldown: chronological, nearest night default,
nights drop out the morning after the run in the observatory's time zone.

Time is frozen with ``unittest.mock`` (freezegun is not a dependency).
"""

import datetime
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from YSE_App.forms import TransientFollowupForm
from YSE_App.models import (
    ClassicalObservingDate,
    ClassicalResource,
    Observatory,
    Telescope,
)
from YSE_App.services.classical_nights import (
    NIGHT_ENDS_LOCAL_TIME,
    night_cutoff,
    observatory_timezone,
    resource_next_night,
    upcoming_classical_resources,
)
from YSE_App.tests.deploy_checklist_helpers import ensure_classical_night_type
from YSE_App.tests.fixtures_minimal import audit_fields, create_test_user

UTC = datetime.timezone.utc


def utc(*args):
    return datetime.datetime(*args, tzinfo=UTC)


class ClassicalNightPulldownTests(TestCase):
    """Two telescopes 11 hours apart: Mauna Kea (HST, UTC-10) and La Palma
    (WEST in October, UTC+1). Both observed the night dated 2026-10-01."""

    # 15:30 UTC on Oct 2 is 05:30 in Hawaii (night of Oct 1 still "tonight")
    # and 16:30 on La Palma (its Oct 1 night ended at 06:00 local = 05:00 UTC).
    FROZEN_NOW = utc(2026, 10, 2, 15, 30)

    def setUp(self):
        self.user = create_test_user("night_pulldown_user", is_staff=False)
        audit = audit_fields(self.user)
        self.night_type = ensure_classical_night_type(self.user)

        mauna_kea = Observatory.objects.create(
            name="Mauna Kea", utc_offset=-10, tz_name="Pacific/Honolulu", **audit
        )
        la_palma = Observatory.objects.create(
            name="Roque de los Muchachos", utc_offset=0, tz_name="Atlantic/Canary", **audit
        )
        # Alphabetically "GTC" < "Keck I": chronological order must win over name order.
        self.keck = Telescope.objects.create(
            name="Keck I", observatory=mauna_kea,
            latitude=19.83, longitude=-155.47, elevation=4145.0, **audit,
        )
        self.gtc = Telescope.objects.create(
            name="GTC", observatory=la_palma,
            latitude=28.76, longitude=-17.89, elevation=2267.0, **audit,
        )

    def _night(self, telescope, year, month, day, *, with_obs_date=True):
        """A one-night resource exactly as AddClassicalResourceFormView builds it."""
        night = utc(year, month, day)
        resource = ClassicalResource.objects.create(
            telescope=telescope,
            begin_date_valid=night,
            end_date_valid=night + datetime.timedelta(days=1),
            **audit_fields(self.user),
        )
        if with_obs_date:
            ClassicalObservingDate.objects.create(
                resource=resource, night_type=self.night_type, obs_date=night,
                **audit_fields(self.user),
            )
        return resource

    # -- time zone resolution --------------------------------------------------

    def test_observatory_timezone_prefers_tz_name(self):
        tz = observatory_timezone(self.keck.observatory)
        self.assertEqual(tz.utcoffset(datetime.datetime(2026, 10, 2)), datetime.timedelta(hours=-10))

    def test_observatory_timezone_falls_back_to_utc_offset_then_utc(self):
        bogus = Observatory(name="Bogus", utc_offset=-7, tz_name="Mountain Standard")
        tz = observatory_timezone(bogus)
        self.assertEqual(tz.utcoffset(datetime.datetime(2026, 10, 2)), datetime.timedelta(hours=-7))
        self.assertEqual(observatory_timezone(None), UTC)

    def test_night_cutoff_is_local_morning_after(self):
        self.assertEqual(NIGHT_ENDS_LOCAL_TIME, datetime.time(6, 0))
        hst = observatory_timezone(self.keck.observatory)
        # Night of Oct 1 (HST) ends 06:00 HST Oct 2 = 16:00 UTC Oct 2.
        self.assertEqual(night_cutoff(datetime.date(2026, 10, 1), hst), utc(2026, 10, 2, 16, 0))
        west = observatory_timezone(self.gtc.observatory)
        # La Palma is UTC+1 in early October: 06:00 local = 05:00 UTC.
        self.assertEqual(night_cutoff(datetime.date(2026, 10, 1), west), utc(2026, 10, 2, 5, 0))

    # -- the cutoff ------------------------------------------------------------

    def test_night_drops_out_the_local_morning_after_the_run(self):
        keck_oct1 = self._night(self.keck, 2026, 10, 1)
        gtc_oct1 = self._night(self.gtc, 2026, 10, 1)
        listed = list(upcoming_classical_resources(ClassicalResource.objects.all(), now=self.FROZEN_NOW))
        self.assertIn(keck_oct1, listed, "05:30 HST: Hawaii's Oct 1 night is still running")
        self.assertNotIn(gtc_oct1, listed, "16:30 WEST: La Palma's Oct 1 night ended this morning")

        # Boundary: gone at exactly 06:00 local, present one minute before.
        self.assertIsNone(resource_next_night(keck_oct1, now=utc(2026, 10, 2, 16, 0)))
        self.assertEqual(
            resource_next_night(keck_oct1, now=utc(2026, 10, 2, 15, 59)), datetime.date(2026, 10, 1)
        )

    def test_old_filter_would_have_kept_the_finished_night(self):
        """The previous rule (end_date_valid > now - 1 day) kept a night for a
        full day after its ``end_date_valid``; La Palma's finished night must
        not survive under the new rule."""
        gtc_oct1 = self._night(self.gtc, 2026, 10, 1)
        old_rule = ClassicalResource.objects.filter(
            end_date_valid__gt=self.FROZEN_NOW - datetime.timedelta(days=1)
        )
        self.assertIn(gtc_oct1, old_rule)
        self.assertNotIn(
            gtc_oct1, upcoming_classical_resources(ClassicalResource.objects.all(), now=self.FROZEN_NOW)
        )

    def test_resource_without_observing_dates_uses_end_date_valid(self):
        resource = self._night(self.gtc, 2026, 10, 1, with_obs_date=False)
        self.assertEqual(resource_next_night(resource, now=utc(2026, 10, 2, 4, 59)), datetime.date(2026, 10, 1))
        self.assertIsNone(resource_next_night(resource, now=utc(2026, 10, 2, 5, 0)))

    def test_semester_resource_with_only_past_nights_stays_until_end_date_valid(self):
        """A long-validity resource (admin-created) whose entered nights are all
        past is still requestable until its window closes; it sorts by that end."""
        now = self.FROZEN_NOW
        semester = ClassicalResource.objects.create(
            telescope=self.keck,
            begin_date_valid=now - datetime.timedelta(days=30),
            end_date_valid=now + datetime.timedelta(days=100),
            **audit_fields(self.user),
        )
        ClassicalObservingDate.objects.create(
            resource=semester, night_type=self.night_type,
            obs_date=now - datetime.timedelta(days=29), **audit_fields(self.user),
        )
        keck_oct3 = self._night(self.keck, 2026, 10, 3)
        listed = list(upcoming_classical_resources(ClassicalResource.objects.all(), now=now))
        self.assertEqual(listed, [keck_oct3, semester])
        self.assertEqual(
            resource_next_night(semester, now=now),
            (semester.end_date_valid - datetime.timedelta(days=1)).date(),
        )

    def test_multi_night_resource_stays_until_its_last_night_is_over(self):
        run = ClassicalResource.objects.create(
            telescope=self.keck,
            begin_date_valid=utc(2026, 9, 30),
            end_date_valid=utc(2026, 10, 4),
            **audit_fields(self.user),
        )
        for day in (9, 30), (10, 1), (10, 3):
            ClassicalObservingDate.objects.create(
                resource=run, night_type=self.night_type, obs_date=utc(2026, *day),
                **audit_fields(self.user),
            )
        # Sep 30 is over (05:30 HST Oct 2); the next remaining night is Oct 1.
        self.assertEqual(resource_next_night(run, now=self.FROZEN_NOW), datetime.date(2026, 10, 1))
        # After Oct 1 ends, Oct 3 is next; after Oct 3's morning, gone.
        self.assertEqual(resource_next_night(run, now=utc(2026, 10, 2, 16, 0)), datetime.date(2026, 10, 3))
        self.assertIsNone(resource_next_night(run, now=utc(2026, 10, 4, 16, 0)))

    # -- ordering and default --------------------------------------------------

    def test_pulldown_is_chronological_and_defaults_to_nearest_night(self):
        # Created out of order and with names that would sort the other way.
        gtc_oct10 = self._night(self.gtc, 2026, 10, 10)
        keck_oct3 = self._night(self.keck, 2026, 10, 3)
        gtc_oct5 = self._night(self.gtc, 2026, 10, 5)
        keck_oct1 = self._night(self.keck, 2026, 10, 1)
        gtc_oct1 = self._night(self.gtc, 2026, 10, 1)  # finished -> not listed

        with mock.patch.object(timezone, "now", return_value=self.FROZEN_NOW):
            form = TransientFollowupForm(user=self.user)

        field = form.fields["classical_resource"]
        self.assertEqual(list(field.queryset), [keck_oct1, keck_oct3, gtc_oct5, gtc_oct10])
        self.assertNotIn(gtc_oct1, field.queryset)
        self.assertEqual(field.initial, keck_oct1)
        self.assertEqual(form.fields["valid_start"].initial, keck_oct1.begin_date_valid)
        self.assertEqual(form.fields["valid_stop"].initial, keck_oct1.end_date_valid)

        # The rendered <select> lists the options in the same order, first one selected.
        html = str(form["classical_resource"])
        positions = [html.index('value="%d"' % r.pk) for r in (keck_oct1, keck_oct3, gtc_oct5, gtc_oct10)]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('value="%d" selected' % keck_oct1.pk, html)
        self.assertNotIn('value="%d"' % gtc_oct1.pk, html)

    def test_same_night_two_telescopes_orders_by_telescope_name(self):
        keck = self._night(self.keck, 2026, 10, 5)
        gtc = self._night(self.gtc, 2026, 10, 5)
        listed = list(upcoming_classical_resources(ClassicalResource.objects.all(), now=self.FROZEN_NOW))
        self.assertEqual(listed, [gtc, keck])

    def test_no_user_form_applies_the_same_rule(self):
        keck_oct1 = self._night(self.keck, 2026, 10, 1)
        gtc_oct1 = self._night(self.gtc, 2026, 10, 1)
        with mock.patch.object(timezone, "now", return_value=self.FROZEN_NOW):
            form = TransientFollowupForm()
        self.assertEqual(list(form.fields["classical_resource"].queryset), [keck_oct1])
        self.assertNotIn(gtc_oct1, form.fields["classical_resource"].queryset)

    def test_bound_form_rejects_a_night_that_is_over(self):
        gtc_oct1 = self._night(self.gtc, 2026, 10, 1)
        with mock.patch.object(timezone, "now", return_value=self.FROZEN_NOW):
            form = TransientFollowupForm(data={"classical_resource": str(gtc_oct1.pk)}, user=self.user)
            self.assertFalse(form.is_valid())
        self.assertIn("classical_resource", form.errors)

    def test_empty_pulldown_when_every_night_is_over(self):
        self._night(self.gtc, 2026, 10, 1)
        qs = upcoming_classical_resources(ClassicalResource.objects.all(), now=self.FROZEN_NOW)
        self.assertEqual(list(qs), [])
        with mock.patch.object(timezone, "now", return_value=self.FROZEN_NOW):
            form = TransientFollowupForm(user=self.user)
        self.assertIsNone(form.fields["classical_resource"].initial)
