"""Selections (#841): sharing, the compiler, SQL scoping, UserQuery panels, pages, API, ON DELETE.

Synthetic data only: made-up users, teams, transients and queries.
"""

import importlib
from unittest import mock

from django.apps import apps
from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.db import connection, models
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from YSE_App import views as views_module
from YSE_App.common import fk_on_delete
from YSE_App.models import Selection, UserQuery
from YSE_App.services import selections as svc
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    audit_fields,
    create_minimal_transient,
    create_test_user,
)
from YSE_App.tests.test_transient_search import SearchFixture

selection_migration = importlib.import_module("YSE_App.migrations.0066_selection_fk_on_delete")
PREVIEW_COOKIE = "yse-ui-preview"


class _ExplorerToDefault:
    """The saved SQL runs on the 'explorer' alias; in a TestCase only 'default' sees the rows."""

    def __getitem__(self, alias):
        if alias == "explorer":
            return connection
        from django.db import connections

        return connections[alias]


def make_query(user, title, sql):
    from explorer.models import Query

    return Query.objects.create(title=title, sql=sql, description="", snapshot=False, created_by_user=user)


def preview_client(user):
    client = Client()
    client.force_login(user)
    client.cookies[PREVIEW_COOKIE] = "1"
    return client


class SharingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = create_test_user("sel_owner", is_staff=False)
        cls.mate = create_test_user("sel_mate", is_staff=False)
        cls.outsider = create_test_user("sel_outsider", is_staff=False)
        cls.team = Group.objects.create(name="sel-team-a")
        cls.other_team = Group.objects.create(name="sel-team-b")
        cls.owner.groups.add(cls.team)
        cls.mate.groups.add(cls.team)
        cls.outsider.groups.add(cls.other_team)

    def _make(self, visibility, team=None, name="Young things"):
        return svc.create_selection(self.owner, name, visibility=visibility, team=team, search_params="q=2099abc")

    def test_me_is_owner_only(self):
        sel = self._make(Selection.VISIBILITY_ME)
        self.assertTrue(svc.can_view(self.owner, sel))
        self.assertFalse(svc.can_view(self.mate, sel))
        self.assertFalse(svc.can_view(self.outsider, sel))
        self.assertEqual(list(svc.visible_selections(self.mate)), [])
        with self.assertRaises(svc.SelectionAccessError):
            svc.selection_queryset(sel, self.mate)

    def test_team_is_seen_and_edited_by_the_team(self):
        sel = self._make(Selection.VISIBILITY_TEAM, team=self.team)
        self.assertTrue(svc.can_view(self.mate, sel))
        self.assertTrue(svc.can_edit(self.mate, sel))
        self.assertFalse(svc.can_delete(self.mate, sel))
        self.assertFalse(svc.can_view(self.outsider, sel))
        self.assertFalse(svc.can_edit(self.outsider, sel))
        svc.update_selection(self.mate, sel, name="Renamed by mate")
        sel.refresh_from_db()
        self.assertEqual(sel.name, "Renamed by mate")
        self.assertEqual(sel.modified_by, self.mate)
        self.assertEqual(sel.slug, "young-things")  # the slug never changes
        with self.assertRaises(svc.SelectionAccessError):
            svc.update_selection(self.outsider, sel, name="nope")
        with self.assertRaises(svc.SelectionAccessError):
            svc.update_selection(self.mate, sel, visibility=Selection.VISIBILITY_ME)

    def test_all_is_seen_by_everyone_and_edited_by_the_owner(self):
        sel = self._make(Selection.VISIBILITY_ALL)
        for user in (self.owner, self.mate, self.outsider):
            self.assertTrue(svc.can_view(user, sel))
        self.assertTrue(svc.can_edit(self.owner, sel))
        self.assertFalse(svc.can_edit(self.outsider, sel))
        self.assertFalse(svc.can_edit(self.mate, sel))  # no team set
        shared = svc.create_selection(self.owner, "Team-run, public", visibility=Selection.VISIBILITY_ALL,
                                      team=self.team, search_params="")
        self.assertTrue(svc.can_edit(self.mate, shared))
        self.assertFalse(svc.can_edit(self.outsider, shared))

    def test_sharing_needs_a_team_you_belong_to(self):
        with self.assertRaises(svc.SelectionError):
            self._make(Selection.VISIBILITY_TEAM)
        with self.assertRaises(svc.SelectionError):
            self._make(Selection.VISIBILITY_TEAM, team=self.other_team)
        sel = self._make(Selection.VISIBILITY_ME, team=self.team)
        self.assertIsNone(sel.team)  # a private Selection keeps no team

    def test_anonymous_sees_nothing(self):
        from django.contrib.auth.models import AnonymousUser

        sel = self._make(Selection.VISIBILITY_ALL)
        self.assertFalse(svc.can_view(AnonymousUser(), sel))
        self.assertEqual(list(svc.visible_selections(AnonymousUser())), [])

    def test_slugs_are_unique_and_avoid_page_names(self):
        first = self._make(Selection.VISIBILITY_ME, name="Same")
        second = self._make(Selection.VISIBILITY_ME, name="Same")
        self.assertEqual((first.slug, second.slug), ("same", "same-2"))
        self.assertNotEqual(self._make(Selection.VISIBILITY_ME, name="save").slug, "save")

    def test_criteria_are_stored_without_page_state(self):
        sel = svc.create_selection(self.owner, "x", search_params="q=2099abc&page=3&sort=-name&per_page=50&ra_gte=")
        self.assertEqual(sel.search_params, "q=2099abc")
        with self.assertRaises(svc.SelectionError):
            svc.create_selection(self.owner, "bad", search_params="ra_gte=notanumber")
        with self.assertRaises(svc.SelectionError):
            svc.create_selection(self.owner, "", search_params="")

    def test_rules_kind_is_stored_and_compiled(self):
        # The rule-tree compiler comes with #843 (tests in test_selection_rules.py).
        tree = {"op": "and", "rules": [{"field": "has_host", "operator": "is", "value": "true"}]}
        sel = svc.create_selection(self.owner, "rules", kind=Selection.KIND_RULES, rules=tree)
        sel.refresh_from_db()
        self.assertEqual(sel.rules, tree)
        self.assertEqual(list(svc.selection_queryset(sel, self.owner)), [])
        with self.assertRaises(svc.SelectionError):
            svc.create_selection(self.owner, "bad rules", kind=Selection.KIND_RULES, rules={"all": []})


class CompilerMatchesSearchTests(SearchFixture):
    """``selection.queryset(user)`` returns the search page's rows, in its order, for each viewer."""

    PARAMS = ("", "status=New&status=Watch", "has_spectrum=false", "q=PS26", "status=New&has_spectrum=true")

    def _page(self, viewer, params):
        client = Client()
        client.force_login(viewer)
        response = client.get("/search/?" + params)
        self.assertEqual(response.status_code, 200)
        return response

    def test_same_rows_as_the_search_page(self):
        for params in self.PARAMS:
            sel = svc.create_selection(self.user, "match %s" % params, visibility=Selection.VISIBILITY_ALL,
                                       search_params=params)
            for viewer in (self.user, self.member, self.outsider):
                with self.subTest(params=params, viewer=viewer.username):
                    response = self._page(viewer, params)
                    expected = set(response.context["table"].data.data.values_list("name", flat=True))
                    got = set(sel.queryset(viewer).values_list("name", flat=True))
                    self.assertEqual(got, expected)

    def test_same_order_as_the_search_filters(self):
        for params in ("ordering=name", "ordering=-ra&has_spectrum=false"):
            sel = svc.create_selection(self.user, "order %s" % params, visibility=Selection.VISIBILITY_ALL,
                                       search_params=params)
            for viewer in (self.user, self.outsider):
                with self.subTest(params=params, viewer=viewer.username):
                    response = self._page(viewer, params)
                    expected = list(response.context["filterset"].qs.values_list("name", flat=True))
                    got = list(sel.queryset(viewer).values_list("name", flat=True))
                    self.assertEqual(got, expected)

    def test_the_viewer_rule_applies_not_the_owner(self):
        sel = svc.create_selection(self.user, "everything", visibility=Selection.VISIBILITY_ALL, search_params="")
        member_rows = set(sel.queryset(self.member).values_list("name", flat=True))
        outsider_rows = set(sel.queryset(self.outsider).values_list("name", flat=True))
        self.assertTrue(outsider_rows <= member_rows)


class SqlSelectionTests(TestCase):
    """A SQL-backed Selection runs for the viewer, under Explorer's page-query scope (#715, #723)."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = create_test_user("sel_sql_owner", is_staff=True)
        cls.member = create_test_user("sel_sql_member", is_staff=False)
        cls.outsider = create_test_user("sel_sql_outsider", is_staff=False)
        cls.private = Group.objects.create(name="sel-sql-private")
        cls.member.groups.add(cls.private)
        public = create_minimal_transient(cls.owner, name="selsqlpub")
        attach_synthetic_photometry(cls.owner, public, n_points=2)
        hidden = create_minimal_transient(cls.owner, name="selsqlhid")
        phot = attach_synthetic_photometry(cls.owner, hidden, n_points=2)
        phot.groups.add(cls.private)
        cls.query = make_query(cls.owner, "sel sql names",
                               "SELECT t.name FROM YSE_App_transient t WHERE t.name LIKE 'selsql%' ORDER BY t.name")
        cls.refused = make_query(
            cls.owner, "sel sql refused",
            "SELECT t.name FROM YSE_App_transient t WHERE t.id IN (SELECT 1 FROM YSE_App_researcherprofile)")

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = mock.patch.object(views_module, "connections", _ExplorerToDefault())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _names(self, sel, user):
        return set(sel.queryset(user).values_list("name", flat=True))

    def test_rows_are_the_viewers_not_the_owners(self):
        sel = svc.create_selection(self.owner, "sql all", kind=Selection.KIND_SQL, query=self.query,
                                   visibility=Selection.VISIBILITY_ALL)
        with override_settings(DATA_SERVICE_READER_USERNAMES=["sel_sql_owner"]):
            self.assertEqual(self._names(sel, self.owner), {"selsqlpub", "selsqlhid"})
            cache.clear()
            self.assertEqual(self._names(sel, self.member), {"selsqlpub", "selsqlhid"})
            cache.clear()
            self.assertEqual(self._names(sel, self.outsider), {"selsqlpub"})

    def test_owners_cached_result_is_never_served_to_a_scoped_viewer(self):
        sel = svc.create_selection(self.owner, "sql cache", kind=Selection.KIND_SQL, query=self.query,
                                   visibility=Selection.VISIBILITY_ALL)
        cache.set(views_module.explorer_query_cache_key(self.query.id), ["selsqlhid"])
        self.assertNotIn("selsqlhid", self._names(sel, self.outsider))

    def test_sql_the_dashboards_refuse_is_refused_for_non_staff(self):
        sel = svc.create_selection(self.owner, "sql refused", kind=Selection.KIND_SQL, query=self.refused,
                                   visibility=Selection.VISIBILITY_ALL)
        with self.assertRaises(svc.SelectionError):
            sel.queryset(self.outsider)
        with self.assertRaises(svc.SelectionError):
            sel.queryset(self.member)

    def test_no_sql_text_can_be_stored_or_read_back(self):
        with self.assertRaises(svc.SelectionError):
            svc.create_selection(self.outsider, "raw", kind=Selection.KIND_SQL, query="SELECT name FROM x")
        with self.assertRaises(svc.SelectionError):
            svc.create_selection(self.outsider, "none", kind=Selection.KIND_SQL)
        self.assertNotIn("sql", {f.name for f in Selection._meta.get_fields()})
        sel = svc.create_selection(self.outsider, "mine", kind=Selection.KIND_SQL, query=self.query)
        client = Client()
        client.force_login(self.outsider)
        data = client.get("/api/selections/%s/" % sel.slug).json()
        self.assertEqual(data["query"], self.query.id)
        self.assertNotIn("SELECT", str(data))

    def test_deleted_query_is_an_error_not_everything(self):
        sel = svc.create_selection(self.owner, "sql gone", kind=Selection.KIND_SQL, query=self.query)
        self.query.delete()
        sel.refresh_from_db()
        self.assertIsNone(sel.query_id)
        with self.assertRaises(svc.SelectionError):
            sel.queryset(self.owner)


@override_settings(DATA_SERVICE_READER_USERNAMES=["sel_uq_user"])
class UserQueryPanelTests(TestCase):
    """Existing dashboard panels keep working; a Selection only points at them."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("sel_uq_user")
        create_minimal_transient(cls.user, name="seluq-1")
        cls.query = make_query(cls.user, "sel uq panel", "SELECT name FROM YSE_App_transient WHERE name = 'seluq-1'")
        cls.panel = UserQuery.objects.create(user=cls.user, query=cls.query, **audit_fields(cls.user))
        cls.python_panel = UserQuery.objects.create(user=cls.user, python_query="selpanel_unknown_name",
                                                    **audit_fields(cls.user))

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = mock.patch.object(views_module, "connections", _ExplorerToDefault())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = Client()
        self.client.force_login(self.user)

    def _snapshot(self):
        return list(UserQuery.objects.order_by("pk").values())

    def _panel(self):
        return self.client.get(reverse("personaldashboard_section", kwargs={"user_query_id": self.panel.id}))

    def test_selection_links_the_panel_without_changing_it(self):
        before_rows = self._snapshot()
        before = self._panel()
        self.assertEqual(before.status_code, 200)
        self.assertIn(b"seluq-1", before.content)

        sel = svc.selection_from_user_query(self.user, self.panel)
        self.assertEqual((sel.kind, sel.query_id, sel.user_query_id),
                         (Selection.KIND_SQL, self.query.id, self.panel.id))
        self.assertEqual(sel.name, "sel uq panel")
        self.assertEqual(set(sel.queryset(self.user).values_list("name", flat=True)), {"seluq-1"})

        self.assertEqual(self._snapshot(), before_rows)
        after = self._panel()
        self.assertEqual(after.status_code, 200)
        self.assertIn(b"seluq-1", after.content)

        sel.delete()
        self.assertEqual(self._snapshot(), before_rows)

    def test_function_panel_link_and_unknown_names(self):
        with self.assertRaises(svc.SelectionError):
            svc.selection_from_user_query(self.user, self.python_panel)
        other = create_test_user("sel_uq_other", is_staff=False)
        with self.assertRaises(svc.SelectionError):
            svc.selection_from_user_query(other, self.panel)

    def test_deleting_the_panel_keeps_the_selection(self):
        sel = svc.selection_from_user_query(self.user, self.panel)
        panel = UserQuery.objects.create(user=self.user, query=self.query, **audit_fields(self.user))
        sel.user_query = panel
        sel.save()
        panel.delete()
        sel.refresh_from_db()
        self.assertIsNone(sel.user_query_id)
        self.assertEqual(sel.query_id, self.query.id)


class PageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = create_test_user("sel_page_owner", is_staff=False)
        cls.mate = create_test_user("sel_page_mate", is_staff=False)
        cls.outsider = create_test_user("sel_page_outsider", is_staff=False)
        cls.team = Group.objects.create(name="sel-page-team")
        cls.owner.groups.add(cls.team)
        cls.mate.groups.add(cls.team)
        for name in ("2099sela", "2099selb", "2099selc"):  # TNS-style names: listed for everyone
            create_minimal_transient(cls.owner, name=name)

    def _sel(self, visibility=Selection.VISIBILITY_TEAM, team=True):
        return svc.create_selection(self.owner, "Page sel", visibility=visibility,
                                    team=self.team if team else None, search_params="q=2099sel")

    def test_pages_are_preview_only(self):
        sel = self._sel()
        client = Client()
        client.force_login(self.owner)
        for url in (reverse("selection_list"), reverse("selection_detail", kwargs={"slug": sel.slug})):
            self.assertEqual(client.get(url).status_code, 404)
        self.assertEqual(client.post(reverse("selection_save"), {"name": "x", "params": ""}).status_code, 404)
        search = client.get("/search/?q=2099sel").content.decode()
        self.assertNotIn("selection-save-modal", search)
        self.assertNotIn("My Selections", search)

    def test_save_from_search(self):
        client = preview_client(self.owner)
        search = client.get("/search/?q=2099sel").content.decode()
        self.assertIn('id="selection-save-modal"', search)
        response = client.post(reverse("selection_save"), {
            "name": "From search", "visibility": "team", "team": str(self.team.pk),
            "params": "q=2099sel&page=2"})
        sel = Selection.objects.get(name="From search")
        self.assertRedirects(response, reverse("selection_detail", kwargs={"slug": sel.slug}),
                             fetch_redirect_response=False)
        self.assertEqual((sel.search_params, sel.team, sel.owner), ("q=2099sel", self.team, self.owner))
        bad = client.post(reverse("selection_save"), {"name": "", "visibility": "me", "params": "q=2099sel"})
        self.assertEqual(bad.status_code, 302)
        self.assertIn("selection_error=", bad.url)

    def test_list_detail_rename_reshare_delete(self):
        sel = self._sel()
        mate = preview_client(self.mate)
        listing = mate.get(reverse("selection_list")).content.decode()
        self.assertIn('data-selection="%s"' % sel.slug, listing)
        detail = mate.get(reverse("selection_detail", kwargs={"slug": sel.slug}))
        self.assertEqual(detail.status_code, 200)
        html = detail.content.decode()
        self.assertIn("3 matches", html)
        self.assertIn('data-yse-list-source="sel"', html)
        self.assertIn('data-yse-list-tab="%s"' % sel.slug, html)
        self.assertIn("yse-obs-tonight", html)

        mate.post(reverse("selection_edit", kwargs={"slug": sel.slug}), {"name": "Mate's name", "visibility": "all"})
        sel.refresh_from_db()
        self.assertEqual((sel.name, sel.visibility, sel.team), ("Mate's name", "all", self.team))
        mate.post(reverse("selection_delete", kwargs={"slug": sel.slug}))
        self.assertTrue(Selection.objects.filter(pk=sel.pk).exists())

        outsider = preview_client(self.outsider)
        outsider.post(reverse("selection_edit", kwargs={"slug": sel.slug}), {"name": "hijack"})
        sel.refresh_from_db()
        self.assertEqual(sel.name, "Mate's name")

        owner = preview_client(self.owner)
        owner.post(reverse("selection_delete", kwargs={"slug": sel.slug}))
        self.assertFalse(Selection.objects.filter(pk=sel.pk).exists())

    def test_hidden_selection_is_404(self):
        sel = self._sel(visibility=Selection.VISIBILITY_ME, team=False)
        response = preview_client(self.outsider).get(reverse("selection_detail", kwargs={"slug": sel.slug}))
        self.assertEqual(response.status_code, 404)

    def test_list_stepping_from_a_selection(self):
        from YSE_App.list_context import list_step_context
        from YSE_App.models import Transient

        sel = self._sel()
        # Sorted by name on the results page: a, b, c.
        first, middle, last = (Transient.objects.get(name=n) for n in ("2099sela", "2099selb", "2099selc"))
        request = RequestFactory().get("/", {"ls": "sel", "lt": sel.slug, "lq": "sort=name_string"})
        request.user = self.mate
        step = list_step_context(request, middle)
        self.assertIsNotNone(step)
        self.assertEqual((step["position"], step["count"]), (2, 3))
        self.assertIn("/%s/" % first.slug, step["prev_url"])
        self.assertIn("/%s/" % last.slug, step["next_url"])
        self.assertIn("ls=sel", step["next_url"])
        self.assertIn(reverse("selection_detail", kwargs={"slug": sel.slug}), step["back_url"])
        request.user = self.outsider
        self.assertIsNone(list_step_context(request, middle))


class ApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = create_test_user("sel_api_owner", is_staff=False)
        cls.other = create_test_user("sel_api_other", is_staff=False)
        create_minimal_transient(cls.owner, name="2099apia")
        cls.private = svc.create_selection(cls.owner, "Private one", search_params="q=2099apia")
        cls.public = svc.create_selection(cls.owner, "Public one", visibility=Selection.VISIBILITY_ALL,
                                          search_params="q=2099apia")

    def test_list_and_detail_are_per_user_and_read_only(self):
        client = Client()
        client.force_login(self.other)
        data = client.get("/api/selections/").json()
        self.assertEqual([row["slug"] for row in data["results"]], [self.public.slug])
        self.assertEqual(client.get("/api/selections/%s/" % self.private.slug).status_code, 404)
        self.assertEqual(client.post("/api/selections/", {"name": "x"}).status_code, 405)
        self.assertEqual(Client().get("/api/selections/").status_code in (401, 403), True)

    def test_transients_endpoint(self):
        client = Client()
        client.force_login(self.other)
        data = client.get("/api/selections/%s/transients/" % self.public.slug).json()
        self.assertEqual([row["name"] for row in data["results"]], ["2099apia"])


class ForeignKeyListTests(SimpleTestCase):
    def test_every_foreign_key_of_the_new_table_is_listed_with_its_rule(self):
        rules = {models.CASCADE: fk_on_delete.CASCADE, models.SET_NULL: fk_on_delete.SET_NULL}
        model = apps.get_model("YSE_App", "Selection")
        expected = {
            (model._meta.db_table, f.column, f.related_model._meta.db_table, rules[f.remote_field.on_delete])
            for f in model._meta.local_concrete_fields if isinstance(f, models.ForeignKey)
        }
        self.assertEqual(set(selection_migration.FOREIGN_KEYS), expected)

    def test_0062_still_exposes_the_helper_names(self):
        fk_0062 = importlib.import_module("YSE_App.migrations.0062_shared_db_fk_on_delete")
        self.assertIs(fk_0062.current_constraints, fk_on_delete.current_constraints)
        self.assertIs(fk_0062.PLAIN_RULES, fk_on_delete.PLAIN_RULES)


def _raw(statement, params):
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def _delete_known_children(parent_table, parent_id):
    """What develop's Django cascade deletes before the parent: every child row except Selections'."""
    new = {"YSE_App_selection"}
    with connection.cursor() as cursor:
        cursor.execute("SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                       "WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME = %s", [parent_table])
        for table, column in [row for row in cursor.fetchall() if row[0] not in new]:
            cursor.execute(f"DELETE FROM `{table}` WHERE `{column}` = %s", [parent_id])


class RawDeleteTests(TestCase):
    """develop's deletes, as raw SQL, after migration 0066: MySQL cascades or nulls Selection rows."""

    def setUp(self):
        if connection.vendor != "mysql":
            self.skipTest("ON DELETE rules are MySQL constraints")
        self.owner = User.objects.create(username="sel-fk-owner")
        self.editor = User.objects.create(username="sel-fk-editor")
        self.team = Group.objects.create(name="sel-fk-team")
        self.owner.groups.add(self.team)
        staff = create_test_user("sel_fk_staff")
        self.query = make_query(staff, "sel fk query", "SELECT name FROM YSE_App_transient")
        self.panel = UserQuery.objects.create(user=staff, query=self.query, **audit_fields(staff))

    def _selection(self):
        return Selection.objects.create(name="fk", slug="fk-%d" % Selection.objects.count(), owner=self.owner,
                                        visibility="team", team=self.team, kind="sql", query=self.query,
                                        user_query=self.panel, modified_by=self.editor)

    def test_constraints_carry_the_model_rules(self):
        for table, column, _, rule in selection_migration.FOREIGN_KEYS:
            with self.subTest(column=column):
                rows = fk_on_delete.current_constraints(connection, table, column)
                self.assertEqual([row[3] for row in rows], [rule])

    def test_owner_delete_cascades(self):
        sel = self._selection()
        _delete_known_children("auth_user", self.owner.id)
        _raw("DELETE FROM auth_user WHERE id = %s", [self.owner.id])
        self.assertFalse(Selection.objects.filter(pk=sel.pk).exists())

    def test_editor_group_query_and_panel_deletes_set_null(self):
        sel = self._selection()
        _delete_known_children("auth_user", self.editor.id)
        _raw("DELETE FROM auth_user WHERE id = %s", [self.editor.id])
        _delete_known_children("auth_group", self.team.id)
        _raw("DELETE FROM auth_group WHERE id = %s", [self.team.id])
        _raw("DELETE FROM YSE_App_userquery WHERE id = %s", [self.panel.id])
        _delete_known_children("explorer_query", self.query.id)
        _raw("DELETE FROM explorer_query WHERE id = %s", [self.query.id])
        sel.refresh_from_db()
        self.assertEqual((sel.modified_by_id, sel.team_id, sel.user_query_id, sel.query_id),
                         (None, None, None, None))
        self.assertEqual(sel.owner_id, self.owner.id)
