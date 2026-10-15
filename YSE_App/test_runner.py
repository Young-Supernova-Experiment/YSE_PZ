"""Test runner: the existing suite runs with the onboarding gate off (#630).

Those tests sign in fresh users that have not accepted a User Agreement, so
with the gate on every page would redirect to /agreement/. The gate's own tests
(YSE_App/tests/test_onboarding_gate.py) switch it on with
``override_settings(ONBOARDING_GATE_ENABLED=True)``.
"""

from django.test.runner import DiscoverRunner
from django.test.utils import override_settings


class YSETestRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._onboarding_override = override_settings(ONBOARDING_GATE_ENABLED=False)
        self._onboarding_override.enable()

    def teardown_test_environment(self, **kwargs):
        override = getattr(self, "_onboarding_override", None)
        if override is not None:
            override.disable()
        super().teardown_test_environment(**kwargs)
