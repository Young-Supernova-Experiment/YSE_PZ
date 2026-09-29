"""Hermes / SCiMMA publishing hook (#281, part of #326).

Publishing to Hermes is out of scope for the TNS slice; the queue routes
``SharingSubmission`` rows of kind ``hermes`` (and TNS submissions whose
service is a Hermes service) here so the wiring exists. Until the Hermes
client lands, :func:`publish` raises :class:`HermesNotConfigured` and the
submission is marked failed with that message.
"""

from __future__ import annotations


class HermesNotConfigured(RuntimeError):
    """Raised until a Hermes client (hop-client, #281) is wired in."""


def publish(submission):
    """Publish ``submission.payload`` to ``submission.service.hermes_topic``.

    Returns ``{"uuid": ...}`` once implemented; raises :class:`HermesNotConfigured` now.
    """
    raise HermesNotConfigured(
        "Hermes publishing is not implemented yet (#281); the submission is kept for a retry once it is."
    )
