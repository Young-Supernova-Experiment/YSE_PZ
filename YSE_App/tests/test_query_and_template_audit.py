"""Bare-string ``__in`` tag filter (the characters of 'YSE' were matched one by one)
and the automated spectrum request view's template. Synthetic data only."""

from django.test import TestCase
from django.urls import reverse

from YSE_App.models import TransientTag
from YSE_App.queries import yse_python_queries as ypq
from YSE_App.tests.fixtures_minimal import audit_fields, create_minimal_transient, create_test_user

PASSWORD = "query-template-audit-pw"


class _Captured(Exception):
    def __init__(self, qs):
        self.qs = qs


def _capture(qs, user):
    raise _Captured(qs)


class YseTagFilterTests(TestCase):
    def test_rising_queryset_matches_only_the_yse_tag(self):
        user = create_test_user("tag_filter_user")
        audit = audit_fields(user)
        yse_tag = TransientTag.objects.create(name="YSE", **audit)
        single_char = TransientTag.objects.create(name="Y", **audit)
        wanted = create_minimal_transient(user, name="2026aaa")
        decoy = create_minimal_transient(user, name="2026bbb")
        wanted.tags.add(yse_tag)
        decoy.tags.add(single_char)
        original = ypq.annotate_rising_transient_qs
        ypq.annotate_rising_transient_qs = _capture
        try:
            with self.assertRaises(_Captured) as ctx:
                ypq.tns_yse_rising_transient_queryset(ndays=365, user=user)
        finally:
            ypq.annotate_rising_transient_qs = original
        self.assertEqual(sorted(ctx.exception.qs.values_list("name", flat=True)), ["2026aaa"])


class AutomatedSpectrumRequestViewTests(TestCase):
    def test_get_renders_existing_template_for_logged_in_user(self):
        user = create_test_user("spec_request_user")
        user.set_password(PASSWORD)
        user.save()
        self.client.login(username=user.username, password=PASSWORD)
        response = self.client.get(reverse("automated_spectrum_request"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "YSE_App/form_snippets/automated_spectrum_form.html")
