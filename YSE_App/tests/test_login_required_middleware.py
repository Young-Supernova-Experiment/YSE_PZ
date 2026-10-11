"""Login requirement for every view except a public allowlist (#496), and the
legacy views that now check login, method and ownership (#495, #497-#504)."""

import datetime
import os
import tempfile

from django.contrib.auth.models import AnonymousUser, Group
from django.http import HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import URLPattern, URLResolver, get_resolver, path, reverse
from django.urls.resolvers import ResolverMatch
from django.utils import timezone

from YSE_App import login_required as lr
from YSE_App import views
from YSE_App.models import (
    ClassicalNightType,
    ClassicalObservingDate,
    ClassicalResource,
    FollowupStatus,
    Profile,
    UserTelescopeToFollow,
    YSEOnCallDate,
)
from YSE_App.services.followup_requests import create_or_attach_request
from YSE_App.tests.deploy_checklist_helpers import basic_auth_header, create_telescope, iers_offline
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

PASSWORD = "login-required-test-pw"


# A tiny urlconf for middleware-only checks (views with no decorators).
def _plain_view(request):
    return HttpResponse("plain")


@lr.login_exempt
def _exempt_view(request):
    return HttpResponse("exempt")


urlpatterns = [
    path("plain/", _plain_view, name="plain"),
    path("exempt/", _exempt_view, name="exempt"),
    path("named-public/", _plain_view, name="survey"),
]


# URL names in YSE_App/urls.py (and django.contrib.auth.urls) that anonymous
# users must still reach.
EXPECTED_PUBLIC_NAMES = {
    "index", "survey", "news", "team", "contact", "acknowledgements",
    "auth_login", "auth_logout",
    "login", "logout", "password_reset", "password_reset_done",
    "password_reset_confirm", "password_reset_complete",
    "slack_events", "external_service_run_callback",
}

# Views behind YSE_App.basicauth.login_or_basic_auth_required: scripts and crons
# call these with HTTP Basic credentials, and the decorator refuses anything else.
EXPECTED_BASIC_AUTH_NAMES = {
    "get_transient", "get_rising_transients", "add_transient", "add_yse_survey_obs",
    "add_yse_survey_fields", "add_gw_candidate", "add_transient_phot", "add_transient_spec",
    "get_host", "get_rising_transients_box", "get_new_transients_box", "box_search",
    "query_api", "change_status_for_query", "download_data", "download_spectra",
    "download_photometry", "download_bulk_photometry", "upload_spectrum", "add_survey_obs",
}

EXPECTED_PUBLIC_NAMESPACES = {"admin", "rest_framework"}


def _iter_patterns(patterns, namespaces=(), prefix=""):
    for p in patterns:
        if isinstance(p, URLResolver):
            ns = namespaces + ((p.namespace,) if p.namespace else ())
            yield from _iter_patterns(p.url_patterns, ns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern), p, namespaces


def _is_drf(callback):
    from rest_framework.views import APIView

    cls = getattr(callback, "cls", None)
    return isinstance(cls, type) and issubclass(cls, APIView)


def _expected_public(pattern, namespaces):
    if pattern.name in EXPECTED_PUBLIC_NAMES or pattern.name in EXPECTED_BASIC_AUTH_NAMES:
        return True
    if EXPECTED_PUBLIC_NAMESPACES.intersection(namespaces):
        return True
    if pattern.callback in (views.auth_logout, views.auth_login):
        return True
    return _is_drf(pattern.callback)


class AllowlistOverEveryUrlPatternTests(SimpleTestCase):
    """Walk every resolvable URL pattern and check the middleware's decision."""

    def _decide(self, pattern, namespaces):
        request = RequestFactory().get("/anything/")
        request.user = AnonymousUser()
        request.resolver_match = ResolverMatch(
            pattern.callback, (), {}, url_name=pattern.name, namespaces=list(namespaces),
            app_names=list(namespaces),
        )
        middleware = lr.LoginRequiredMiddleware(lambda r: HttpResponse())
        return middleware.process_view(request, pattern.callback, (), {})

    def test_every_pattern_is_public_only_when_allowlisted(self):
        patterns = list(_iter_patterns(get_resolver().url_patterns))
        self.assertGreater(len(patterns), 150)
        public, private = [], []
        for route, pattern, namespaces in patterns:
            with self.subTest(route=route, name=pattern.name):
                response = self._decide(pattern, namespaces)
                if _expected_public(pattern, namespaces):
                    self.assertIsNone(response, "should be reachable anonymously")
                    public.append(route)
                else:
                    self.assertIsNotNone(response, "should redirect anonymous users to login")
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response["Location"].startswith("/login/?next="), response["Location"])
                    private.append(route)
        self.assertTrue(private)
        self.assertTrue(public)

    def test_allowlisted_names_all_exist(self):
        names = {p.name for _r, p, _ns in _iter_patterns(get_resolver().url_patterns)}
        missing = (EXPECTED_PUBLIC_NAMES | EXPECTED_BASIC_AUTH_NAMES) - names
        self.assertEqual(missing, set())

    def test_basic_auth_views_are_detected_from_the_decorator(self):
        by_name = {p.name: p for _r, p, _ns in _iter_patterns(get_resolver().url_patterns)}
        for name in EXPECTED_BASIC_AUTH_NAMES:
            with self.subTest(name=name):
                self.assertTrue(lr.view_is_public(by_name[name].callback))

    def test_legacy_views_are_not_public(self):
        by_name = {p.name: p for _r, p, _ns in _iter_patterns(get_resolver().url_patterns)}
        for name in ("submit_to_tns", "remove_followup_notice", "add_oncall_observer",
                     "download_target_list", "download_targets_and_finders", "finderchart",
                     "finderim", "view_yse_fields", "return_serialized_transients",
                     "get_ps1_image", "rise_time", "yse_pointings", "move_yse_field",
                     "add_classical_resource", "dashboard", "transient_detail"):
            with self.subTest(name=name):
                self.assertFalse(lr.view_is_public(by_name[name].callback))


@override_settings(ROOT_URLCONF="YSE_App.tests.test_login_required_middleware", LOGIN_URL="/login/")
class MiddlewareBehaviourTests(TestCase):
    def test_undecorated_view_redirects_anonymous_users(self):
        response = Client().get("/plain/?a=1")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/login/?next=/plain/%3Fa%3D1")

    def test_exempt_and_named_public_views_are_reachable(self):
        self.assertEqual(Client().get("/exempt/").status_code, 200)
        self.assertEqual(Client().get("/named-public/").status_code, 200)

    def test_logged_in_users_pass(self):
        client = Client()
        client.force_login(create_test_user("lrm_plain_user", is_staff=False))
        self.assertEqual(client.get("/plain/").status_code, 200)

    @override_settings(LOGIN_REQUIRED_MIDDLEWARE=False)
    def test_rollback_switch_turns_the_check_off(self):
        self.assertEqual(Client().get("/plain/").status_code, 200)

    @override_settings(LOGIN_URL="/yse_experimental/login/")
    def test_sub_path_deployment_keeps_the_script_name(self):
        response = Client().get("/plain/", SCRIPT_NAME="/yse_experimental")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/yse_experimental/login/?next=/yse_experimental/plain/")
        self.assertEqual(Client().get("/exempt/", SCRIPT_NAME="/yse_experimental").status_code, 200)


class PublicPagesStayPublicTests(TestCase):
    def test_public_pages_render_anonymously(self):
        for url in ("/", "/login/", "/survey/", "/news/", "/team/", "/contact/",
                    "/acknowledgements/", reverse("password_reset"), reverse("password_reset_done")):
            with self.subTest(url=url):
                self.assertEqual(Client().get(url).status_code, 200)

    def test_slack_events_endpoint_is_not_redirected(self):
        response = Client().post(
            reverse("slack_events"), data='{"type": "url_verification", "challenge": "c"}',
            content_type="application/json",
        )
        self.assertNotEqual(response.status_code, 302)

    def test_service_run_callback_is_not_redirected(self):
        response = Client().post(
            reverse("external_service_run_callback", args=["00000000-0000-0000-0000-000000000000"]),
            data="{}", content_type="application/json",
        )
        self.assertNotEqual(response.status_code, 302)

    def test_anonymous_api_request_is_answered_by_drf_not_redirected(self):
        response = Client().get("/api/transients/")
        self.assertNotEqual(response.status_code, 302)


class BasicAuthStillWorksTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("lrm_basic_user")
        cls.user.set_password(PASSWORD)
        cls.user.save()
        cls.transient = create_minimal_transient(cls.user, name="2026lrmbasic")

    def test_basic_auth_api_request(self):
        response = Client().get(
            "/api/transients/", HTTP_AUTHORIZATION=basic_auth_header(self.user.username, PASSWORD)
        )
        self.assertEqual(response.status_code, 200)

    def test_basic_auth_legacy_endpoint(self):
        response = Client().get(
            reverse("get_transient", args=[self.transient.slug]),
            HTTP_AUTHORIZATION=basic_auth_header(self.user.username, PASSWORD),
        )
        self.assertEqual(response.status_code, 200)

    def test_token_auth_api_request_reaches_drf(self):
        from django.conf import settings
        from rest_framework.authtoken.models import Token

        token, _ = Token.objects.get_or_create(user=self.user)
        response = Client().get("/api/transients/", HTTP_AUTHORIZATION="Token %s" % token.key)
        self.assertNotEqual(response.status_code, 302)
        classes = settings.REST_FRAMEWORK.get("DEFAULT_AUTHENTICATION_CLASSES", [])
        if "rest_framework.authentication.TokenAuthentication" in classes:
            self.assertEqual(response.status_code, 200)

    def test_ingest_endpoints_accept_basic_auth_posts(self):
        # The endpoints YSE_App/data_ingest and util scripts POST to. The
        # payloads are empty, so only check that the middleware lets the
        # request through to the view's own Basic-auth check and that check passes.
        client = Client(raise_request_exception=False)
        auth = {"HTTP_AUTHORIZATION": basic_auth_header(self.user.username, PASSWORD)}
        for name in ("add_transient", "add_transient_phot", "add_transient_spec",
                     "add_yse_survey_obs", "add_yse_survey_fields", "add_gw_candidate"):
            with self.subTest(name=name):
                response = client.post(reverse(name), data="{}", content_type="application/json", **auth)
                self.assertNotIn(response.status_code, (301, 302, 401, 403))
        response = client.get(reverse("upload_spectrum"), **auth)
        self.assertEqual(response.status_code, 200)

    def test_every_basic_auth_view_passes_the_middleware_with_credentials(self):
        by_name = {p.name: p for _r, p, _ns in _iter_patterns(get_resolver().url_patterns)}
        middleware = lr.LoginRequiredMiddleware(lambda r: HttpResponse())
        for name in EXPECTED_BASIC_AUTH_NAMES:
            with self.subTest(name=name):
                request = RequestFactory().post(
                    "/x/", HTTP_AUTHORIZATION=basic_auth_header(self.user.username, PASSWORD)
                )
                request.user = AnonymousUser()
                request.resolver_match = ResolverMatch(by_name[name].callback, (), {}, url_name=name)
                self.assertIsNone(middleware.process_view(request, by_name[name].callback, (), {}))

    def test_basic_auth_endpoint_without_credentials_is_refused_not_redirected(self):
        response = Client().get(reverse("get_transient", args=[self.transient.slug]))
        self.assertEqual(response.status_code, 403)


class LegacyViewsRequireLoginTests(TestCase):
    """#501, #502, #500, #499: anonymous users are sent to login, with and
    without the middleware (each view has its own decorator too)."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("lrm_legacy_user")
        cls.transient = create_minimal_transient(cls.user, name="2026lrmlegacy")

    def _get_urls(self):
        tid = self.transient.id
        return [
            reverse("return_serialized_transients"),
            reverse("view_yse_fields"),
            reverse("yse_pointings", args=["F1", "snid"]),
            reverse("yse_pointings_plot", args=["F1", "snid"]),
            reverse("adjust_yse_pointings", args=["F1", "snid"]),
            reverse("adjust_yse_pointings_plot", args=["F1", "snid"]),
            reverse("rise_time", args=[tid, 1]),
            reverse("set_time", args=[tid, 1]),
            reverse("moon_angle", args=[tid, 1]),
            reverse("tonight_rise_time", args=[tid, 1]),
            reverse("tonight_set_time", args=[tid, 1]),
            reverse("tonight_moon_angle", args=[tid, 1]),
            reverse("delta_too_hours", args=[1]),
            reverse("get_ps1_image", args=[tid]),
            reverse("get_hst_image", args=[tid]),
            reverse("get_hst_status", args=[tid]),
            reverse("get_chandra_image", args=[tid]),
            reverse("get_chandra_status", args=[tid]),
            reverse("get_legacy_image", args=[tid]),
            reverse("finderchart", args=[tid]),
            reverse("finderim", args=[tid]),
            reverse("download_target_list", args=["Tel", "2026-01-01"]),
            reverse("download_targets_and_finders", args=["Tel", "2026-01-01"]),
            reverse("submit_to_tns", args=[self.transient.name]),
            reverse("add_oncall_observer"),
            reverse("remove_followup_notice", args=[1]),
        ]

    def _post_urls(self):
        return [
            reverse("add_classical_resource"),
            reverse("add_too_resource"),
            reverse("add_transient_observation_task"),
            reverse("add_survey_field"),
            reverse("add_oncall_observer"),
            reverse("add_transient_comment"),
            reverse("add_followup_notice"),
            reverse("remove_followup_notice", args=[1]),
            reverse("automated_spectrum_request"),
            reverse("move_yse_field"),
            reverse("submit_to_tns", args=[self.transient.name]),
        ]

    def _assert_login_redirect(self, response):
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/login/?next="), response["Location"])

    def test_anonymous_get_redirects_to_login(self):
        for enabled in (True, False):
            with override_settings(LOGIN_REQUIRED_MIDDLEWARE=enabled):
                for url in self._get_urls():
                    with self.subTest(url=url, middleware=enabled):
                        self._assert_login_redirect(Client().get(url))

    def test_anonymous_post_redirects_to_login(self):
        for enabled in (True, False):
            with override_settings(LOGIN_REQUIRED_MIDDLEWARE=enabled):
                for url in self._post_urls():
                    with self.subTest(url=url, middleware=enabled):
                        self._assert_login_redirect(
                            Client().post(url, {}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
                        )


class LegacyTnsSubmitTests(TestCase):
    """#495: login, POST and a reporter check before anything is built or sent."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("lrm_tns_staff", is_staff=True)
        cls.member = create_test_user("lrm_tns_member", is_staff=False)
        cls.member.groups.add(Group.objects.get_or_create(name="DECAT")[0])
        cls.outsider = create_test_user("lrm_tns_outsider", is_staff=False)
        cls.url = reverse("submit_to_tns", args=["2026lrmnosuch_cand"])

    def _client(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_get_is_refused(self):
        self.assertEqual(self._client(self.staff).get(self.url).status_code, 405)

    def test_user_outside_reporter_groups_is_forbidden(self):
        response = self._client(self.outsider).post(self.url)
        self.assertEqual(response.status_code, 403)

    def test_staff_and_reporter_group_pass_the_check(self):
        # The transient does not exist, so an allowed user gets a 404 and
        # nothing is sent anywhere.
        self.assertEqual(self._client(self.staff).post(self.url).status_code, 404)
        self.assertEqual(self._client(self.member).post(self.url).status_code, 404)

    def test_permission_helper(self):
        from YSE_App.util.submit_to_tns import user_may_submit_legacy_tns

        self.assertFalse(user_may_submit_legacy_tns(AnonymousUser()))
        self.assertTrue(user_may_submit_legacy_tns(self.staff))
        self.assertTrue(user_may_submit_legacy_tns(self.member))
        self.assertFalse(user_may_submit_legacy_tns(self.outsider))


class RemoveFollowupNoticeTests(TestCase):
    """#497: login, POST and ownership."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = create_test_user("lrm_notice_owner", is_staff=False)
        cls.other = create_test_user("lrm_notice_other", is_staff=False)
        telescope = create_telescope(cls.owner, "LRM-Notice-Tel")
        phone = {"phone_country_code": "0", "phone_area": "000", "phone_first_three": "000",
                 "phone_last_four": "0000", "phone_provider_str": "none"}
        profile = Profile.objects.create(user=cls.owner, **phone, **audit_fields(cls.owner))
        Profile.objects.create(user=cls.other, **phone, **audit_fields(cls.other))
        cls.notice = UserTelescopeToFollow.objects.create(
            profile=profile, telescope=telescope, **audit_fields(cls.owner)
        )

    def _client(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _url(self):
        return reverse("remove_followup_notice", args=[self.notice.pk])

    def test_get_is_refused(self):
        self.assertEqual(self._client(self.owner).get(self._url()).status_code, 405)
        self.assertTrue(UserTelescopeToFollow.objects.filter(pk=self.notice.pk).exists())

    def test_other_user_cannot_remove_it(self):
        response = self._client(self.other).post(self._url(), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(UserTelescopeToFollow.objects.filter(pk=self.notice.pk).exists())

    def test_owner_removes_it(self):
        response = self._client(self.owner).post(self._url(), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["removed"], 1)
        self.assertFalse(UserTelescopeToFollow.objects.filter(pk=self.notice.pk).exists())

    def test_owner_non_ajax_post_redirects_to_dashboard(self):
        response = self._client(self.owner).post(self._url())
        self.assertRedirects(response, reverse("personaldashboard"), fetch_redirect_response=False)


class OncallFormMethodTests(TestCase):
    """#498: GET is refused (it listed the on-call users); POST still works."""

    def test_get_is_refused(self):
        client = Client()
        client.force_login(create_test_user("lrm_oncall_user"))
        n_before = YSEOnCallDate.objects.count()
        self.assertEqual(client.get(reverse("add_oncall_observer")).status_code, 405)
        self.assertEqual(YSEOnCallDate.objects.count(), n_before)


class TargetListVisibilityTests(TestCase):
    """#499: the target-list downloads apply the follow-up visibility filter."""

    @classmethod
    def setUpTestData(cls):
        cls.requester = create_test_user("lrm_target_requester", is_staff=False)
        cls.bystander = create_test_user("lrm_target_bystander", is_staff=False)
        cls.obs_date = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
        _obs_group, instrument, _band = create_instrument_stack(cls.requester, obs_group_name="lrm-target")
        night_type, _ = ClassicalNightType.objects.get_or_create(
            name="Full", defaults=audit_fields(cls.requester)
        )
        resource = ClassicalResource.objects.create(
            telescope=instrument.telescope,
            begin_date_valid=cls.obs_date - datetime.timedelta(days=3),
            end_date_valid=cls.obs_date + datetime.timedelta(days=3),
            **audit_fields(cls.requester),
        )
        cls.night = ClassicalObservingDate.objects.create(
            resource=resource, night_type=night_type, obs_date=cls.obs_date, **audit_fields(cls.requester)
        )
        cls.transient = create_minimal_transient(cls.requester, name="lrmtarget0", ra=50.0, dec=1.0)
        attach_synthetic_photometry(cls.requester, cls.transient, n_points=2)
        status, _ = FollowupStatus.objects.get_or_create(name="Requested", defaults=audit_fields(cls.requester))
        parent, _child, _created = create_or_attach_request(
            cls.requester, cls.transient, status=status,
            valid_start=resource.begin_date_valid - datetime.timedelta(days=1),
            valid_stop=resource.end_date_valid + datetime.timedelta(days=1),
            comment="lrm-comment", classical_resource=resource,
        )
        parent.is_public = False
        parent.save()

    def _download(self, user):
        client = Client()
        client.force_login(user)
        url = reverse("download_target_list", kwargs={
            "telescope": self.night.resource.telescope.name.replace(" ", "_"),
            "obs_date": self.obs_date.strftime("%Y-%m-%d"),
        })
        with iers_offline():
            response = client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_requester_sees_their_follow_up(self):
        self.assertIn("lrmtarget0", self._download(self.requester))

    def test_other_user_does_not(self):
        self.assertNotIn("lrmtarget0", self._download(self.bystander))


class FinderChartPathTests(SimpleTestCase):
    """#500: the transient name is one safe path component under the finder directory."""

    def test_safe_name(self):
        from YSE_App.view_utils import finder_chart_safe_name

        self.assertEqual(finder_chart_safe_name("2026abc"), "2026abc")
        self.assertEqual(finder_chart_safe_name("DECam_2026x_cand"), "DECam_2026x_cand")
        for name in ("../../etc/x", "a/b", "..", ".hidden", "a\\b", ""):
            with self.subTest(name=name):
                safe = finder_chart_safe_name(name)
                self.assertNotIn("/", safe)
                self.assertNotIn("\\", safe)
                self.assertFalse(safe.startswith("."))
                self.assertTrue(safe)

    def test_paths_stay_under_the_finder_directory(self):
        from YSE_App.view_utils import finder_chart_paths, finder_chart_static_relpath

        with tempfile.TemporaryDirectory() as root, override_settings(STATIC_ROOT=root):
            base = os.path.realpath(os.path.join(root, "YSE_App/images/findercharts"))
            for name in ("2026abc", "../../outside", "x/../../y"):
                with self.subTest(name=name):
                    outdir, offsets, finder_png = finder_chart_paths(name)
                    self.assertEqual(os.path.dirname(outdir), base)
                    self.assertEqual(os.path.dirname(offsets), outdir)
                    self.assertEqual(os.path.dirname(finder_png), outdir)
        self.assertEqual(
            finder_chart_static_relpath("2026abc"), "YSE_App/images/findercharts/2026abc/2026abc.finder.png"
        )


class LoginRedirectTargetTests(TestCase):
    """#503: ``next`` must point at this site."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("lrm_login_user", is_staff=False)
        cls.user.set_password(PASSWORD)
        cls.user.save()

    def _login(self, next_url):
        return Client().post(
            reverse("auth_login"), {"username": self.user.username, "password": PASSWORD, "next": next_url}
        )

    def test_local_next_is_followed(self):
        response = self._login("/transient_detail/2026abc/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/transient_detail/2026abc/")

    def test_other_host_next_is_ignored(self):
        for next_url in ("https://example.invalid/", "//example.invalid/x", "https:example.invalid"):
            with self.subTest(next_url=next_url):
                response = self._login(next_url)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], "/dashboard/")


class LogoutIsPostOnlyTests(TestCase):
    """#504: GET shows a confirmation form; only POST logs out."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("lrm_logout_user", is_staff=False)

    def _client(self):
        client = Client()
        client.force_login(self.user)
        return client

    def test_get_does_not_log_out(self):
        for url in (reverse("auth_logout"), "/accounts/logout/", "/api-auth/logout/"):
            with self.subTest(url=url):
                client = self._client()
                response = client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'method="post"')
                self.assertIn("_auth_user_id", client.session)

    def test_post_logs_out(self):
        for url in (reverse("auth_logout"), "/accounts/logout/"):
            with self.subTest(url=url):
                client = self._client()
                self.assertEqual(client.post(url).status_code, 200)
                self.assertNotIn("_auth_user_id", client.session)

    def test_logout_link_in_the_page_chrome_is_a_post_form(self):
        response = self._client().get(reverse("dashboard"))
        html = response.content.decode()
        self.assertIn('action="%s"' % reverse("auth_logout"), html)
        self.assertNotIn('href="%s"' % reverse("auth_logout"), html)
