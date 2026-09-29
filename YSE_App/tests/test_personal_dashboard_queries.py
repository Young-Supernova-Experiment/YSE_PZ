"""Regression tests for saved queries on the personal dashboard (issue #203).

Ryan Foley's dashboard on yse_experimental showed ~15 copies of one saved query
and the trash button stopped removing them:

* ``UserQuery`` has no uniqueness, and the add view always created a row, so a
  re-clicked Submit during the slow post-add reload attached the query again.
* ``RemoveDashboardQueryFormView`` deleted one ``pk`` and redirected the AJAX
  caller into a full ``personaldashboard`` render, which queued behind the
  section loads for the duplicated (slow) query and timed out.
"""

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase
from django.urls import reverse

from YSE_App import views as views_module
from YSE_App.models import UserQuery
from YSE_App.services.dashboard_queries import (
    dedupe_user_queries,
    duplicates_of,
    find_duplicate_groups,
    matching_user_queries,
)
from YSE_App.tests.fixtures_minimal import audit_fields, create_test_user

AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


def make_query(user, title):
    from explorer.models import Query

    return Query.objects.create(
        title=title,
        sql="SELECT name FROM YSE_App_transient WHERE name = 'none'",
        description="issue 203",
        snapshot=False,
        created_by_user=user,
    )


def attach(user, query=None, python_query=None, n=1):
    rows = []
    for _ in range(n):
        rows.append(
            UserQuery.objects.create(
                user=user, query=query, python_query=python_query, **audit_fields(user)
            )
        )
    return rows


class PersonalDashboardQueryViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("pdash_dupe_user")
        cls.other = create_test_user("pdash_other_user")
        cls.query = make_query(cls.user, "YSE Magnitude-Limited Sample (min mag < 18.6)")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    # ------------------------------------------------------------------ add

    def test_add_twice_attaches_query_once(self):
        for _ in range(2):
            response = self.client.post(
                reverse("add_dashboard_query"),
                {"query": self.query.id, "python_query": ""},
                **AJAX,
            )
            self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            UserQuery.objects.filter(user=self.user, query=self.query).count(), 1
        )
        self.assertFalse(response.json()["created"])

    def test_add_python_query_twice_attaches_once(self):
        for _ in range(2):
            response = self.client.post(
                reverse("add_dashboard_query"),
                {"query": "", "python_query": "rising_transient_queryset"},
                **AJAX,
            )
            self.assertEqual(response.status_code, 200, response.content)
        rows = UserQuery.objects.filter(
            user=self.user, python_query="rising_transient_queryset"
        )
        self.assertEqual(rows.count(), 1)
        self.assertIsNone(rows.get().query)

    def test_add_without_a_query_is_rejected(self):
        response = self.client.post(
            reverse("add_dashboard_query"), {"query": "", "python_query": ""}, **AJAX
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(UserQuery.objects.filter(user=self.user).exists())

    def test_add_requires_login(self):
        response = Client().post(
            reverse("add_dashboard_query"), {"query": self.query.id, "python_query": ""}, **AJAX
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    # --------------------------------------------------------------- remove

    def test_remove_deletes_every_duplicate_for_that_user(self):
        mine = attach(self.user, query=self.query, n=9)
        theirs = attach(self.other, query=self.query, n=1)[0]
        other_query = make_query(self.user, "Something else")
        keep = attach(self.user, query=other_query)[0]

        response = self.client.post(
            reverse("remove_dashboard_query", kwargs={"pk": mine[3].id}), **AJAX
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["removed"], 9)
        self.assertFalse(UserQuery.objects.filter(user=self.user, query=self.query).exists())
        self.assertTrue(UserQuery.objects.filter(pk=theirs.pk).exists())
        self.assertTrue(UserQuery.objects.filter(pk=keep.pk).exists())

    def test_remove_ajax_answers_json_without_rendering_dashboard(self):
        row = attach(self.user, query=self.query)[0]
        with mock.patch.object(
            views_module, "_personaldashboard_table_for_user_query",
            side_effect=AssertionError("remove must not evaluate saved queries"),
        ):
            response = self.client.post(
                reverse("remove_dashboard_query", kwargs={"pk": row.id}), **AJAX
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertFalse(UserQuery.objects.filter(pk=row.pk).exists())

    def test_remove_non_ajax_still_redirects_to_dashboard(self):
        row = attach(self.user, query=self.query)[0]
        response = self.client.post(reverse("remove_dashboard_query", kwargs={"pk": row.id}))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("personaldashboard"))
        self.assertFalse(UserQuery.objects.filter(pk=row.pk).exists())

    def test_remove_already_removed_row_is_a_no_op(self):
        row = attach(self.user, query=self.query)[0]
        row_id = row.id
        row.delete()
        response = self.client.post(
            reverse("remove_dashboard_query", kwargs={"pk": row_id}), **AJAX
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["removed"], 0)

    def test_cannot_remove_another_users_query(self):
        theirs = attach(self.other, query=self.query)[0]
        response = self.client.post(
            reverse("remove_dashboard_query", kwargs={"pk": theirs.id})
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(UserQuery.objects.filter(pk=theirs.pk).exists())

    def test_remove_python_query_duplicates(self):
        rows = attach(self.user, python_query="rising_transient_queryset", n=3)
        attach(self.user, python_query="fastrising_transient_queryset")
        response = self.client.post(
            reverse("remove_dashboard_query", kwargs={"pk": rows[0].id}), **AJAX
        )
        self.assertEqual(response.json()["removed"], 3)
        self.assertEqual(
            list(UserQuery.objects.filter(user=self.user).values_list("python_query", flat=True)),
            ["fastrising_transient_queryset"],
        )


class DashboardQueryHelperTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("pdash_helper_user")
        cls.other = create_test_user("pdash_helper_other")
        cls.query = make_query(cls.user, "Helper query")

    def test_matching_with_nothing_selected_is_empty(self):
        attach(self.user, query=self.query)
        self.assertFalse(matching_user_queries(self.user).exists())

    def test_duplicates_of_scopes_to_user_and_query(self):
        mine = attach(self.user, query=self.query, n=2)
        attach(self.other, query=self.query)
        attach(self.user, python_query="rising_transient_queryset")
        self.assertEqual(
            set(duplicates_of(mine[0]).values_list("id", flat=True)),
            {row.id for row in mine},
        )

    def test_find_duplicate_groups_keeps_oldest_first(self):
        rows = attach(self.user, query=self.query, n=3)
        attach(self.other, query=self.query)
        groups = find_duplicate_groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual([r.id for r in groups[0]], sorted(r.id for r in rows))

    def test_dedupe_dry_run_deletes_nothing(self):
        attach(self.user, query=self.query, n=3)
        groups, doomed = dedupe_user_queries(dry_run=True)
        self.assertEqual(len(doomed), 2)
        self.assertEqual(UserQuery.objects.count(), 3)

    def test_dedupe_keeps_one_row_per_group(self):
        rows = attach(self.user, query=self.query, n=3)
        py_rows = attach(self.user, python_query="rising_transient_queryset", n=2)
        attach(self.other, query=self.query)
        groups, doomed = dedupe_user_queries()
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(doomed), 3)
        remaining = set(UserQuery.objects.values_list("id", flat=True))
        self.assertIn(rows[0].id, remaining)
        self.assertIn(py_rows[0].id, remaining)
        self.assertEqual(len(remaining), 3)


class DedupeDashboardQueriesCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("pdash_cmd_user")
        cls.other = create_test_user("pdash_cmd_other")
        cls.query = make_query(cls.user, "Command query")

    def _run(self, *args):
        out = StringIO()
        call_command("dedupe_dashboard_queries", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_reports_without_deleting(self):
        attach(self.user, query=self.query, n=4)
        output = self._run("--dry-run")
        self.assertIn("x4", output)
        self.assertIn("would delete", output)
        self.assertIn("Dry run", output)
        self.assertEqual(UserQuery.objects.count(), 4)

    def test_apply_deletes_extra_rows(self):
        rows = attach(self.user, query=self.query, n=4)
        output = self._run()
        self.assertIn("Deleted 3 duplicate row(s).", output)
        self.assertEqual(
            list(UserQuery.objects.values_list("id", flat=True)), [rows[0].id]
        )

    def test_user_filter(self):
        attach(self.user, query=self.query, n=2)
        attach(self.other, query=self.query, n=2)
        self._run("--user", self.other.username)
        self.assertEqual(UserQuery.objects.filter(user=self.user).count(), 2)
        self.assertEqual(UserQuery.objects.filter(user=self.other).count(), 1)

    def test_unknown_user_errors(self):
        with self.assertRaises(CommandError):
            self._run("--user", "nobody-here")

    def test_no_duplicates(self):
        attach(self.user, query=self.query)
        self.assertIn("No duplicates found.", self._run())
