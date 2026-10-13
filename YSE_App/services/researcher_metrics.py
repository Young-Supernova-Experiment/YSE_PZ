"""Researcher publication metrics from SciX (formerly ADS), keyed by ORCID (#629).

API (https://github.com/adsabs/adsabs-dev-api): every request sends
``Authorization: Bearer <token>``.

1. ``GET {base}/search/query?q=orcid:<iD>&fl=bibcode,property,orcid_pub,orcid_user,orcid_other``
   (paged with ``start``/``rows``) lists the person's records. A record counts as
   first-author when the ORCID is in position 0 of one of the ORCID arrays,
   which run parallel to the author list.
2. ``POST {base}/metrics`` with ``{"bibcodes": [...], "types": ["basic", "citations", "indicators"]}``
   gives ``indicators.h``, ``citation stats["total number of citations"]`` and
   ``basic stats refereed["number of papers"]``.

The service-account token is an ``EncryptedCredential`` (service
``RESEARCHER_METRICS_SCIX_SERVICE``, default ``scix``; payload ``{"token": "..."}``).
Google Scholar is never scraped. Off unless ``RESEARCHER_METRICS_ENABLED``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable, Dict, List, Optional

import requests
from django.conf import settings
from django.utils import timezone

from YSE_App.models import EncryptedCredential, ResearcherMetrics, ResearcherProfile
from YSE_App.services.researcher_profile import orcid_checksum_ok

logger = logging.getLogger(__name__)

SEARCH_ROWS = 200
MAX_RECORDS = 4000
METRICS_BATCH = 2000


class MetricsError(Exception):
    pass


class RateLimited(MetricsError):
    pass


def enabled() -> bool:
    return bool(getattr(settings, "RESEARCHER_METRICS_ENABLED", False))


def scix_token() -> Optional[str]:
    service = getattr(settings, "RESEARCHER_METRICS_SCIX_SERVICE", "scix")
    cred = (EncryptedCredential.objects.filter(service=service, is_active=True)
            .order_by("-modified_date", "-id").first())
    if cred is None or not cred.has_secret:
        return None
    try:
        token = (cred.get_secret(touch=True) or {}).get("token")
    except Exception:  # noqa: BLE001 - undecryptable credential = not configured
        logger.warning("researcher metrics: the %s credential could not be decrypted", service)
        return None
    return (token or "").strip() or None


def _base_url() -> str:
    return getattr(settings, "RESEARCHER_METRICS_SCIX_API_URL", "https://api.adsabs.harvard.edu/v1").rstrip("/")


def _timeout() -> int:
    return int(getattr(settings, "RESEARCHER_METRICS_HTTP_TIMEOUT_SECONDS", 30))


def _check(resp) -> Dict:
    if resp.status_code == 429:
        raise RateLimited("SciX rate limit reached")
    if resp.status_code >= 400:
        raise MetricsError("SciX returned HTTP %s" % resp.status_code)
    try:
        return resp.json()
    except ValueError as exc:
        raise MetricsError("SciX returned invalid JSON") from exc


def fetch_scix(orcid: str, token: str, http=None) -> Dict:
    """Return the metric values and a small raw summary for one ORCID iD."""
    http = http or requests
    headers = {"Authorization": "Bearer %s" % token}
    docs: List[Dict] = []
    start = 0
    while start < MAX_RECORDS:
        resp = http.get(
            _base_url() + "/search/query", headers=headers, timeout=_timeout(),
            params={"q": "orcid:%s" % orcid, "fl": "bibcode,property,orcid_pub,orcid_user,orcid_other",
                    "rows": SEARCH_ROWS, "start": start},
        )
        payload = _check(resp).get("response", {})
        page = payload.get("docs", []) or []
        docs.extend(page)
        start += len(page)
        if not page or start >= int(payload.get("numFound", 0) or 0):
            break
    bibcodes = [d["bibcode"] for d in docs if d.get("bibcode")]
    first_author = sum(
        1 for d in docs
        if any((d.get(k) or [None])[0] == orcid for k in ("orcid_pub", "orcid_user", "orcid_other")))
    refereed_from_search = sum(1 for d in docs if "REFEREED" in (d.get("property") or []))
    result = {
        "h_index": None, "citation_count": None, "refereed_count": refereed_from_search,
        "first_author_count": first_author,
        "raw": {"records": len(bibcodes), "api": _base_url()},
    }
    if not bibcodes:
        result.update(h_index=0, citation_count=0)
        return result
    metrics = _check(http.post(
        _base_url() + "/metrics", headers=dict(headers, **{"Content-Type": "application/json"}),
        timeout=_timeout(),
        json={"bibcodes": bibcodes[:METRICS_BATCH], "types": ["basic", "citations", "indicators"]},
    ))
    indicators = metrics.get("indicators") or {}
    citations = metrics.get("citation stats") or {}
    refereed = metrics.get("basic stats refereed") or {}
    result["h_index"] = indicators.get("h")
    result["citation_count"] = citations.get("total number of citations")
    if refereed.get("number of papers") is not None:
        result["refereed_count"] = refereed.get("number of papers")
    result["raw"].update({
        "indicators": indicators,
        "citation stats": {k: v for k, v in citations.items() if not isinstance(v, list)},
        "basic stats refereed": refereed,
        "truncated": len(bibcodes) > METRICS_BATCH,
    })
    return result


@dataclass
class RefreshSummary:
    fetched: int = 0
    skipped: int = 0
    failed: int = 0
    stopped: str = ""
    errors: List[str] = field(default_factory=list)

    def __str__(self):
        text = "fetched %d, skipped %d, failed %d" % (self.fetched, self.skipped, self.failed)
        return text + (" (%s)" % self.stopped if self.stopped else "")


def refresh_all(force: bool = False, limit: Optional[int] = None, http=None,
                sleep: Callable[[float], None] = time.sleep, usernames=None) -> RefreshSummary:
    """Fetch SciX metrics for profiles with a valid ORCID, rate-limited."""
    summary = RefreshSummary()
    token = scix_token()
    if not token:
        summary.stopped = "no SciX token (EncryptedCredential service %r)" % getattr(
            settings, "RESEARCHER_METRICS_SCIX_SERVICE", "scix")
        return summary
    limit = limit if limit is not None else int(getattr(settings, "RESEARCHER_METRICS_MAX_PER_RUN", 200))
    interval = float(getattr(settings, "RESEARCHER_METRICS_REQUEST_INTERVAL_SECONDS", 2.0))
    min_age = timedelta(days=int(getattr(settings, "RESEARCHER_METRICS_MIN_AGE_DAYS", 25)))
    profiles = (ResearcherProfile.objects.exclude(orcid="").filter(user__is_active=True)
                .select_related("user").order_by("user_id"))
    if usernames:
        profiles = profiles.filter(user__username__in=list(usernames))
    first = True
    for profile in profiles:
        if summary.fetched + summary.failed >= limit:
            summary.stopped = "limit of %d per run reached" % limit
            break
        if not orcid_checksum_ok(profile.orcid):
            summary.skipped += 1
            continue
        if not force and ResearcherMetrics.objects.filter(
                user=profile.user, source=ResearcherMetrics.SOURCE_SCIX,
                fetched_at__gte=timezone.now() - min_age).exists():
            summary.skipped += 1
            continue
        if not first and interval > 0:
            sleep(interval)
        first = False
        try:
            values = fetch_scix(profile.orcid, token, http=http)
        except RateLimited as exc:
            summary.failed += 1
            summary.stopped = str(exc)
            break
        except (MetricsError, requests.RequestException) as exc:
            summary.failed += 1
            summary.errors.append("profile %s: %s" % (profile.pk, exc.__class__.__name__))
            continue
        ResearcherMetrics.objects.create(user=profile.user, source=ResearcherMetrics.SOURCE_SCIX, **values)
        summary.fetched += 1
    return summary
