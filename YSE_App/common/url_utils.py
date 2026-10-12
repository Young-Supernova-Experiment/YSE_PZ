"""URL helpers for the https follow-up (#441).

``https_url`` upgrades ``http://`` links to hosts that are known to serve https;
``public_base_url`` is the one place that builds the absolute base of our own
links (email, Slack, notifications). Neither makes a network request.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from django.conf import settings

# Our own hosts. A subdomain of one of these counts as ours too.
OWN_HOSTS = frozenset({
    "ziggy.ucolick.org",
})

# Third-party services that answer on https. Only exact host names (no
# wildcard): a host not listed here is never rewritten.
HTTPS_SERVICE_HOSTS = frozenset({
    "ps1images.stsci.edu",
    "pstamp.ipp.ifa.hawaii.edu",
    "mast.stsci.edu",
    "archive.stsci.edu",
    "www.wis-tns.org",
    "wis-tns.org",
    "sandbox.wis-tns.org",
    "api.antares.noirlab.edu",
    "antares.noirlab.edu",
    "ned.ipac.caltech.edu",
    "irsa.ipac.caltech.edu",
    "simbad.cds.unistra.fr",
    "simbad.u-strasbg.fr",
    "vizier.cds.unistra.fr",
    "arxiv.org",
    "doi.org",
    "dx.doi.org",
    "ui.adsabs.harvard.edu",
    "www.astronomerstelegram.org",
    "gcn.gsfc.nasa.gov",
    "gcn.nasa.gov",
    "github.com",
    "api.open-meteo.com",
    "api.openweathermap.org",
})

# Hosts that are known to refuse https (or present a certificate for another
# name). Never rewritten; recheck them from time to time (see the runbook).
HTTP_ONLY_HOSTS = frozenset({
    "skyvision.caltech.edu",
    "schedule.ztf.uw.edu",
    "datastore.ipp.ifa.hawaii.edu",
})

_LOCAL_DEFAULT_BASE_URL = "http://127.0.0.1:8000/"
_PRODUCTION_DEFAULT_BASE_URL = "https://ziggy.ucolick.org/yse/"


def url_host(url: str) -> Optional[str]:
    """Lower-case host of an absolute ``http(s)`` URL, or None."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    return (parts.hostname or "").lower() or None


def is_https_capable_host(host: Optional[str]) -> bool:
    """True for our own hosts and the known https-capable services."""
    if not host or host in HTTP_ONLY_HOSTS:
        return False
    if host in HTTPS_SERVICE_HOSTS:
        return True
    return any(host == own or host.endswith("." + own) for own in OWN_HOSTS)


def https_url(url: str) -> str:
    """Return ``url`` with ``http://`` changed to ``https://`` when the host is
    ours or a known https-capable service; anything else is returned unchanged.

    An explicit port is left alone (``http://host:8080/`` is not the https
    service), and only the scheme is touched, so placeholders such as
    ``{lat}`` and the rest of the text survive byte for byte.
    """
    if not isinstance(url, str):
        return url
    stripped = url.lstrip()
    if stripped[:7].lower() != "http://":
        return url
    try:
        port = urlsplit(stripped).port
    except ValueError:
        return url
    if port is not None or not is_https_capable_host(url_host(stripped)):
        return url
    lead = url[: len(url) - len(stripped)]
    return lead + "https://" + stripped[7:]


def public_base_url() -> str:
    """Absolute base URL of this site, with a trailing slash.

    ``settings.YSE_PUBLIC_BASE_URL`` first; with DEBUG a local address;
    otherwise the production https address.
    """
    base = getattr(settings, "YSE_PUBLIC_BASE_URL", None)
    if base:
        return base.rstrip("/") + "/"
    if settings.DEBUG:
        return _LOCAL_DEFAULT_BASE_URL
    return _PRODUCTION_DEFAULT_BASE_URL
