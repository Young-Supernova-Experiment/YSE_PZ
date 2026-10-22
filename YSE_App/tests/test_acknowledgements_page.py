"""/acknowledgements/ renders inside the site layout (it was a bare fragment without base.html)."""

from django.test import TestCase
from django.urls import reverse

from YSE_App.tests.fixtures_minimal import create_test_user


class AcknowledgementsPageTests(TestCase):
    def test_public_page_uses_site_layout(self):
        r = self.client.get(reverse("acknowledgements"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Acknowledging YSE-PZ")
        self.assertContains(r, "main-header")
        self.assertContains(r, "<html", html=False)

    def test_signed_in_page_uses_site_layout(self):
        self.client.force_login(create_test_user("ack_page_user"))
        r = self.client.get(reverse("acknowledgements"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "main-sidebar")
