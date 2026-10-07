"""view_utils.Q is Django's Q, not the rise/set multiprocessing queue (#414)."""

from django.db.models import Q as DjangoQ
from django.test import SimpleTestCase

from YSE_App import view_utils


class ViewUtilsQTests(SimpleTestCase):
    def test_q_is_djangos_q(self):
        self.assertIs(view_utils.Q, DjangoQ)
        self.assertIsInstance(view_utils.Q(pk=1) | view_utils.Q(pk=2), DjangoQ)

    def test_rise_set_queue_is_separate(self):
        self.assertTrue(hasattr(view_utils._RISE_SET_QUEUE, "put"))
        self.assertTrue(hasattr(view_utils._RISE_SET_QUEUE, "get"))
