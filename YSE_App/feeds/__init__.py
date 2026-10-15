"""Other feeds (#280): Hermes / SCiMMA (#281), Einstein Probe (#282), JPL Scout (#283).

Importing this package registers the three feed providers with the broker
registry and the ``feeds.*`` job handlers with the queue; see docs/feeds-*.md.
"""

from YSE_App.feeds import einstein_probe, hermes, scout  # noqa: F401  (registers the providers)
from YSE_App.feeds.base import FeedError, FeedMessage, FeedProvider  # noqa: F401
from YSE_App.feeds.jobs import POLL_KIND, SCREEN_KIND, enqueue_poll, enqueue_screen, run_source  # noqa: F401

__all__ = ["FeedError", "FeedMessage", "FeedProvider", "POLL_KIND", "SCREEN_KIND", "einstein_probe", "enqueue_poll",
           "enqueue_screen", "hermes", "run_source", "scout"]
