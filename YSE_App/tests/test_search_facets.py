"""Facet sidebar with live counts on Transient Search (#842), UI preview only.

Synthetic data only: the five made-up transients of ``SearchFixture`` plus one whose only
photometry is shared with a private group (so a user outside it may not see it listed).

Checks: every facet value's count equals the number of rows clicking it returns, for a
viewer who sees everything listed, a private-group member and an outsider; hidden objects
and hidden photometry never reach the counts; the counts cost six statements whatever
the number of rows and none when cached; the page with the preview off is unchanged;
the partial reload; "Save as Selection" keeps facet parameters; the tag page links to the
tags facet.
"""

import datetime

from django.core.cache import cache
from django.db import connection
from django.http import QueryDict
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from YSE_App.models import Selection, Transient, TransientPhotometry, TransientTag, WebAppColor
from YSE_App.services import search_facets as sf
from YSE_App.services import selections as sel_svc
from YSE_App.tests.fixtures_minimal import create_minimal_transient
from YSE_App.tests.js_syntax_utils import html_script_problems
from YSE_App.tests.test_transient_search import SearchFixture, _point

PREVIEW_COOKIE = "yse-ui-preview"
HIDDEN_NAME = "PSfacethid"  # not TNS-style: listed only for those who may see its data
HIDDEN_TAG = "facet-secret-tag"


def _client(user, preview):
    client = Client()
    client.force_login(user)
    if preview:
        client.cookies[PREVIEW_COOKIE] = "1"
    return client


class FacetFixture(SearchFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        color = WebAppColor.objects.get(color="blue")
        cls.secret_tag = TransientTag.objects.create(name=HIDDEN_TAG, color=color, **cls.audit)
        hidden = create_minimal_transient(cls.user, name=HIDDEN_NAME, status_name="Watch", obs_group_name="YSE",
                                          ra=200.0, dec=10.0)
        hidden.disc_date = timezone.now() - datetime.timedelta(days=2)
        hidden.save()
        hidden.tags.add(cls.secret_tag)
        phot = TransientPhotometry.objects.create(transient=hidden, instrument=cls.instrument,
                                                  obs_group=cls.obs_group, **cls.audit)
        phot.groups.add(cls.group)
        _point(phot, cls.band, 1, mag=18.2, user=cls.user)
        cls.hidden = hidden


def _qd(params):
    data = QueryDict(mutable=True)
    for key, value in params.items():
        if isinstance(value, (list, tuple)):
            data.setlist(key, list(value))
        else:
            data[key] = value
    return data


def _is_active(params, facet_key, value):
    for facet in sf.FACETS:
        if facet.key == facet_key:
            return value in params.getlist(facet.param)
    for facet in sf.RANGE_FACETS:
        if facet.key == facet_key:
            return any(b.key == value and sf._bucket_active(params, facet, b) for b in facet.buckets)
    return False


class FacetCountTests(FacetFixture):
    def _listed_names(self, params, user):
        from YSE_App.filters.transient_search import TransientSearchFilterSet
        from YSE_App.views import search_results_queryset

        request = self.factory.get("/search/", params)
        request.user = user
        fs = TransientSearchFilterSet(request.GET, queryset=search_results_queryset(user), request=request)
        self.assertTrue(fs.is_valid(), fs.errors)
        return set(fs.qs.values_list("name", flat=True)), fs

    def _assert_counts_match_clicks(self, user, start=None):
        start = _qd(start or {})
        _, fs = self._listed_names(start, user)
        counts = sf.facet_counts(fs, user, start, use_cache=False)
        values = sf.all_facet_values(counts)
        self.assertTrue(values)
        for facet_key, value, n in values:
            # a chosen value counts the rows listed now; any other value, the rows clicking it lists
            clicked = start if _is_active(start, facet_key, value) else sf.facet_toggle_params(start, facet_key, value)
            names, _ = self._listed_names(clicked, user)
            self.assertEqual(n, len(names), (user.username, facet_key, value, clicked.urlencode()))
        return counts

    def test_counts_equal_the_rows_each_value_returns(self):
        for user in (self.user, self.member, self.outsider):
            self._assert_counts_match_clicks(user)

    def test_counts_are_over_the_already_filtered_rows(self):
        counts = self._assert_counts_match_clicks(self.user, {"status": ["New"]})
        self.assertEqual(dict(counts["values"]["status"]), {"New": 2})
        self.assertEqual(sum(n for _, n in counts["values"]["obs_group"]), 2)

    def test_status_counts_sum_to_the_result_count(self):
        names, fs = self._listed_names({}, self.member)
        counts = sf.facet_counts(fs, self.member, QueryDict(), use_cache=False)
        self.assertEqual(sum(n for _, n in counts["values"]["status"]), len(names))
        self.assertEqual(dict(counts["values"]["tags"])["young"], 1)

    def test_hidden_object_is_never_counted(self):
        member = self._assert_counts_match_clicks(self.member)
        outsider = self._assert_counts_match_clicks(self.outsider)
        self.assertIn(HIDDEN_TAG, dict(member["values"]["tags"]))
        self.assertNotIn(HIDDEN_TAG, dict(outsider["values"]["tags"]))
        self.assertEqual(dict(member["values"]["status"])["Watch"], dict(outsider["values"]["status"])["Watch"] + 1)
        member_names, _ = self._listed_names({}, self.member)
        outsider_names, _ = self._listed_names({}, self.outsider)
        self.assertIn(HIDDEN_NAME, member_names)
        self.assertNotIn(HIDDEN_NAME, outsider_names)

    def test_hidden_photometry_is_not_in_the_magnitude_buckets(self):
        # 2026prv is listed for everyone (TNS-style name), but its only photometry
        # (peak 19.0) is shared with the private group.
        member = self._assert_counts_match_clicks(self.member)["ranges"]
        outsider = self._assert_counts_match_clicks(self.outsider)["ranges"]
        self.assertEqual(member["mag_19_20"] - outsider["mag_19_20"], 1)
        self.assertEqual(member["mag_18_19"] - outsider["mag_18_19"], 2)  # 2026prv (19.0, on the edge), hidden one

    def test_cache_is_per_viewer(self):
        a = sf.facet_cache_key(self.member, QueryDict("status=New"))
        b = sf.facet_cache_key(self.outsider, QueryDict("status=New"))
        self.assertNotEqual(a, b)
        # paging, sorting and ordering do not change which rows match
        self.assertEqual(a, sf.facet_cache_key(self.member, QueryDict("status=New&page=3&sort=name&ordering=-name")))
        self.assertNotEqual(a, sf.facet_cache_key(self.member, QueryDict("status=Watch")))


class FacetCostTests(FacetFixture):
    def _measure(self, user):
        from YSE_App.filters.transient_search import TransientSearchFilterSet
        from YSE_App.views import search_results_queryset

        request = self.factory.get("/search/", {"peak_mag_max": "21"})
        request.user = user
        fs = TransientSearchFilterSet(request.GET, queryset=search_results_queryset(user), request=request)
        self.assertTrue(fs.is_valid())
        cache.clear()
        with CaptureQueriesContext(connection) as ctx:
            sf.facet_counts(fs, user, request.GET.copy())
        with CaptureQueriesContext(connection) as cached:
            sf.facet_counts(fs, user, request.GET.copy())
        return ctx, cached

    def test_six_counting_statements_none_on_raw_photometry_and_zero_when_cached(self):
        for user in (self.user, self.outsider):
            ctx, cached = self._measure(user)
            sqls = [q["sql"] for q in ctx.captured_queries]
            counting = [s for s in sqls if "COUNT(" in s.upper()]
            # one GROUP BY per value facet plus one aggregate for every range bucket;
            # anything else is the viewer's group lookup for the visible stat values
            self.assertEqual(len(counting), len(sf.FACETS) + 1, sqls)
            # the rest are small per-viewer lookups (groups, access rules), never a scan of the transients
            others = [s for s in sqls if s not in counting]
            self.assertLessEqual(len(others), 6, others)
            for s in others:
                self.assertNotIn("FROM `YSE_App_transient`", s)
                self.assertNotIn('FROM "YSE_App_transient"', s)
            self.assertEqual(len(cached.captured_queries), 0)
            for q in ctx.captured_queries:
                self.assertNotIn("transientphotdata", q["sql"].lower())

    def test_statement_count_does_not_grow_with_rows(self):
        before, _ = self._measure(self.user)
        for i in range(12):
            t = create_minimal_transient(self.user, name="2026fc%s" % chr(ord("a") + i), status_name="New")
            t.tags.add(self.tag_young)
        after, _ = self._measure(self.user)
        self.assertEqual(len(before.captured_queries), len(after.captured_queries))


class FacetPageTests(FacetFixture):
    def test_preview_off_page_has_no_facets(self):
        html = _client(self.user, False).get("/search/").content.decode()
        self.assertNotIn('id="search-facets"', html)
        self.assertNotIn("search-facets.js", html)
        self.assertNotIn('id="search-preview-layout"', html)
        self.assertIn('id="search-group-relations"', html)
        self.assertEqual(html_script_problems(html), [])

    def test_preview_off_ignores_the_partial_header(self):
        response = _client(self.user, False).get("/search/", HTTP_X_YSE_PARTIAL="search")
        self.assertEqual(response["Content-Type"].split(";")[0], "text/html")

    def test_preview_page_shows_facets_with_counts_and_rule_builder_slot(self):
        response = _client(self.member, True).get("/search/?status=New")
        html = response.content.decode()
        self.assertIn('id="search-facets"', html)
        self.assertIn('id="search-rule-builder-slot"', html)
        self.assertIn('id="search-facet-tags"', html)
        self.assertIn('id="search-facet-peak_mag"', html)
        self.assertIn('data-yse-facet="status" data-yse-facet-value="New"', html)
        self.assertIn('aria-current="true"', html)  # New is chosen
        self.assertIn("search-facets.js", html)
        self.assertIn("X-YSE-Partial", response.get("Vary", ""))
        self.assertEqual(html_script_problems(html), [])
        # the filter groups still render
        self.assertIn('id="search-group-relations"', html)

    def test_outsider_page_does_not_leak_hidden_tag(self):
        html = _client(self.outsider, True).get("/search/").content.decode()
        self.assertNotIn(HIDDEN_TAG, html.split('id="search-filters"')[0])  # not in the sidebar
        member_html = _client(self.member, True).get("/search/").content.decode()
        self.assertIn(HIDDEN_TAG, member_html.split('id="search-filters"')[0])

    def test_partial_reload_returns_fragments_and_params(self):
        response = _client(self.member, True).get("/search/?status=New&page=1", HTTP_X_YSE_PARTIAL="search")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-cache", response.get("Cache-Control", ""))
        data = response.json()
        self.assertEqual(data["result_count"], 2)
        self.assertEqual(data["count_text"], "2 matches")
        self.assertIn("status=New", data["bulk_params"])
        self.assertNotIn("page=", data["bulk_params"])
        self.assertIn('data-yse-facet="status"', data["facets_html"])
        self.assertIn("2026pub", data["table_html"])
        self.assertNotIn("2026sea", data["table_html"])
        self.assertIn('id="search-active-filters"', data["chips_html"])
        self.assertIn('name="status"', data["groups_html"])
        self.assertFalse(data["has_errors"])
        # table links never carry the partial marker
        self.assertNotIn("partial", data["table_html"])

    def test_save_as_selection_keeps_facet_params(self):
        client = _client(self.member, True)
        data = client.get("/search/?status=New&tags=old", HTTP_X_YSE_PARTIAL="search").json()
        response = client.post(reverse("selection_save"), {"name": "Facet pick", "visibility": "me",
                                                           "params": data["bulk_params"]})
        self.assertEqual(response.status_code, 302)
        selection = Selection.objects.get(name="Facet pick")
        names = set(sel_svc.selection_queryset(selection, self.member).values_list("name", flat=True))
        self.assertEqual(names, {"PS26abc"})
        self.assertEqual(data["result_count"], 1)

    def test_tag_page_links_to_the_tags_facet(self):
        html = _client(self.member, True).get(reverse("transient_tags")).content.decode()
        self.assertIn('id="tag-facet-link"', html)
        self.assertIn("#search-facet-tags", html)


@override_settings(SEARCH_FACET_CACHE_SECONDS=0)
class FacetNoCacheTests(FacetFixture):
    def test_zero_seconds_turns_the_cache_off(self):
        fs = self.filterset({}, user=self.member)
        sf.facet_counts(fs, self.member, QueryDict())
        self.assertIsNone(cache.get(sf.facet_cache_key(self.member, QueryDict())))


class FacetDisplayTests(FacetFixture):
    def test_toggle_urls_add_and_remove_and_drop_page(self):
        params = QueryDict("status=New&page=4", mutable=True)
        url, active = sf._toggle_value(params, "status", "New")
        self.assertTrue(active)
        self.assertEqual(url, "?")
        url, active = sf._toggle_value(params, "status", "Watch")
        self.assertFalse(active)
        self.assertEqual(QueryDict(url[1:]).getlist("status"), ["New", "Watch"])

    def test_range_bucket_replaces_the_previous_range(self):
        params = QueryDict("peak_mag_min=17&peak_mag_max=18", mutable=True)
        clicked = sf.facet_toggle_params(params, "peak_mag", "mag_18_19")
        self.assertEqual((clicked["peak_mag_min"], clicked["peak_mag_max"]), ("18", "19"))
        cleared = sf.facet_toggle_params(params, "peak_mag", "mag_17_18")
        self.assertNotIn("peak_mag_min", cleared)
        self.assertNotIn("peak_mag_max", cleared)
        none = sf.facet_toggle_params(QueryDict("redshift_min=0.1", mutable=True), "redshift", "z_none")
        self.assertEqual(none.get("has_redshift"), "false")
        self.assertNotIn("redshift_min", none)

    def test_chosen_value_with_no_rows_stays_removable(self):
        counts = {"values": {f.key: [] for f in sf.FACETS}, "ranges": {}}
        facets = sf.build_facets(counts, QueryDict("status=Watch&peak_mag_min=21"))
        status = next(f for f in facets if f["key"] == "status")
        self.assertEqual([(v["value"], v["count"], v["active"]) for v in status["values"]], [("Watch", 0, True)])
        mag = next(f for f in facets if f["key"] == "peak_mag")
        self.assertEqual([(v["value"], v["active"]) for v in mag["values"]], [("mag_21_hi", True)])

    def test_invalid_filters_give_no_facets(self):
        fs = self.filterset({"ra": "10"}, user=self.member)  # RA without Dec
        self.assertIsNone(sf.facet_counts(fs, self.member, QueryDict("ra=10"), use_cache=False))
        self.assertEqual(sf.build_facets(None, QueryDict()), [])
        self.assertTrue(Transient.objects.exists())
