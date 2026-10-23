"""Follow-up picker: groups the observing resource does not allow are hidden, not greyed out."""

from types import SimpleNamespace

from django.template.loader import render_to_string
from django.test import SimpleTestCase


def _choice(enabled):
    return {"group": SimpleNamespace(pk=7, name="cfh-team"), "enabled": enabled, "checked": False,
            "is_public_group": False, "hidden": False, "is_default": False}


class FollowupHideIneligibleTests(SimpleTestCase):
    def test_ineligible_group_is_hidden_and_disabled(self):
        html = render_to_string("YSE_App/form_snippets/followup_audience_choice.html", {"choice": _choice(False)})
        self.assertIn('style="display: none;"', html)
        self.assertIn("yse-followup-audience-ineligible", html)
        self.assertIn("disabled", html)

    def test_eligible_group_is_shown(self):
        html = render_to_string("YSE_App/form_snippets/followup_audience_choice.html", {"choice": _choice(True)})
        self.assertNotIn("display: none", html)
        self.assertNotIn("disabled", html)
