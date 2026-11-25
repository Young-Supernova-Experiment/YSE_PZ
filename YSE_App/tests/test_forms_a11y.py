"""UI/UX forms and accessibility fixes (#815, #817, #820, #830, #833). Synthetic data only."""

from django.test import RequestFactory, TestCase
from django.urls import reverse

from YSE_App import views as views_module
from YSE_App.forms import AddDashboardQueryForm, QueryModelChoiceField
from YSE_App.tests.fixtures_minimal import create_test_user


class QueryPickerTests(TestCase):
    def test_label_from_instance_returns_string(self):
        class Obj:
            def __str__(self):
                return "readable"

        self.assertEqual(QueryModelChoiceField(queryset=None).label_from_instance(Obj()), "readable")

    def test_grouped_choices_and_readable_python_labels(self):
        from explorer.models import Query

        me = create_test_user("fa_me")
        other = create_test_user("fa_other")
        Query.objects.create(title="Mine A", sql="SELECT 1", created_by_user=me)
        Query.objects.create(title="Theirs B", sql="SELECT 1", created_by_user=other)
        form = AddDashboardQueryForm(user=me)
        groups = {g[0]: g[1] for g in form.fields["query"].choices if isinstance(g[1], list)}
        self.assertEqual([label for _, label in groups["My queries"]], ["Mine A"])
        self.assertEqual([label for _, label in groups["Shared"]], ["Theirs B"])
        for key, label in form.fields["python_query"].choices:
            if key:
                self.assertNotIn("_", label)


class DashboardPerPageTests(TestCase):
    def _request(self, query=""):
        request = RequestFactory().get("/dashboard/" + query)
        request.session = {}
        return request

    def test_default_valid_invalid_and_remembered(self):
        request = self._request()
        self.assertEqual(views_module.dashboard_per_page(request), 10)
        request = self._request("?dash_per_page=50")
        self.assertEqual(views_module.dashboard_per_page(request), 50)
        session = request.session
        later = RequestFactory().get("/dashboard/")
        later.session = session
        self.assertEqual(views_module.dashboard_per_page(later), 50)
        bad = RequestFactory().get("/dashboard/?dash_per_page=7")
        bad.session = {}
        self.assertEqual(views_module.dashboard_per_page(bad), 10)

    def test_selector_rendered(self):
        user = create_test_user("fa_dash")
        self.client.force_login(user)
        response = self.client.get(reverse("dashboard"), {"dash_per_page": "25"})
        self.assertContains(response, 'id="dash_per_page"')
        self.assertContains(response, '<option value="25" selected>')


class SearchHelpTextTests(TestCase):
    def test_help_text_rendered(self):
        user = create_test_user("fa_search")
        self.client.force_login(user)
        response = self.client.get(reverse("search"))
        self.assertContains(response, "yse-search-help")
