"""
Deploy checklist, part 2: the "specific pages to check against prod" list and
table sorting (docs/deploy-checklist-ci.md).

CI has no prod to diff against, so each page is checked for a 200 render with
no template errors and, where it applies, for the rows a checklist user expects.
Sorting is checked by seeding transients in every status bucket and asserting
that ``?<prefix>sort=name`` / ``-name`` changes the row order on the main
dashboard (sync "New" table), on the AJAX status fragments (the "other" tables)
and on the summary view.
"""

from __future__ import annotations

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from YSE_App.models import Transient
from YSE_App.table_utils import TransientTable
from YSE_App.tests.deploy_checklist_helpers import (
    create_instrument,
    create_telescope,
    ensure_observation_group,
    ensure_task_statuses,
    iers_offline,
    order_of,
    transient_slugs_in_order,
)
from YSE_App.tests.fixtures_minimal import (
    create_minimal_transient,
    create_test_user,
    create_transient_with_synthetic_data,
    ensure_transient_statuses,
    seed_dashboard_transients,
)

SORT_STATUSES = ("New", "Watch", "Following", "FollowupRequested", "Interesting")


def _sort_param(prefix: str) -> str:
    """django_tables2 builds the query-string key from the table prefix; don't guess it."""
    return TransientTable(Transient.objects.none(), prefix=prefix).prefixed_order_by_field


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DeployChecklistPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("checklist_page_user", is_staff=True)
        ensure_transient_statuses(cls.user)
        ensure_task_statuses(cls.user)
        cls.transient = create_transient_with_synthetic_data(
            cls.user, name="chk-page-sn", n_phot_points=5
        )
        seed_dashboard_transients(cls.user, count_per_status=1)
        # three names per bucket whose alphabetical and reverse orders differ
        cls.sort_slugs = {}
        for status in SORT_STATUSES:
            slugs = []
            for suffix in ("c", "a", "b"):
                t = create_minimal_transient(
                    cls.user,
                    name=f"chk-sort-{status.lower()}-{suffix}",
                    status_name=status,
                    ra=15.0,
                )
                slugs.append(t.slug)
            cls.sort_slugs[status] = slugs
        # YSE survey pages need the Pan-STARRS1 telescope row.
        ensure_observation_group(cls.user, "YSE")
        ps1 = create_telescope(cls.user, "Pan-STARRS1")
        create_instrument(cls.user, ps1, "GPC1", band_names=("g", "r"))

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _get_ok(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, f"{url} -> {response.status_code}")
        body = response.content.decode("utf-8", errors="replace")
        for marker in ("TemplateSyntaxError", "Invalid filter", "Invalid block tag"):
            self.assertNotIn(marker, body, url)
        return body

    # ------------------------------------------------- pages vs. prod list

    def test_transient_detail_page(self):
        body = self._get_ok(f"/transient_detail/{self.transient.slug}/")
        self.assertIn(self.transient.name, body)

    def test_dashboard_page(self):
        body = self._get_ok(reverse("dashboard"))
        self.assertIn("chk-sort-new-a", body)

    def test_personal_dashboard_page(self):
        self._get_ok(reverse("personaldashboard"))

    def test_sql_explorer_page(self):
        self._get_ok("/explorer/")

    def test_yse_schedule_pages(self):
        with iers_offline():
            self._get_ok(reverse("yse_observing_calendar"))
            self._get_ok(reverse("yse_observing_night", kwargs={"obs_date": "2026-09-22"}))

    def test_observing_schedule_pages(self):
        self._get_ok(reverse("observing_calendar"))
        self._get_ok(reverse("too_calendar"))
        self._get_ok(reverse("yse_oncall_calendar"))
        self._get_ok(reverse("calendar"))

    def test_followup_page(self):
        self._get_ok(reverse("followup"))

    # --------------------------------------------------------------- sorting

    def _assert_sorted(self, url_base: str, param: str, expected_slugs, *, label: str, column="name_string"):
        """``column`` is the django_tables2 column name (``name_string`` orders by ``name``);
        the summary view takes raw ORM field names instead."""
        asc = order_of(transient_slugs_in_order(self._get_ok(f"{url_base}?{param}={column}")), expected_slugs)
        desc = order_of(transient_slugs_in_order(self._get_ok(f"{url_base}?{param}=-{column}")), expected_slugs)
        self.assertEqual(asc, sorted(expected_slugs), f"{label}: ascending sort by name")
        self.assertEqual(desc, sorted(expected_slugs, reverse=True), f"{label}: descending sort by name")

    def test_dashboard_new_table_sorts_by_name(self):
        self._assert_sorted(
            reverse("dashboard"), _sort_param("new"), self.sort_slugs["New"], label="dashboard/New"
        )

    def test_dashboard_other_tables_sort_by_name(self):
        """The non-New tables are AJAX fragments; the sort param must reach them too."""
        for status in ("Watch", "Following", "FollowupRequested", "Interesting"):
            prefix = status.lower()
            self._assert_sorted(
                reverse("dashboard_section", kwargs={"status_key": prefix}),
                _sort_param(prefix),
                self.sort_slugs[status],
                label=f"dashboard_section/{status}",
            )

    def test_dashboard_tables_sort_by_other_columns(self):
        for status in ("New", "Watch"):
            prefix = status.lower()
            url = (
                reverse("dashboard")
                if status == "New"
                else reverse("dashboard_section", kwargs={"status_key": prefix})
            )
            for column in ("disc_date_string", "-recent_magdate", "recent_mag", "-status_string"):
                self._get_ok(f"{url}?{_sort_param(prefix)}={column}")

    def test_summary_view_sorts_by_name_and_last_obs_date(self):
        url = reverse("transient_summary", kwargs={"status_or_query_name": "Watch"})
        self._assert_sorted(url, "sort", self.sort_slugs["Watch"], label="transient_summary/Watch", column="name")
        self._get_ok(f"{url}?sort=last_obs_date")
        self._get_ok(f"{url}?sort=-last_mag")
