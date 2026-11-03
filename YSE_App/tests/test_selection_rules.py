"""Rule-tree Selections and the visual rule builder (#843).

Synthetic data only: the made-up transients, users and groups of ``SearchFixture``.
"""

import json

from django.db import connection
from django.test import Client, SimpleTestCase, override_settings
from django.urls import reverse

from YSE_App.filters.transient_search import TransientSearchFilterSet
from YSE_App.models import Selection, Transient
from YSE_App.services import selection_rules as rules_svc
from YSE_App.services import selections as svc
from YSE_App.tests.js_syntax_utils import html_script_problems
from YSE_App.tests.test_transient_search import SearchFixture

PREVIEW_COOKIE = "yse-ui-preview"


def _client(user, preview=True):
    client = Client()
    client.force_login(user)
    if preview:
        client.cookies[PREVIEW_COOKIE] = "1"
    return client


def rule(field, operator, value):
    return {"field": field, "operator": operator, "value": value}


def group(op, *rules):
    return {"op": op, "rules": list(rules)}


class RegistryTests(SimpleTestCase):
    """Field labels come from the registry over the search filters, never database columns."""

    def test_every_operator_uses_a_search_filter(self):
        filters = TransientSearchFilterSet.base_filters
        for rule_field in rules_svc.RULE_FIELDS:
            self.assertTrue(rule_field.operators, rule_field.key)
            for op in rule_field.operators:
                for name, _fixed in op.params:
                    self.assertIn(name, filters, (rule_field.key, op.key))

    def test_keys_are_unique_and_labels_are_human(self):
        keys = [f.key for f in rules_svc.RULE_FIELDS]
        self.assertEqual(len(keys), len(set(keys)))
        for rule_field in rules_svc.RULE_FIELDS:
            self.assertNotIn("__", rule_field.label)
            self.assertNotIn("_", rule_field.label.replace("|b|", ""), rule_field.label)
            self.assertTrue(rule_field.label[0].isupper(), rule_field.label)

    def test_magnitudes_read_brighter_and_fainter(self):
        for key in ("peak_mag", "latest_mag", "deepest_limit"):
            labels = {op.key: op.label for op in rules_svc.get_field(key).operators}
            self.assertEqual(labels, {"brighter": "brighter than", "fainter": "fainter than"})
        brighter = rules_svc.get_field("peak_mag").operator("brighter")
        self.assertEqual(brighter.params[0][0], "peak_mag_max")  # smaller magnitudes are brighter

    def test_spectral_phase_fields_are_offered(self):
        keys = {f.key for f in rules_svc.RULE_FIELDS}
        self.assertIn("spec_phase", keys)
        self.assertIn("spec_phase_discovery", keys)
        op = rules_svc.get_field("spec_phase_discovery").operator("at_least")
        self.assertEqual(dict(op.params), {"spec_phase_min": None, "spec_phase_ref": "discovery"})


@override_settings(DATA_SERVICE_READER_USERNAMES=["search_staff"])
class CompilerTests(SearchFixture):
    """A rule tree selects what the equivalent search filters select, for each viewer."""

    def viewers(self):
        return (self.user, self.member, self.outsider)

    def search_names(self, params, viewer):
        sel = svc.create_selection(self.user, "search %s" % params, visibility=Selection.VISIBILITY_ALL,
                                   search_params=params)
        return set(sel.queryset(viewer).values_list("name", flat=True))

    def rule_names(self, tree, viewer):
        sel = svc.create_selection(self.user, "rules", kind=Selection.KIND_RULES,
                                   visibility=Selection.VISIBILITY_ALL, rules=tree)
        return set(sel.queryset(viewer).values_list("name", flat=True))

    def test_single_rules_match_their_search_filter(self):
        cases = [
            (rule("peak_mag", "brighter", "18"), "peak_mag_max=18"),
            (rule("peak_mag", "fainter", "20"), "peak_mag_min=20"),
            (rule("status", "any", ["New", "Watch"]), "status=New&status=Watch"),
            (rule("has_spectrum", "is", "true"), "has_spectrum=true"),
            (rule("has_spectrum", "is", "false"), "has_spectrum=false"),
            (rule("tags", "all", ["young", "has-host"]), "tags_all=young&tags_all=has-host"),
            (rule("redshift", "at_least", "0.1"), "redshift_min=0.1"),
            (rule("name_contains", "is", "26"), "name_contains=26"),
            (rule("num_det", "at_most", "2"), "num_det_max=2"),
        ]
        for tree_rule, params in cases:
            for viewer in self.viewers():
                with self.subTest(params=params, viewer=viewer.username):
                    self.assertEqual(self.rule_names(group("and", tree_rule), viewer),
                                     self.search_names(params, viewer))

    def test_and_matches_combined_search_filters(self):
        tree = group("and", rule("status", "any", ["New", "Following"]), rule("has_host", "is", "true"))
        for viewer in self.viewers():
            with self.subTest(viewer=viewer.username):
                self.assertEqual(self.rule_names(tree, viewer),
                                 self.search_names("status=New&status=Following&has_host=true", viewer))
        self.assertEqual(self.rule_names(tree, self.user), {"2026sea", "2026pub"})

    def test_or_is_the_union_of_the_searches(self):
        tree = group("or", rule("peak_mag", "brighter", "18"), rule("status", "any", ["Watch"]))
        for viewer in self.viewers():
            with self.subTest(viewer=viewer.username):
                expected = self.search_names("peak_mag_max=18", viewer) | self.search_names("status=Watch", viewer)
                self.assertEqual(self.rule_names(tree, viewer), expected)
        self.assertEqual(self.rule_names(tree, self.user), {"2026sea", "2026zzz"})

    def test_nested_groups(self):
        # status New AND (has a spectrum OR redshift at least 0.1)
        tree = group("and", rule("status", "any", ["New"]),
                     group("or", rule("has_spectrum", "is", "true"), rule("redshift", "at_least", "0.1")))
        for viewer in self.viewers():
            with self.subTest(viewer=viewer.username):
                expected = (self.search_names("status=New&has_spectrum=true", viewer)
                            | self.search_names("status=New&redshift_min=0.1", viewer))
                self.assertEqual(self.rule_names(tree, viewer), expected)
        self.assertEqual(self.rule_names(tree, self.user), {"2026pub"})

    def test_none_of_is_the_complement_within_the_listed_rows(self):
        tree = group("and", rule("status", "none", ["New"]))
        for viewer in self.viewers():
            with self.subTest(viewer=viewer.username):
                everything = self.search_names("", viewer)
                self.assertEqual(self.rule_names(tree, viewer), everything - self.search_names("status=New", viewer))

    def test_empty_tree_selects_every_listed_transient(self):
        for viewer in self.viewers():
            self.assertEqual(self.rule_names(group("and"), viewer), self.search_names("", viewer))

    def test_order_is_the_search_pages(self):
        tree = group("or", rule("status", "any", ["New"]), rule("has_spectrum", "is", "true"))
        sel = svc.create_selection(self.user, "ordered", kind=Selection.KIND_RULES, rules=tree)
        rows = list(sel.queryset(self.user).values_list("pk", flat=True))
        expected = list(svc.base_queryset(self.user).filter(pk__in=rows).values_list("pk", flat=True))
        self.assertEqual(rows, expected)


class VisibilityTests(SearchFixture):
    """A rule tree never shows a viewer a transient or a value they could not see on the search page."""

    def test_private_photometry_does_not_match_for_an_outsider(self):
        tree = group("and", rule("latest_mag", "brighter", "19.2"))
        sel = svc.create_selection(self.member, "bright", kind=Selection.KIND_RULES,
                                   visibility=Selection.VISIBILITY_ALL, rules=tree)
        outsider_rows = set(sel.queryset(self.outsider).values_list("name", flat=True))
        member_rows = set(sel.queryset(self.member).values_list("name", flat=True))
        self.assertNotIn("2026prv", outsider_rows)
        self.assertIn("2026prv", member_rows)  # the member's group sees that photometry

    def test_results_stay_within_the_listed_rows(self):
        trees = [group("and"), group("and", rule("status", "none", ["New"])),
                 group("or", rule("has_spectrum", "is", "false"), rule("peak_mag", "fainter", "0"))]
        for tree in trees:
            sel = svc.create_selection(self.member, "t", kind=Selection.KIND_RULES,
                                       visibility=Selection.VISIBILITY_ALL, rules=tree)
            for viewer in (self.member, self.outsider):
                with self.subTest(tree=tree, viewer=viewer.username):
                    listed = set(svc.base_queryset(viewer).values_list("pk", flat=True))
                    rows = set(sel.queryset(viewer).values_list("pk", flat=True))
                    self.assertTrue(rows <= listed)
                    count = rules_svc.rules_queryset(tree, viewer).count()
                    self.assertEqual(count, len(rows))

    def test_tag_options_are_only_tags_on_listed_transients(self):
        from YSE_App.models import TransientTag, WebAppColor

        color = WebAppColor.objects.first()
        TransientTag.objects.create(name="rule-unused-tag", color=color, **self.audit)
        tags = rules_svc.get_field("tags")
        offered = dict(rules_svc.field_choices(tags, user=self.outsider))
        self.assertNotIn("rule-unused-tag", offered)
        self.assertIn("young", offered)  # on 2026sea, listed for everyone
        kept = dict(rules_svc.field_choices(tags, user=self.outsider, keep={"rule-unused-tag"}))
        self.assertIn("rule-unused-tag", kept)  # a saved rule's own value stays selectable

    def test_private_selection_is_not_compiled_for_others(self):
        sel = svc.create_selection(self.member, "mine", kind=Selection.KIND_RULES, rules=group("and"))
        with self.assertRaises(svc.SelectionAccessError):
            svc.selection_queryset(sel, self.outsider)


class ValidationTests(SearchFixture):
    def assertRejected(self, tree):
        with self.assertRaises(svc.SelectionError):
            rules_svc.clean_rules(tree)
        with self.assertRaises(svc.SelectionError):
            svc.create_selection(self.user, "bad", kind=Selection.KIND_RULES, rules=tree)

    def test_unknown_fields_and_columns_are_rejected(self):
        self.assertRejected(group("and", rule("disc_date__gte", "is", "2026-01-01")))
        self.assertRejected(group("and", rule("peak_mag_max", "is", "18")))  # a filter name is not a field
        self.assertRejected(group("and", rule("", "is", "x")))

    def test_unknown_operators_are_rejected(self):
        self.assertRejected(group("and", rule("peak_mag", "lte", "18")))
        self.assertRejected(group("and", rule("followup_status", "none", ["Requested"])))

    def test_values_are_checked_by_the_filter_field_types(self):
        self.assertRejected(group("and", rule("peak_mag", "brighter", "bright")))
        self.assertRejected(group("and", rule("disc_date", "after", "yesterday-ish")))
        self.assertRejected(group("and", rule("status", "any", ["No such status"])))
        self.assertRejected(group("and", rule("has_spectrum", "is", "maybe")))
        self.assertRejected(group("and", rule("peak_mag", "brighter", ["18", "19"])))
        self.assertRejected(group("and", rule("peak_mag", "brighter", "")))
        self.assertRejected(group("and", rule("status", "any", [])))

    def test_tree_shape_is_checked(self):
        self.assertRejected({"all": []})
        self.assertRejected([rule("peak_mag", "brighter", "18")])
        self.assertRejected({"op": "xor", "rules": []})
        self.assertRejected("not json")
        deep = group("and")
        node = deep
        for _ in range(rules_svc.MAX_DEPTH):
            child = group("or", rule("has_host", "is", "true"))
            node["rules"].append(child)
            node = child
        self.assertRejected(deep)
        self.assertRejected(group("and", *[rule("has_host", "is", "true")] * (rules_svc.MAX_RULES + 1)))

    def test_cleaning_normalises(self):
        cleaned = rules_svc.clean_rules(json.dumps(
            {"op": "OR", "rules": [rule("peak_mag", "brighter", " 18 "), group("and"),
                                   rule("status", "any", ["New", "New", ""])], "extra": 1}))
        self.assertEqual(cleaned, {"op": "or", "rules": [rule("peak_mag", "brighter", "18"),
                                                         rule("status", "any", ["New"])]})

    def test_injection_style_value_is_a_literal(self):
        value = "x'); DROP TABLE dummy; --"  # no % or _: LIKE escapes those in the parameter
        tree = group("and", rule("name_contains", "is", value))
        sel = svc.create_selection(self.user, "inject", kind=Selection.KIND_RULES, rules=tree)
        self.assertEqual(list(sel.queryset(self.user)), [])
        self.assertEqual(Transient.objects.count(), len(self.all_names))  # the table is still there
        sql, params, display = rules_svc.rules_sql(tree, self.user)
        self.assertNotIn(value, sql)
        self.assertTrue(any(value in str(p) for p in params), params)
        self.assertIn("DROP TABLE dummy; --", display)  # shown, quoted, for reading
        self.assertIn("x''); DROP", display)

    def test_update_and_describe(self):
        sel = svc.create_selection(self.user, "described", kind=Selection.KIND_RULES, rules=group(
            "and", rule("peak_mag", "brighter", "19"),
            group("or", rule("status", "any", ["New", "Watch"]), rule("has_spectrum", "is", "true"))))
        self.assertEqual(svc.criteria_summary(sel, self.user), [(
            "Rules", "Peak magnitude brighter than 19 AND (Status is any of New, Watch OR Has a spectrum is Yes)")])
        svc.update_selection(self.user, sel, rules=group("and", rule("has_host", "is", "false")))
        sel.refresh_from_db()
        self.assertEqual(sel.rules, group("and", rule("has_host", "is", "false")))
        with self.assertRaises(svc.SelectionError):
            svc.update_selection(self.user, sel, rules=group("and", rule("nope", "is", "1")))
        search = svc.create_selection(self.user, "search", search_params="status=New")
        with self.assertRaises(svc.SelectionError):
            svc.update_selection(self.user, search, rules=group("and"))


class PageTests(SearchFixture):
    TREE = group("and", rule("peak_mag", "brighter", "18"),
                 group("or", rule("status", "any", ["Following"]), rule("tags", "any", ["old"])))

    def test_preview_off_has_no_builder_and_404s(self):
        client = _client(self.user, preview=False)
        html = client.get("/search/").content.decode()
        self.assertNotIn('id="rule-builder"', html)
        self.assertNotIn("rule-builder.js", html)
        self.assertNotIn("rule-builder.css", html)
        self.assertEqual(client.post(reverse("rule_builder_check"), {"rules": json.dumps(group("and"))}).status_code, 404)
        self.assertEqual(client.post(reverse("rule_builder_save"),
                                     {"name": "x", "rules": json.dumps(group("and"))}).status_code, 404)
        self.assertFalse(Selection.objects.exists())
        sel = svc.create_selection(self.user, "rules", kind=Selection.KIND_RULES, rules=self.TREE)
        self.assertEqual(client.get(reverse("selection_detail", kwargs={"slug": sel.slug})).status_code, 404)

    def test_preview_on_renders_the_builder_in_the_facet_slot(self):
        html = _client(self.user).get("/search/?status=New").content.decode()
        slot = html.split('id="search-rule-builder-slot"', 1)[1].split('id="search-facets-body"', 1)[0]
        self.assertNotIn("hidden>", slot.split(">", 1)[0] + ">")
        self.assertIn('id="rule-builder"', slot)
        self.assertIn('class="yse-rule-row-tpl"', slot)
        self.assertIn('data-yse-rule-field="peak_mag"', slot)
        self.assertIn(">brighter than<", slot)
        self.assertIn(">Peak magnitude<", slot)
        self.assertIn('name="params" value="status=New"', slot)
        self.assertIn("rule-builder.js?v=rule-builder-1", html)
        self.assertIn("rule-builder.css?v=rule-builder-1", html)
        self.assertEqual(html_script_problems(html), [])

    def test_partial_reload_does_not_build_the_builder(self):
        response = _client(self.user).get("/search/?status=New", HTTP_X_YSE_PARTIAL="search")
        self.assertNotIn("rule-builder", json.loads(response.content)["facets_html"])

    def test_check_returns_count_summary_and_sql(self):
        client = _client(self.member)
        response = client.post(reverse("rule_builder_check"),
                               {"rules": json.dumps(self.TREE), "count": "1", "sql": "1"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"], data)
        expected = rules_svc.rules_queryset(self.TREE, self.member).count()
        self.assertEqual(data["count"], expected)
        self.assertIn("Peak magnitude brighter than 18", data["summary"])
        self.assertIn("SELECT", data["sql"])
        self.assertIn("YSE_App_transient", data["sql"])
        bad = client.post(reverse("rule_builder_check"),
                          {"rules": json.dumps(group("and", rule("peak_mag", "brighter", "x")))}).json()
        self.assertFalse(bad["ok"])
        self.assertIn("Peak magnitude", bad["error"])
        self.assertEqual(client.get(reverse("rule_builder_check")).status_code, 405)

    def test_save_then_open_reloads_the_builder(self):
        client = _client(self.user)
        response = client.post(reverse("rule_builder_save"), {
            "name": "Bright and following", "visibility": "me", "rules": json.dumps(self.TREE), "params": "q=x"})
        sel = Selection.objects.get(name="Bright and following")
        self.assertEqual(sel.kind, Selection.KIND_RULES)
        self.assertEqual(sel.rules, self.TREE)
        self.assertRedirects(response, reverse("selection_detail", kwargs={"slug": sel.slug}),
                             fetch_redirect_response=False)
        detail = client.get(reverse("selection_detail", kwargs={"slug": sel.slug}))
        self.assertEqual(detail.status_code, 200)
        html = detail.content.decode()
        names = {row.record.name for row in detail.context["table"].page.object_list}
        self.assertEqual(names, set(sel.queryset(self.user).values_list("name", flat=True)))
        builder = html.split('id="selection-rule-builder"', 1)[1]
        self.assertIn('<option value="peak_mag" selected>', builder)
        self.assertIn('value="18"', builder)
        self.assertIn('<option value="or" selected>', builder)
        self.assertIn('<option value="Following" selected>', builder)
        self.assertIn('<option value="old" selected>', builder)
        self.assertIn("Save rules", builder)
        self.assertIn("rule-builder.js?v=rule-builder-1", html)
        self.assertEqual(html_script_problems(html), [])

    def test_save_error_goes_back_to_the_search(self):
        response = _client(self.user).post(reverse("rule_builder_save"), {
            "name": "Bad", "visibility": "me", "params": "status=New",
            "rules": json.dumps(group("and", rule("nope", "is", "1")))})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/search/?", response["Location"])
        self.assertIn("status=New", response["Location"])
        self.assertIn("selection_error=", response["Location"])
        self.assertFalse(Selection.objects.filter(name="Bad").exists())

    def test_edit_page_saves_rules_and_only_editors_see_the_builder(self):
        sel = svc.create_selection(self.user, "Edit me", kind=Selection.KIND_RULES,
                                   visibility=Selection.VISIBILITY_ALL, rules=self.TREE)
        url = reverse("selection_detail", kwargs={"slug": sel.slug})
        self.assertNotIn('id="selection-rule-builder"', _client(self.outsider).get(url).content.decode())
        new = group("or", rule("has_host", "is", "true"))
        response = _client(self.user).post(reverse("selection_edit", kwargs={"slug": sel.slug}),
                                           {"rules": json.dumps(new), "next": "detail"})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        sel.refresh_from_db()
        self.assertEqual(sel.rules, new)
        _client(self.outsider).post(reverse("selection_edit", kwargs={"slug": sel.slug}),
                                    {"rules": json.dumps(group("and")), "next": "detail"})
        sel.refresh_from_db()
        self.assertEqual(sel.rules, new)  # the outsider cannot edit it

    def test_search_selection_page_has_no_builder(self):
        sel = svc.create_selection(self.user, "plain", search_params="status=New")
        html = _client(self.user).get(reverse("selection_detail", kwargs={"slug": sel.slug})).content.decode()
        self.assertNotIn('id="rule-builder"', html)
        self.assertNotIn("rule-builder.js", html)

    def test_no_table_was_harmed(self):
        # The injection test's value through the page: the check endpoint treats it as text.
        value = "%'; DELETE FROM YSE_App_transient; --"
        data = _client(self.user).post(reverse("rule_builder_check"), {
            "rules": json.dumps(group("and", rule("name_contains", "is", value))), "count": "1"}).json()
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["count"], 0)
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM YSE_App_transient")
            self.assertEqual(cursor.fetchone()[0], len(self.all_names))
