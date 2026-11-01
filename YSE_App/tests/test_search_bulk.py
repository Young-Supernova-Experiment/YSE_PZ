"""Bulk actions, CSV export and schema descriptions on the search page (#284, #287).

Bulk actions need POST + CSRF and only touch transients the user may see
listed (``filter_listed_transients``, #607: TNS-named objects, plus objects
whose data the user may see); the CSV holds exactly the rows the search
returns for that user; every filter has a schema description.
"""

import csv
import io

from django.test import Client
from django.urls import reverse

from YSE_App.filters.transient_search import FILTER_HELP, TransientSearchFilterSet
from YSE_App.models import Transient, TransientPhotometry, TransientStatus, TransientTag
from YSE_App.tests.fixtures_minimal import audit_fields, create_minimal_transient
from YSE_App.tests.test_transient_search import PRIVATE_GROUP, SearchFixture, _point


def _hidden_transient(fixture):
    """A non-TNS-named transient whose only data are shared with the private group (#607)."""
    t = create_minimal_transient(fixture.user, name='PS26hid', status_name='Ignore', ra=101.0, dec=-41.0)
    phot = TransientPhotometry.objects.create(
        transient=t, instrument=fixture.instrument, obs_group=fixture.obs_group, **audit_fields(fixture.user))
    phot.groups.add(fixture.group)
    _point(phot, fixture.band, 2, mag=19.0, user=fixture.user)
    return t


def _names(response):
    rows = list(csv.reader(io.StringIO(response.content.decode())))
    return rows[0], {r[0] for r in rows[1:] if r and not r[0].startswith('#')}


class SearchBulkTests(SearchFixture):

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse('search_bulk_action')
        self.watch = TransientStatus.objects.get(name='Watch')

    def _ids(self, *names):
        return [str(Transient.objects.get(name=n).pk) for n in names]

    def test_get_is_refused_and_anonymous_is_redirected(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        resp = Client().post(self.url, {'action': 'status'})
        self.assertEqual(resp.status_code, 302)
        self.assertIn('login', resp['Location'])

    def test_csrf_token_is_required(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        resp = strict.post(self.url, {'action': 'status', 'status_id': self.watch.pk,
                                      'transient_id': self._ids('2026pub')})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Transient.objects.get(name='2026pub').status.name, 'New')

    def test_status_change_for_selected_rows_only(self):
        resp = self.client.post(self.url, {'action': 'status', 'status_id': self.watch.pk,
                                           'transient_id': self._ids('2026pub', 'PS26abc'),
                                           'params': 'status=New'})
        self.assertEqual(resp.status_code, 302)
        self.assertIn('bulk_result=', resp['Location'])
        self.assertIn('status=New', resp['Location'])
        self.assertEqual(Transient.objects.get(name='2026pub').status.name, 'Watch')
        self.assertEqual(Transient.objects.get(name='PS26abc').status.name, 'Watch')
        self.assertEqual(Transient.objects.get(name='2026sea').status.name, 'Following')
        self.assertEqual(Transient.objects.get(name='2026pub').modified_by, self.user)

    def test_select_all_reruns_the_search(self):
        self.client.post(self.url, {'action': 'status', 'status_id': self.watch.pk,
                                    'select_all': '1', 'params': 'status=New'})
        self.assertEqual(
            set(Transient.objects.filter(status=self.watch).values_list('name', flat=True)),
            {'PS26abc', '2026pub', '2026zzz'})
        self.assertEqual(Transient.objects.get(name='2026prv').status.name, 'Ignore')

    def test_add_tag_is_idempotent(self):
        tag = TransientTag.objects.get(name='old')
        data = {'action': 'add_tag', 'tag_id': tag.pk, 'transient_id': self._ids('2026pub', '2026zzz')}
        self.client.post(self.url, data)
        self.client.post(self.url, data)
        tagged = Transient.objects.get(name='2026pub').tags.filter(pk=tag.pk)
        self.assertEqual(tagged.count(), 1)
        self.assertTrue(Transient.objects.get(name='2026zzz').tags.filter(pk=tag.pk).exists())
        self.assertFalse(Transient.objects.get(name='2026sea').tags.filter(pk=tag.pk).exists())

    def test_non_staff_cannot_touch_transients_they_cannot_see(self):
        self.client.force_login(self.outsider)
        private = _hidden_transient(self)
        from YSE_App.services.visibility import filter_listed_transients
        self.assertFalse(filter_listed_transients(self.outsider, Transient.objects.filter(pk=private.pk)).exists())
        self.client.post(self.url, {'action': 'status', 'status_id': self.watch.pk, 'transient_id': [str(private.pk)]})
        private.refresh_from_db()
        self.assertEqual(private.status.name, 'Ignore')
        self.client.post(self.url, {'action': 'status', 'status_id': self.watch.pk, 'select_all': '1',
                                    'params': 'status=Ignore'})
        private.refresh_from_db()
        self.assertEqual(private.status.name, 'Ignore')

    def test_tns_named_objects_are_listed_for_everyone(self):
        """#607: a TNS-named object is public; its basic fields (status) are open like any listed object."""
        self.client.force_login(self.outsider)
        tns_named = Transient.objects.get(name='2026prv')
        self.client.post(self.url, {'action': 'status', 'status_id': self.watch.pk, 'transient_id': [str(tns_named.pk)]})
        tns_named.refresh_from_db()
        self.assertEqual(tns_named.status.name, 'Watch')

    def test_member_of_the_private_group_can(self):
        self.client.force_login(self.member)
        private = Transient.objects.get(name='2026prv')
        self.client.post(self.url, {'action': 'status', 'status_id': self.watch.pk, 'transient_id': [str(private.pk)]})
        private.refresh_from_db()
        self.assertEqual(private.status.name, 'Watch')

    def test_missing_or_bad_arguments_change_nothing(self):
        before = list(Transient.objects.order_by('pk').values_list('status_id', flat=True))
        for data in (
            {'action': 'nonsense', 'transient_id': self._ids('2026pub')},
            {'action': 'status', 'transient_id': self._ids('2026pub')},
            {'action': 'status', 'status_id': 'x', 'transient_id': self._ids('2026pub')},
            {'action': 'add_tag', 'transient_id': self._ids('2026pub')},
            {'action': 'status', 'status_id': self.watch.pk},
            {'action': 'status', 'status_id': self.watch.pk, 'select_all': '1', 'params': 'ra=10'},
        ):
            resp = self.client.post(self.url, data)
            self.assertEqual(resp.status_code, 302, data)
            self.assertIn('bulk_result=', resp['Location'])
        self.assertEqual(before, list(Transient.objects.order_by('pk').values_list('status_id', flat=True)))

    def test_photometry_zip_only_for_visible_rows(self):
        import zipfile
        resp = self.client.post(self.url, {'action': 'photometry', 'transient_id': self._ids('2026pub', '2026sea')})
        self.assertEqual(resp.status_code, 200)
        names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
        self.assertEqual(sorted(names), ['2026pub.csv', '2026sea.csv'])
        hidden = _hidden_transient(self)
        self.client.force_login(self.outsider)
        resp = self.client.post(self.url, {'action': 'photometry', 'transient_id': [str(hidden.pk)]})
        self.assertEqual(zipfile.ZipFile(io.BytesIO(resp.content)).namelist(), [])

    def test_page_offers_the_bulk_form_and_csv_link(self):
        html = self.client.get('/search/?status=New&bulk_result=Done+it').content.decode()
        self.assertIn('id="search-bulk-form"', html)
        self.assertIn('csrfmiddlewaretoken', html)
        self.assertIn('class="search-select-row"', html)
        self.assertIn('/search/export/?status=New', html)
        self.assertIn('Done it', html)
        self.assertNotIn('bulk_result=', html.split('id="search-export-csv"')[1].split('"')[1])


class SearchExportTests(SearchFixture):

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse('search_export_csv')

    def test_csv_matches_the_search(self):
        resp = self.client.get(self.url, {'status': 'New', 'ordering': 'name'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp['Content-Type'].startswith('text/csv'))
        header, names = _names(resp)
        self.assertEqual(header[:3], ['name', 'ra', 'dec'])
        self.assertEqual(names, {'PS26abc', '2026pub'})
        rows = list(csv.reader(io.StringIO(resp.content.decode())))[1:]
        self.assertEqual([r[0] for r in rows], sorted(r[0] for r in rows))

    def test_csv_only_has_rows_the_user_can_see(self):
        _hidden_transient(self)
        self.client.force_login(self.member)
        _, member_names = _names(self.client.get(self.url, {}))
        self.client.force_login(self.outsider)
        _, outsider_names = _names(self.client.get(self.url, {}))
        self.assertIn('PS26hid', member_names)
        self.assertNotIn('PS26hid', outsider_names)
        self.assertIn('2026prv', outsider_names)  # TNS-named: listed for everyone (#607)
        self.assertLessEqual(outsider_names, member_names)
        _, staff_names = _names(Client_logged_in(self.user).get(self.url, {}))
        self.assertEqual(staff_names, outsider_names)  # staff outside the group: like the outsider (#706)

    def test_group_filter_cannot_leak_for_non_staff(self):
        self.client.force_login(self.outsider)
        _, names = _names(self.client.get(self.url, {'visible_to_group': PRIVATE_GROUP}))
        self.assertEqual(names, set())

    def test_invalid_search_is_400_and_anonymous_redirects(self):
        self.assertEqual(self.client.get(self.url, {'status': 'NoSuchStatus'}).status_code, 400)
        self.assertEqual(Client().get(self.url).status_code, 302)

    def test_formula_cells_are_neutralised(self):
        from YSE_App.views import _csv_safe
        self.assertEqual(_csv_safe('=1+1'), "'=1+1")
        self.assertEqual(_csv_safe('@x'), "'@x")
        self.assertEqual(_csv_safe('2026abc'), '2026abc')
        self.assertEqual(_csv_safe(3.5), 3.5)


def Client_logged_in(user):
    client = Client()
    client.force_login(user)
    return client


class SchemaDocsTests(SearchFixture):

    def test_every_filter_has_a_description(self):
        missing = set(TransientSearchFilterSet.base_filters) - set(FILTER_HELP)
        self.assertEqual(missing, set())
        extra = set(FILTER_HELP) - set(TransientSearchFilterSet.base_filters)
        self.assertEqual(extra, set())
        for name, filt in TransientSearchFilterSet.base_filters.items():
            self.assertTrue(filt.extra.get('help_text'), name)

    def test_schema_fields_carry_the_descriptions(self):
        import types
        try:
            import coreapi  # noqa: F401
        except ImportError:  # pragma: no cover - coreapi is pinned in requirements.txt
            self.skipTest('coreapi not installed')
        from django_filters.rest_framework import DjangoFilterBackend
        from YSE_App.api_views import TransientViewSet

        view = TransientViewSet()
        view.request = types.SimpleNamespace(user=self.user)
        fields = {f.name: f for f in DjangoFilterBackend().get_schema_fields(view)}
        self.assertIn('gal_b_abs_min', fields)
        self.assertEqual(fields['gal_b_abs_min'].schema.description, FILTER_HELP['gal_b_abs_min'])
        self.assertEqual(fields['status'].schema.description, FILTER_HELP['status'])
