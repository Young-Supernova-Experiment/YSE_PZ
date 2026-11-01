"""Allowlist checks for run-time ORM keys (#540).

``YSE_App.common.lookup_guard`` and the places that build ``filter(**...)`` /
``Q(**...)`` / ``order_by()`` keys at run time: the archive flags, the
``legacy.<key>`` annotation filter, the search ordering and the facility
request API's id parameters.
"""

from django.test import Client, RequestFactory, SimpleTestCase, TestCase

from YSE_App import view_utils
from YSE_App.common.lookup_guard import (
    DisallowedLookup,
    checked_lookup,
    checked_ordering,
    is_allowed_lookup,
    is_allowed_ordering,
    positive_int_or_none,
)
from YSE_App.filters.transient_search import (
    ARCHIVE_FLAG_FILTER_FIELDS,
    StableOrderingFilter,
    TransientSearchFilterSet,
    _legacy_filter_columns,
)
from YSE_App.models import Transient
from YSE_App.services.annotations import LEGACY_COLUMNS
from YSE_App.tests.fixtures_minimal import create_minimal_transient, create_test_user


def _filterset(query, user):
    request = RequestFactory().get("/search/?" + query)
    request.user = user
    return TransientSearchFilterSet(request.GET, queryset=Transient.objects.all(), request=request)


class CheckedLookupTests(SimpleTestCase):
    FIELDS = ("has_hst", "antares_classification__name")
    LOOKUPS = ("gte", "lte", "isnull")

    def test_allowed_field_and_lookup(self):
        self.assertEqual(checked_lookup("has_hst", self.FIELDS), "has_hst")
        self.assertEqual(checked_lookup("has_hst__isnull", self.FIELDS, self.LOOKUPS), "has_hst__isnull")
        self.assertEqual(checked_lookup("antares_classification__name", self.FIELDS), "antares_classification__name")
        self.assertEqual(checked_lookup("antares_classification__name__gte", self.FIELDS, self.LOOKUPS),
                         "antares_classification__name__gte")

    def test_unknown_field_refused(self):
        for key in ("has_jwst", "name", "pk", "created_by__password", "has_hstx"):
            with self.subTest(key=key):
                self.assertRaises(DisallowedLookup, checked_lookup, key, self.FIELDS, self.LOOKUPS)

    def test_unlisted_lookup_refused(self):
        for key in ("has_hst__regex", "has_hst__in", "has_hst__startswith", "has_hst__exact__gte"):
            with self.subTest(key=key):
                self.assertRaises(DisallowedLookup, checked_lookup, key, self.FIELDS, self.LOOKUPS)

    def test_extra_relation_hop_refused(self):
        for key in ("antares_classification__created_by__username", "antares_classification__id",
                    "has_hst__isnull__gte", "antares_classification"):
            with self.subTest(key=key):
                self.assertRaises(DisallowedLookup, checked_lookup, key, self.FIELDS, self.LOOKUPS)

    def test_underscore_and_empty_parts_refused_even_if_listed(self):
        fields = ("_private", "has_hst", "a___b")
        for key in ("_private", "has_hst___gte", "__has_hst", "has_hst__", "a___b", "", None, 3):
            with self.subTest(key=key):
                self.assertRaises(DisallowedLookup, checked_lookup, key, fields, ("gte", "_x"))
        self.assertRaises(DisallowedLookup, checked_lookup, "has_hst___x", fields, ("_x",))

    def test_is_allowed_lookup(self):
        self.assertTrue(is_allowed_lookup("has_hst__gte", self.FIELDS, self.LOOKUPS))
        self.assertFalse(is_allowed_lookup("has_hst__regex", self.FIELDS, self.LOOKUPS))


class CheckedOrderingTests(SimpleTestCase):
    FIELDS = ("name", "photstat__peak_mag")

    def test_allowed(self):
        for value in ("name", "-name", "photstat__peak_mag", "-photstat__peak_mag"):
            with self.subTest(value=value):
                self.assertEqual(checked_ordering(value, self.FIELDS), value)
                self.assertTrue(is_allowed_ordering(value, self.FIELDS))

    def test_refused(self):
        for value in ("pk", "--name", "?", "photstat__transient__name", "photstat", "_name", "-_name",
                      "name__lower", "", None):
            with self.subTest(value=value):
                self.assertRaises(DisallowedLookup, checked_ordering, value, self.FIELDS)
                self.assertFalse(is_allowed_ordering(value, self.FIELDS))


class PositiveIntTests(SimpleTestCase):
    def test_values(self):
        self.assertEqual(positive_int_or_none("12"), 12)
        self.assertEqual(positive_int_or_none(7), 7)
        for raw in ("", "0", "-1", "1.5", "1e3", " 1", "abc", "١٢", None, True, 0, -3, [1]):
            with self.subTest(raw=raw):
                self.assertIsNone(positive_int_or_none(raw))


class ArchiveFlagTests(TestCase):
    def setUp(self):
        self.user = create_test_user("lookup_guard_user")
        self.transient = create_minimal_transient(self.user, name="2026guard")

    def test_known_flag_recorded(self):
        self.assertTrue(view_utils._record_archive_flag(self.transient.pk, "has_jwst", True))
        self.transient.refresh_from_db()
        self.assertTrue(self.transient.has_jwst)

    def test_other_column_not_written(self):
        for field in ("name", "has_spitzer", "status_id", "has_jwst__exact", "_state"):
            with self.subTest(field=field):
                with self.assertLogs("YSE_App.view_utils", level="WARNING"):
                    self.assertFalse(view_utils._record_archive_flag(self.transient.pk, field, True))
        self.transient.refresh_from_db()
        self.assertEqual(self.transient.name, "2026guard")

    def test_allowlists_match(self):
        self.assertEqual(set(view_utils.ARCHIVE_FLAG_FIELDS), set(ARCHIVE_FLAG_FILTER_FIELDS))
        for field in ARCHIVE_FLAG_FILTER_FIELDS:
            Transient._meta.get_field(field)  # a real column

    def test_filter_archive_flag_refuses_other_columns(self):
        Transient.objects.filter(pk=self.transient.pk).update(has_hst=False)
        fs = _filterset("", self.user)
        self.assertEqual(list(fs._filter_archive_flag(Transient.objects.all(), "has_hst", False)), [self.transient])
        self.assertRaises(DisallowedLookup, fs._filter_archive_flag, Transient.objects.all(), "name", True)


class LegacyColumnTests(SimpleTestCase):
    def test_every_legacy_column_is_allowed(self):
        allowed = _legacy_filter_columns(LEGACY_COLUMNS)
        self.assertIn("antares_classification__name", allowed)
        self.assertNotIn("antares_classification", allowed)
        for column in allowed:
            with self.subTest(column=column):
                self.assertFalse(any(part.startswith("_") for part in column.split("__")))
                checked_lookup(column + "__isnull", allowed, ("isnull",))


class SearchOrderingTests(TestCase):
    def setUp(self):
        self.user = user = create_test_user("lookup_guard_order")
        self.a = create_minimal_transient(user, name="2026aaa")
        self.b = create_minimal_transient(user, name="2026bbb")

    def test_declared_ordering_applies(self):
        fs = _filterset("ordering=-name", self.user)
        self.assertEqual([t.name for t in fs.qs], ["2026bbb", "2026aaa"])

    def test_undeclared_ordering_is_rejected_by_the_form(self):
        fs = _filterset("ordering=created_by__password", self.user)
        self.assertFalse(fs.is_valid())

    def test_filter_skips_undeclared_values_when_called_directly(self):
        f = StableOrderingFilter(fields=(("name", "name"),))
        qs = Transient.objects.all()
        self.assertIs(f.filter(qs, ["created_by__password"]), qs)
        self.assertIs(f.filter(qs, ["-_state"]), qs)
        ordered = f.filter(qs, ["-name", "created_by__password"])
        self.assertEqual(ordered.query.order_by, ("-name", "-pk"))


class FacilityRequestIdParamTests(TestCase):
    def setUp(self):
        self.staff = create_test_user("lookup_guard_staff", is_staff=True, is_superuser=True)
        self.client = Client()
        self.client.force_login(self.staff)

    def test_non_numeric_ids_return_no_rows(self):
        for query in ("transient=abc", "transient=1__gt", "allocation=-1", "allocation=0", "transient=1%20OR%201"):
            with self.subTest(query=query):
                response = self.client.get("/api/facilityrequests/?" + query, HTTP_ACCEPT="application/json")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["count"], 0)

    def test_numeric_id_accepted(self):
        response = self.client.get("/api/facilityrequests/?transient=999999", HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)

    def test_anonymous_refused(self):
        response = Client().get("/api/facilityrequests/?transient=1", HTTP_ACCEPT="application/json")
        self.assertIn(response.status_code, (302, 401, 403))
