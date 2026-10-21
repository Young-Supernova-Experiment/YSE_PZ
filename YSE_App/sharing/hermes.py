"""Hermes / SCiMMA publishing hook (#281, part of #326).

The sharing queue routes ``SharingSubmission`` rows of kind ``hermes`` (and
every submission of a Hermes ``SharingService``) here. :func:`publish` builds
the Hermes message from the transient and posts it through the Hermes REST
API (:mod:`YSE_App.feeds.hermes`). A service without a topic or a Hermes API
token raises :class:`HermesNotConfigured`, and the submission is marked failed
with that message so the operator can fix the service and retry it.
"""

from __future__ import annotations

from YSE_App.feeds.base import FeedError


class HermesNotConfigured(RuntimeError):
    """The sharing service lacks a Hermes topic or token, or Hermes rejected the message."""


def publish(submission):
    """Publish ``submission`` to ``submission.service.hermes_topic``; returns ``{"uuid": ..., "topic": ...}``."""
    from YSE_App.feeds.hermes import publish_submission

    try:
        return publish_submission(submission)
    except FeedError as exc:
        raise HermesNotConfigured(str(exc))
