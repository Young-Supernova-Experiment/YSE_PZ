"""Small ``requests`` wrapper shared by the REST providers (Fink, ALeRCE)."""

from __future__ import annotations

from typing import Any, Dict, Optional

import requests
from django.conf import settings

from YSE_App.brokers.base import BrokerError

USER_AGENT = "YSE-PZ broker client (+https://github.com/Young-Supernova-Experiment/YSE_PZ)"


def timeout_seconds() -> float:
    return float(getattr(settings, "BROKER_HTTP_TIMEOUT_SECONDS", 30) or 30)


def request_json(method: str, url: str, *, params: Optional[Dict] = None, json: Any = None,
                 headers: Optional[Dict] = None, timeout: Optional[float] = None) -> Any:
    """Perform the request and return the decoded JSON body; raise :class:`BrokerError`."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    hdrs.update(headers or {})
    try:
        response = requests.request(
            method, url, params=params, json=json, headers=hdrs, timeout=timeout or timeout_seconds()
        )
    except requests.RequestException as exc:
        raise BrokerError("%s %s failed: %s" % (method, url, exc)) from exc
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise BrokerError("%s %s returned HTTP %s: %s" % (method, url, response.status_code, response.text[:200]))
    try:
        return response.json()
    except ValueError as exc:
        raise BrokerError("%s %s returned non-JSON body" % (method, url)) from exc
