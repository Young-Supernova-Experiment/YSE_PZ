"""Write authorization for the REST API, upload endpoints and SQL Explorer (#520-#527)."""

import base64
import json
import os
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Group, Permission, User
from django.test import Client, RequestFactory, TestCase, override_settings
from rest_framework.test import APIClient

from YSE_App.common.authz import (
    bulk_ingest_denial,
    explorer_access_allowed,
    upload_denial,
)
from YSE_App.models import Log, TransientPhotometry, UserQuery
from YSE_App.tests.fixtures_minimal import (
    create_instrument_stack,
    create_minimal_transient,
    ensure_transient_statuses,
)

PASSWORD = "authz-test-pass-1"


def _user(username, **kw):
    user = User.objects.create(username=username, email=f"{username}@example.com", **kw)
    user.set_password(PASSWORD)
    user.save()
    return user


def _basic(user):
    token = base64.b64encode(f"{user.username}:{PASSWORD}".encode()).decode()
    return {"HTTP_AUTHORIZATION": f"Basic {token}"}


class AuthzBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = _user("authz_staff", is_staff=True)
        cls.superuser = _user("authz_super", is_staff=True, is_superuser=True)
        cls.member = _user("authz_member")
        cls.other = _user("authz_other")
        cls.service = _user("authz_service")
        cls.g1 = Group.objects.create(name="authz-g1")
        cls.g2 = Group.objects.create(name="authz-g2")
        cls.member.groups.add(cls.g1)
        cls.other.groups.add(cls.g2)
        cls.writers = Group.objects.get(name=settings.API_WRITERS_GROUP)
        cls.service.groups.add(cls.writers)
        ensure_transient_statuses(cls.staff)
        cls.obs_group, cls.instrument, cls.band = create_instrument_stack(cls.staff, obs_group_name="authz-og")
        cls.telescope = cls.instrument.telescope

    def api(self, user=None):
        client = APIClient()
        if user is not None:
            client.force_authenticate(user=user)
        return client


class ApiRootTests(AuthzBase):
    def test_root_listing_requires_authentication(self):
        self.assertIn(self.api().get("/api/").status_code, (401, 403))

    def test_root_listing_for_signed_in_user(self):
        self.assertEqual(self.api(self.member).get("/api/").status_code, 200)

    def test_schema_requires_authentication(self):
        self.assertIn(self.api().get("/api/schema/").status_code, (401, 403))


class GroupMigrationTests(AuthzBase):
    def test_writer_group_holds_model_permissions(self):
        codenames = set(self.writers.permissions.values_list("codename", flat=True))
        for codename in ("add_transient", "change_transientphotdata", "add_surveyfield", "change_telescope"):
            self.assertIn(codename, codenames)
        self.assertNotIn("delete_transient", codenames)

    def test_explorer_group_exists(self):
        self.assertTrue(Group.objects.filter(name=settings.EXPLORER_ACCESS_GROUP).exists())


class ApiWritePermissionTests(AuthzBase):
    def url(self):
        return f"/api/telescopes/{self.telescope.id}/"

    @override_settings(API_WRITE_PERMISSIONS_ENFORCED=False)
    def test_flag_off_keeps_previous_behaviour(self):
        resp = self.api(self.member).patch(self.url(), {"elevation": 1.0}, format="json")
        self.assertNotEqual(resp.status_code, 403)

    @override_settings(API_WRITE_PERMISSIONS_ENFORCED=True)
    def test_member_without_permission_is_refused(self):
        resp = self.api(self.member).patch(self.url(), {"elevation": 2.0}, format="json")
        self.assertEqual(resp.status_code, 403)
        resp = self.api(self.member).post("/api/telescopes/", {"name": "x"}, format="json")
        self.assertEqual(resp.status_code, 403)

    @override_settings(API_WRITE_PERMISSIONS_ENFORCED=True)
    def test_reads_unchanged(self):
        self.assertEqual(self.api(self.member).get("/api/telescopes/").status_code, 200)

    @override_settings(API_WRITE_PERMISSIONS_ENFORCED=True)
    def test_anonymous_write_refused(self):
        resp = self.api().patch(self.url(), {"elevation": 2.0}, format="json")
        self.assertIn(resp.status_code, (401, 403))

    @override_settings(API_WRITE_PERMISSIONS_ENFORCED=True)
    def test_staff_superuser_and_service_account_allowed(self):
        for user in (self.staff, self.superuser, self.service):
            resp = self.api(user).patch(self.url(), {"elevation": 3.0}, format="json")
            self.assertNotEqual(resp.status_code, 403, user.username)

    @override_settings(API_WRITE_PERMISSIONS_ENFORCED=True)
    def test_explicit_model_permission_allowed(self):
        self.other.user_permissions.add(Permission.objects.get(codename="change_telescope"))
        other = User.objects.get(pk=self.other.pk)  # fresh permission cache
        resp = self.api(other).patch(self.url(), {"elevation": 4.0}, format="json")
        self.assertNotEqual(resp.status_code, 403)

    @override_settings(API_WRITE_PERMISSIONS_ENFORCED=True)
    def test_ui_paths_stay_open_to_members(self):
        transient = create_minimal_transient(self.staff, name="authz-sn")
        resp = self.api(self.member).patch(f"/api/transients/{transient.id}/", {}, format="json")
        self.assertNotEqual(resp.status_code, 403)
        resp = self.api(self.member).post("/api/transienttags/", {"name": "authz-tag"}, format="json")
        self.assertNotEqual(resp.status_code, 403)


class CommentOwnerTests(AuthzBase):
    def setUp(self):
        self.log = Log.objects.create(comment="original", is_public=True,
                                      created_by=self.member, modified_by=self.member)

    def patch(self, user):
        return self.api(user).patch(f"/api/logs/{self.log.id}/", {"comment": f"by {user.username}"}, format="json")

    def test_non_owner_cannot_edit(self):
        self.assertEqual(self.patch(self.other).status_code, 403)
        self.log.refresh_from_db()
        self.assertEqual(self.log.comment, "original")

    def test_owner_can_edit(self):
        self.assertNotEqual(self.patch(self.member).status_code, 403)
        self.log.refresh_from_db()
        self.assertEqual(self.log.comment, "by authz_member")

    def test_staff_can_edit(self):
        self.assertNotEqual(self.patch(self.staff).status_code, 403)
        self.log.refresh_from_db()
        self.assertEqual(self.log.comment, "by authz_staff")


class UserScopedViewsetTests(AuthzBase):
    def _view(self, cls, user):
        view = cls()
        view.request = mock.Mock(user=user)
        return view

    def test_user_queries_scoped_to_owner(self):
        from YSE_App.api_views import UserQueryViewSet

        mine = UserQuery.objects.create(user=self.member, python_query="a", created_by=self.member, modified_by=self.member)
        UserQuery.objects.create(user=self.other, python_query="b", created_by=self.other, modified_by=self.other)
        self.assertEqual(list(self._view(UserQueryViewSet, self.member).get_queryset()), [mine])
        self.assertEqual(self._view(UserQueryViewSet, self.staff).get_queryset().count(), 2)

    def test_user_query_owner_comes_from_request(self):
        from YSE_App.api_views import UserQueryViewSet
        from YSE_App.serializers import UserQuerySerializer

        self.assertTrue(UserQuerySerializer().fields["user"].read_only)
        serializer = mock.Mock()
        self._view(UserQueryViewSet, self.member).perform_create(serializer)
        self.assertEqual(serializer.save.call_args.kwargs["user"], self.member)

    def test_followed_telescopes_scoped_and_checked(self):
        from rest_framework.exceptions import PermissionDenied

        from YSE_App.api_views import UserTelescopeToFollowViewSet
        from YSE_App.models import Profile

        profile = Profile.objects.create(user=self.other, phone_country_code="1", phone_area="0", phone_first_three="0",
                                         phone_last_four="0", phone_provider_str="x",
                                         created_by=self.other, modified_by=self.other)
        view = self._view(UserTelescopeToFollowViewSet, self.member)
        self.assertEqual(view.get_queryset().count(), 0)
        with self.assertRaises(PermissionDenied):
            view.perform_create(mock.Mock(validated_data={"profile": profile}))


class UploadFlagDefaultTests(AuthzBase):
    def test_upload_checks_default_on_and_can_be_turned_off(self):
        from YSE_PZ import settings as project_settings

        with mock.patch.dict(os.environ, {"YSE_NO_SUCH_FLAG": ""}):
            self.assertTrue(project_settings._authz_flag("YSE_NO_SUCH_FLAG", "NO_SUCH_FLAG", default=True))
            self.assertFalse(project_settings._authz_flag("YSE_NO_SUCH_FLAG", "NO_SUCH_FLAG"))
        with mock.patch.dict(os.environ, {"YSE_NO_SUCH_FLAG": "0"}):
            self.assertFalse(project_settings._authz_flag("YSE_NO_SUCH_FLAG", "NO_SUCH_FLAG", default=True))

    def test_checks_apply_without_an_override(self):
        # On unless the stack's settings.ini or env turns it off.
        if not settings.UPLOAD_GROUP_CHECKS_ENFORCED:
            self.skipTest("this environment turns the upload checks off")
        self.assertIsNotNone(bulk_ingest_denial(self.member))
        self.assertIsNone(bulk_ingest_denial(self.service))


class UploadDenialTests(AuthzBase):
    @override_settings(UPLOAD_GROUP_CHECKS_ENFORCED=False)
    def test_flag_off_allows(self):
        self.assertIsNone(upload_denial(self.other, ["authz-g1"]))
        self.assertIsNone(bulk_ingest_denial(self.other))

    @override_settings(UPLOAD_GROUP_CHECKS_ENFORCED=True)
    def test_member_rules(self):
        lone = _user("authz_lone")
        self.assertIsNotNone(upload_denial(lone))
        self.assertIsNone(upload_denial(self.member, ["authz-g1"]))
        self.assertIsNotNone(upload_denial(self.member, ["authz-g2"]))
        own = mock.Mock(created_by_id=self.member.id)
        theirs = mock.Mock(created_by_id=self.other.id)
        self.assertIsNone(upload_denial(self.member, [], [own]))
        self.assertIsNotNone(upload_denial(self.member, [], [theirs]))

    @override_settings(UPLOAD_GROUP_CHECKS_ENFORCED=True)
    def test_trusted_accounts(self):
        theirs = mock.Mock(created_by_id=self.other.id)
        for user in (self.staff, self.superuser, self.service):
            self.assertIsNone(upload_denial(user, ["authz-g2"], [theirs]))
            self.assertIsNone(bulk_ingest_denial(user))
        self.assertIsNotNone(bulk_ingest_denial(self.member))


class UploadEndpointTests(AuthzBase):
    def phot_payload(self, groups, clobber=False, name="authz-phot-sn"):
        return {
            "header": {"clobber": clobber, "mjdmatchmin": 0.01},
            "transient": {"name": name, "ra": 10.0, "dec": 20.0, "status": "New"},
            "photheader": {"instrument": self.instrument.name, "obs_group": self.obs_group.name, "groups": groups},
        }

    def post(self, path, user, payload):
        return Client().post(path, json.dumps(payload), content_type="application/json", **_basic(user))

    @override_settings(UPLOAD_GROUP_CHECKS_ENFORCED=True)
    def test_add_transient_limited_to_trusted_accounts(self):
        self.assertEqual(self.post("/add_transient/", self.member, {}).status_code, 403)
        self.assertEqual(self.post("/add_transient/", self.service, {}).status_code, 200)

    @override_settings(UPLOAD_GROUP_CHECKS_ENFORCED=False)
    def test_add_transient_flag_off(self):
        self.assertEqual(self.post("/add_transient/", self.member, {}).status_code, 200)

    @override_settings(UPLOAD_GROUP_CHECKS_ENFORCED=True)
    def test_phot_groups_must_be_own(self):
        self.assertEqual(self.post("/add_transient_phot/", self.member, self.phot_payload("authz-g2")).status_code, 403)
        self.assertEqual(self.post("/add_transient_phot/", self.member, self.phot_payload("authz-g1")).status_code, 200)

    @override_settings(UPLOAD_GROUP_CHECKS_ENFORCED=True)
    def test_phot_clobber_needs_owner(self):
        transient = create_minimal_transient(self.other, name="authz-owned-sn")
        TransientPhotometry.objects.create(transient=transient, instrument=self.instrument, obs_group=self.obs_group,
                                           created_by=self.other, modified_by=self.other)
        payload = self.phot_payload("", clobber=True, name=transient.name)
        self.assertEqual(self.post("/add_transient_phot/", self.member, payload).status_code, 403)
        self.assertEqual(self.post("/add_transient_phot/", self.service, payload).status_code, 200)

    @override_settings(UPLOAD_GROUP_CHECKS_ENFORCED=False)
    def test_phot_flag_off(self):
        self.assertEqual(self.post("/add_transient_phot/", self.member, self.phot_payload("authz-g2")).status_code, 200)


class ExplorerAccessTests(AuthzBase):
    @override_settings(EXPLORER_RESTRICTED_ACCESS=False)
    def test_default_is_staff(self):
        self.assertTrue(explorer_access_allowed(self.staff))
        self.assertFalse(explorer_access_allowed(self.member))
        request = RequestFactory().get("/")
        request.user = self.staff
        self.assertTrue(settings.EXPLORER_PERMISSION_VIEW(request))

    @override_settings(EXPLORER_RESTRICTED_ACCESS=True)
    def test_restricted_needs_group_or_superuser(self):
        self.assertFalse(explorer_access_allowed(self.staff))
        self.assertTrue(explorer_access_allowed(self.superuser))
        self.staff.groups.add(Group.objects.get(name=settings.EXPLORER_ACCESS_GROUP))
        self.assertTrue(explorer_access_allowed(self.staff))
        request = RequestFactory().get("/")
        request.user = self.member
        self.assertFalse(settings.EXPLORER_PERMISSION_CHANGE(request))

    def test_schema_hides_account_tables(self):
        for prefix in ("auth_", "authtoken_", "django_session", "YSE_App_encryptedcredential", "YSE_App_profile"):
            self.assertIn(prefix, settings.EXPLORER_SCHEMA_EXCLUDE_TABLE_PREFIXES)


class TransientSummaryVisibilityTests(TestCase):
    """transient_summary applies the collaboration visibility filter in every branch (#547)."""

    @classmethod
    def setUpTestData(cls):
        from YSE_App.tests.fixtures_minimal import attach_synthetic_photometry

        cls.staff = _user("sumvis_staff", is_staff=True)
        cls.member = _user("sumvis_member")
        cls.outsider = _user("sumvis_outsider")
        group = Group.objects.create(name="sumvis-group")
        cls.member.groups.add(group)
        cls.transient = create_minimal_transient(cls.staff, name="sumvis-sn")
        phot = attach_synthetic_photometry(cls.staff, cls.transient, n_points=2)
        phot.groups.add(group)

    def names(self, user, slug):
        client = Client()
        client.force_login(user)
        resp = client.get(f"/transient_summary/{slug}/")
        self.assertEqual(resp.status_code, 200)
        return {t.name for t in resp.context["transients"]}

    def check_branch(self, slug):
        self.assertIn("sumvis-sn", self.names(self.staff, slug))
        self.assertIn("sumvis-sn", self.names(self.member, slug))
        self.assertNotIn("sumvis-sn", self.names(self.outsider, slug))

    def test_status_branch(self):
        self.check_branch("New")

    def test_saved_sql_branch(self):
        from explorer.models import Query

        Query.objects.create(title="sumvis query", sql="SELECT name FROM YSE_App_transient", created_by_user=self.staff)
        with mock.patch("YSE_App.views.run_explorer_query_cached", return_value=["sumvis-sn"]), \
                mock.patch("YSE_App.views.dashboard_sql_is_supported", return_value=True):
            self.check_branch("sumvis query")

    def test_python_query_branch(self):
        from YSE_App import views
        from YSE_App.models import Transient

        UserQuery.objects.create(user=self.staff, python_query="sumvis_python_query",
                                 created_by=self.staff, modified_by=self.staff)
        # Python queries are looked up in the registry (#505), not by attribute.
        with mock.patch.dict(views.yse_python_queries.python_query_reg.all,
                             {"sumvis_python_query": lambda: Transient.objects.filter(name="sumvis-sn")}):
            self.check_branch("sumvis_python_query")
