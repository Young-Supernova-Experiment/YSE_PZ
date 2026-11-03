"""Combined candidate list (#849): sources, dedup, "why it's here", the TNS rule, listing rules, pages.

Synthetic data only: made-up users, groups, tags, queries and transients.
"""

import datetime
from unittest import mock

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.http import QueryDict
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App import views as views_module
from YSE_App.common.ui_preview import PREVIEW_COOKIE
from YSE_App.models import (
    FollowupStatus,
    Selection,
    Transient,
    TransientFollowup,
    TransientPhotStat,
    TransientTag,
    UserQuery,
)
from YSE_App.services import combined_list as comb
from YSE_App.services import selections as sel_svc
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    audit_fields,
    create_minimal_transient,
    create_test_user,
)
from YSE_App.tests.test_selections import _ExplorerToDefault, make_query


def _stat(transient, mag, days_ago, now):
    from YSE_App.services import photstat

    when = now - datetime.timedelta(days=days_ago)
    mjd = 40587.0 + when.timestamp() / 86400.0
    return TransientPhotStat.objects.update_or_create(transient=transient, defaults=dict(
        num_obs_global=3, num_det_global=3, last_obs_date=when, last_obs_mjd=mjd,
        last_detected_mag=mag, last_detected_date=when, last_detected_mjd=mjd,
        peak_mag=mag - 0.5, peak_mjd=mjd - 5, decay_rate=0.1,
        schema_version=photstat.SCHEMA_VERSION))[0]


@override_settings(UI_PREVIEW_DEFAULT=False)
class CombinedListBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.viewer = create_test_user("comb_viewer", is_staff=False)
        cls.member = create_test_user("comb_member", is_staff=False)
        cls.other = create_test_user("comb_other", is_staff=False)
        cls.private = Group.objects.create(name="comb-private")
        cls.member.groups.add(cls.private)
        now = timezone.now()
        cls.now = now
        t = {}
        for key in "abcdefghi":
            # TNS-style names: listed for every signed-in user.
            t[key] = create_minimal_transient(cls.viewer, name="2099cl%s" % key)
        cls.t = t
        # A non-TNS object with photometry only the private group may see.
        cls.hidden = create_minimal_transient(cls.viewer, name="combhidden1")
        attach_synthetic_photometry(cls.viewer, cls.hidden, n_points=2).groups.add(cls.private)

        cls.tag = TransientTag.objects.create(name="comb-tag", **audit_fields(cls.viewer))
        for key in ("a", "b", "e"):
            t[key].tags.add(cls.tag)
        cls.hidden.tags.add(cls.tag)

        cls.selection = sel_svc.create_selection(cls.viewer, "Comb sel", search_params="q=2099cla")
        cls.secret = sel_svc.create_selection(cls.other, "Not yours", search_params="q=2099clf")
        cls.query = make_query(cls.viewer, "comb uq",
                               "SELECT t.name FROM YSE_App_transient t WHERE t.name = '2099clb'")
        cls.panel = UserQuery.objects.create(user=cls.viewer, query=cls.query, **audit_fields(cls.viewer))

        # TNS rule: d bright and recent; e bright and recent but already tagged; f too faint; g too old.
        _stat(t["d"], 15.2, 5, now)
        _stat(t["e"], 15.0, 2, now)
        _stat(t["f"], 17.0, 2, now)
        _stat(t["g"], 15.0, 40, now)

        # Follow-up requests: c now, h ten days ago; i private to another user.
        requested, _ = FollowupStatus.objects.get_or_create(name="Requested", defaults=audit_fields(cls.viewer))
        for key, owner, public in (("c", cls.viewer, True), ("h", cls.viewer, True), ("i", cls.other, False)):
            attach_synthetic_photometry(cls.viewer, t[key], n_points=2)
            TransientFollowup.objects.create(
                transient=t[key], status=requested, valid_start=now, valid_stop=now + datetime.timedelta(days=2),
                requested_by=owner, is_public=public, **audit_fields(owner))
        TransientFollowup.objects.filter(transient=t["h"]).update(created_date=now - datetime.timedelta(days=10))

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = mock.patch.object(views_module, "connections", _ExplorerToDefault())
        patcher.start()
        self.addCleanup(patcher.stop)

    def choice(self, **kw):
        params = dict(sel=[self.selection.slug], uq=[self.panel.pk], tag=[self.tag.pk], nights=3, tns=True,
                      explicit=True)
        params.update(kw)
        return comb.Choice(selections=params["sel"], user_queries=params["uq"], tags=params["tag"],
                           nights=params["nights"], tns=params["tns"], explicit=True)

    def rows(self, user, choice):
        sources = comb.build_sources(user, choice)
        names = list(comb.combined_queryset(user, sources).values_list("name", flat=True))
        return names, sources

    def reasons(self, user, choice):
        names, sources = self.rows(user, choice)
        ids = Transient.objects.filter(name__in=names).values_list("pk", flat=True)
        by_pk = comb.reasons_for(sources, ids)
        return {Transient.objects.get(pk=pk).name: labels for pk, labels in by_pk.items()}


class SourceTests(CombinedListBase):
    def test_each_object_once_with_every_source_named(self):
        names, _sources = self.rows(self.viewer, self.choice())
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), {"2099cla", "2099clb", "2099clc", "2099cld", "2099cle"})
        why = self.reasons(self.viewer, self.choice())
        self.assertEqual(why["2099cla"], ["Selection · Comb sel", "Tag · comb-tag"])
        self.assertEqual(why["2099clb"], ["Dashboard · comb uq", "Tag · comb-tag"])
        self.assertEqual(why["2099clc"], ["Requested in the last 3 nights"])
        self.assertEqual(len(why["2099cld"]), 1)
        self.assertTrue(why["2099cld"][0].startswith("TNS"))
        self.assertEqual(comb.reasons_text(why["2099cla"]), "Selection · Comb sel; Tag · comb-tag")

    def test_tns_rule_mag_date_and_already_present(self):
        names, _ = self.rows(self.viewer, self.choice(sel=[], uq=[], tag=[], nights=0))
        self.assertEqual(set(names), {"2099cld", "2099cle"})  # f too faint, g too old
        # e is tagged: it stays in the list, but TNS is not given as a reason.
        self.assertEqual(self.reasons(self.viewer, self.choice())["2099cle"], ["Tag · comb-tag"])
        names, _ = self.rows(self.viewer, self.choice(tns=False))
        self.assertNotIn("2099cld", names)
        self.assertIn("2099cle", names)

    def test_recent_requests_by_nights_and_visibility(self):
        only = dict(sel=[], uq=[], tag=[], tns=False)
        self.assertEqual(self.rows(self.viewer, self.choice(nights=3, **only))[0], ["2099clc"])
        self.assertEqual(set(self.rows(self.viewer, self.choice(nights=14, **only))[0]), {"2099clc", "2099clh"})
        self.assertEqual(self.rows(self.viewer, self.choice(nights=0, **only)), ([], []))
        # i's request is private to its requester.
        self.assertNotIn("2099cli", self.rows(self.viewer, self.choice(nights=14, **only))[0])

    def test_listing_rules_apply_to_every_source(self):
        only_tag = dict(sel=[], uq=[], nights=0, tns=False)
        self.assertIn("combhidden1", self.rows(self.member, self.choice(**only_tag))[0])
        self.assertNotIn("combhidden1", self.rows(self.viewer, self.choice(**only_tag))[0])
        # Another user's private Selection and dashboard panel are not sources for the viewer.
        names, sources = self.rows(self.member, self.choice(sel=[self.secret.slug, self.selection.slug],
                                                            uq=[self.panel.pk], tag=[], nights=0, tns=False))
        self.assertEqual((names, sources), ([], []))

    def test_defaults_and_query_string(self):
        choice = comb.parse_choice(QueryDict(""), self.viewer)
        self.assertFalse(choice.explicit)
        self.assertEqual((choice.selections, choice.user_queries, choice.tags, choice.nights, choice.tns),
                         ([self.selection.slug], [self.panel.pk], [], 3, True))
        again = comb.parse_choice(QueryDict(comb.choice_query(self.choice(nights=5, tns=False))), self.viewer)
        self.assertTrue(again.explicit)
        self.assertEqual((again.selections, again.user_queries, again.tags, again.nights, again.tns),
                         ([self.selection.slug], [self.panel.pk], [self.tag.pk], 5, False))
        clamped = comb.parse_choice(QueryDict("src=1&nights=999&uq=x&tag=-3"), self.viewer)
        self.assertEqual((clamped.nights, clamped.user_queries, clamped.tags), (comb.MAX_NIGHTS, [], []))

    def test_rule_selections_are_in_the_defaults(self):
        # Rule Selections compile since #843, so they are default sources like the other kinds.
        rules = sel_svc.create_selection(self.viewer, "zz rules", kind=Selection.KIND_RULES,
                                     rules={"op": "and", "rules": []})
        choice = comb.parse_choice(QueryDict(""), self.viewer)
        self.assertIn(rules.slug, choice.selections)
        sources = comb.build_sources(self.viewer, choice)
        self.assertTrue(any(s.queryset is not None and not s.error for s in sources if rules.name in s.label))

    def test_nothing_is_stored(self):
        before = {m: m.objects.count() for m in (Selection, UserQuery, TransientTag, TransientFollowup)}
        client = Client()
        client.force_login(self.viewer)
        client.cookies[PREVIEW_COOKIE] = "1"
        self.assertEqual(client.get(reverse("combined_list"), {"src": "1", "tag": self.tag.pk}).status_code, 200)
        self.assertEqual({m: m.objects.count() for m in before}, before)


class PageTests(CombinedListBase):
    def _client(self, preview):
        client = Client()
        client.force_login(self.viewer)
        if preview:
            client.cookies[PREVIEW_COOKIE] = "1"
        return client

    def test_preview_off_is_404_and_sidebar_unchanged(self):
        client = self._client(False)
        self.assertEqual(client.get(reverse("combined_list")).status_code, 404)
        self.assertNotIn('href="%s"' % reverse("combined_list"), client.get("/dashboard/").content.decode())

    def test_anonymous_is_sent_to_login(self):
        response = Client().get(reverse("combined_list"))
        self.assertEqual(response.status_code, 302)

    def test_preview_on_page(self):
        client = self._client(True)
        self.assertIn('href="%s"' % reverse("combined_list"), client.get("/dashboard/").content.decode())
        query = comb.choice_query(self.choice())
        response = client.get(reverse("combined_list") + "?" + query)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("5 objects", html)
        self.assertIn("Why it&#x27;s here", html)
        self.assertIn("Selection · Comb sel; Tag · comb-tag", html)
        self.assertIn('data-yse-list-source="comb"', html)
        self.assertIn("yse-obs-tonight", html)
        self.assertIn("combined-list.js", html)
        self.assertIn('data-yse-storage-key="yse-combined-list:%d"' % self.viewer.pk, html)
        for column in ("Last Det.", "Trend", "Spec. Class", "Redshift", "Host"):
            self.assertIn(column, html)
        table = response.context["table"]
        names = [row.record.name for row in table.page.object_list]
        self.assertEqual(len(names), len(set(names)))

    def test_list_stepping_and_comment(self):
        from YSE_App.list_context import list_step_context, parse_list_params

        query = comb.choice_query(self.choice()) + "&sort=name_string"
        self.assertEqual(parse_list_params(QueryDict("ls=comb&lq=" + "src%3D1")), ("comb", "src=1", ""))
        request = RequestFactory().get("/", {"ls": "comb", "lq": query})
        request.user = self.viewer
        request.session = {}
        step = list_step_context(request, self.t["b"])  # a, b, c, d, e by name
        self.assertEqual((step["position"], step["count"]), (2, 5))
        self.assertIn("/%s/" % self.t["a"].slug, step["prev_url"])
        self.assertIn("/%s/" % self.t["c"].slug, step["next_url"])
        self.assertIn("ls=comb", step["next_url"])
        self.assertIn(reverse("combined_list"), step["back_url"])
        self.assertEqual(step["default_comment"],
                         "In list: Combined candidate list (why: Dashboard · comb uq; Tag · comb-tag)")
