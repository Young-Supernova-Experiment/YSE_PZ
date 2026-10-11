"""Named python queries come from the registry (#505), the summary sort field is
allowlisted (#506), the analysis JSON uses json_script (#507), and invalid
queries raise Http404 (#508)."""

from unittest import mock

from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from YSE_App.queries import yse_python_queries
from YSE_App.tests.fixtures_minimal import create_minimal_transient, create_test_user


class PythonQueryRegistryTests(SimpleTestCase):
    def test_registered_query_is_returned(self):
        self.assertIs(
            yse_python_queries.get_python_query("rising_transient_queryset"),
            yse_python_queries.rising_transient_queryset,
        )

    def test_unregistered_module_attributes_are_refused(self):
        for name in ("annotate_rising_transient_qs", "makeRegistrar", "np", "connection",
                     "__builtins__", "", None, "no_such_query"):
            with self.subTest(name=name):
                with self.assertRaises(KeyError):
                    yse_python_queries.get_python_query(name)

    def test_run_python_query_calls_the_registered_function(self):
        sentinel = object()
        with mock.patch.dict(yse_python_queries.python_query_reg.all, {"lhm_q": lambda: sentinel}):
            self.assertIs(yse_python_queries.run_python_query("lhm_q"), sentinel)


class InvalidQueryIs404Tests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("lhm_query_user")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_query_api_unknown_name(self):
        self.assertEqual(self.client.get(reverse("query_api", args=["np"])).status_code, 404)

    def test_bulk_photometry_unknown_name(self):
        self.assertEqual(
            self.client.get(reverse("download_bulk_photometry", args=["no_such_query"])).status_code, 404
        )


class SummarySortAllowlistTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("lhm_sort_user")
        create_minimal_transient(cls.user, name="2026lhmsort", status_name="New")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _get(self, sort):
        return self.client.get(reverse("transient_summary", args=["New"]), {"sort": sort})

    def test_allowlisted_sort_fields_work(self):
        for sort in ("name", "-name", "-disc_date", "obs_group__name"):
            with self.subTest(sort=sort):
                self.assertEqual(self._get(sort).status_code, 200)

    def test_other_sort_values_are_ignored(self):
        # "--name" and "no_such_field" raised FieldError (500) when passed to order_by.
        for sort in ("--name", "no_such_field", "created_by__password", "?"):
            with self.subTest(sort=sort):
                self.assertEqual(self._get(sort).status_code, 200)

    def test_allowlist(self):
        from YSE_App.views import _TRANSIENT_SUMMARY_SORT_FIELDS

        self.assertNotIn("created_by__password", _TRANSIENT_SUMMARY_SORT_FIELDS)
        self.assertIn("last_mag", _TRANSIENT_SUMMARY_SORT_FIELDS)


class AnalysisJsonScriptTests(SimpleTestCase):
    def test_template_uses_json_script(self):
        from django.template import loader

        source = loader.get_template("YSE_App/transient_detail_analysis_tab.html").template.source
        self.assertIn('analysis_services_data|json_script:"yse_analysis_services_json"', source)
        self.assertNotIn("analysis_services_json|safe", source)

    def test_json_script_escapes_markup(self):
        from django.template import Context, Template

        html = Template('{{ data|json_script:"yse_analysis_services_json" }}').render(
            Context({"data": [{"name": "</script><b>x"}]})
        )
        self.assertNotIn("</script><b>", html)
        self.assertIn("\\u003C/script\\u003E", html)
