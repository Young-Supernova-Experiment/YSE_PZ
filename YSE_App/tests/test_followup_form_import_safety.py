"""Ensure TransientFollowupForm does not query ClassicalResource at import time."""

from django.test import SimpleTestCase

from YSE_App.forms import TransientFollowupForm
from YSE_App.models import ClassicalResource


class TransientFollowupFormImportSafetyTests(SimpleTestCase):
    def test_classical_resource_field_defaults_to_empty_queryset(self):
        field = TransientFollowupForm.base_fields["classical_resource"]
        self.assertIs(field.queryset.model, ClassicalResource)
        # ``.none()`` must not require DB columns such as creator_only.
        self.assertEqual(list(field.queryset), [])
