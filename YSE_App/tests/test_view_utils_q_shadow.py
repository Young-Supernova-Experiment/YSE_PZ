"""view_utils.Q is Django's Q, not a multiprocessing queue (#414)."""

from django.db.models import Q as DjangoQ
from django.test import SimpleTestCase

from YSE_App import view_utils


class ViewUtilsQTests(SimpleTestCase):
    def test_q_is_djangos_q(self):
        self.assertIs(view_utils.Q, DjangoQ)
        self.assertIsInstance(view_utils.Q(pk=1) | view_utils.Q(pk=2), DjangoQ)

    def test_no_module_global_queue_shadows_anything(self):
        # the rise/set Queue is gone (#463); nothing but Django's Q may be called Q
        self.assertFalse(hasattr(view_utils, "_RISE_SET_QUEUE"))
        self.assertIs(view_utils.Q, DjangoQ)
