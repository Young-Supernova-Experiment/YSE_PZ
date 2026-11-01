"""Account pages share one tab bar: profile, settings, teams, notification preferences."""

import re

from django.test import TestCase
from django.urls import reverse

from YSE_App.tests.fixtures_minimal import create_test_user


class SettingsTabsTests(TestCase):
    def setUp(self):
        self.client.force_login(create_test_user("settings_tabs_user"))

    def _active_tab(self, url_name):
        r = self.client.get(reverse(url_name))
        self.assertEqual(r.status_code, 200, url_name)
        html = r.content.decode()
        self.assertIn("yse-settings-tabs", html, url_name)
        active = re.findall(r'class="nav-link active" href="([^"]+)"', html)
        self.assertEqual(len(active), 1, (url_name, active))
        return active[0]

    def test_each_page_shows_the_tabs_with_itself_active(self):
        for url_name in ("researcher_profile", "user_settings", "team_list", "notification_preferences"):
            self.assertEqual(self._active_tab(url_name), reverse(url_name), url_name)

    def test_edit_profile_highlights_my_profile(self):
        self.assertEqual(self._active_tab("researcher_profile_edit"), reverse("researcher_profile"))
