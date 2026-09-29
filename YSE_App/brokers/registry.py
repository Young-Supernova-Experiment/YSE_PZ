"""Provider registry keyed by broker slug (issue #273).

Providers register at import time with :func:`register`; ``settings.BROKERS_ENABLED``
(``[brokers] enabled`` in settings.ini, default: every shipped provider) decides
which of them the UI, the API and the ingest job offer. Credentials come from an
active ``EncryptedCredential`` with ``service=<slug>`` (#264) when one exists;
providers that need none (Fink, ALeRCE, ANTARES public search) work without.
"""

from __future__ import annotations

import importlib
import logging
import threading
from typing import Dict, List, Optional, Type

from django.conf import settings

from YSE_App.brokers.base import BrokerProvider, BrokerUnavailable

logger = logging.getLogger(__name__)

# Modules whose import registers the providers the application ships.
DEFAULT_PROVIDER_MODULES = (
    "YSE_App.brokers.antares",
    "YSE_App.brokers.fink",
    "YSE_App.brokers.alerce",
)

_registry: Dict[str, Type[BrokerProvider]] = {}
_lock = threading.Lock()
_discovered = False


def register(cls: Type[BrokerProvider]) -> Type[BrokerProvider]:
    """Class decorator: make ``cls`` the provider for ``cls.slug``."""
    if not cls.slug:
        raise ValueError("provider %r has no slug" % cls)
    with _lock:
        _registry[cls.slug] = cls
    return cls


def unregister(slug: str) -> None:
    with _lock:
        _registry.pop(slug, None)


def autodiscover(force: bool = False) -> None:
    global _discovered
    if _discovered and not force:
        return
    _discovered = True
    modules = list(DEFAULT_PROVIDER_MODULES)
    for name in getattr(settings, "BROKER_PROVIDER_MODULES", None) or ():
        if name and name not in modules:
            modules.append(name)
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 - one broken provider must not hide the others
            logger.exception("broker provider module %s failed to import", name)


def registered_slugs() -> List[str]:
    autodiscover()
    return sorted(_registry)


def provider_class(slug: str) -> Optional[Type[BrokerProvider]]:
    autodiscover()
    return _registry.get(slug)


def enabled_slugs() -> List[str]:
    """Slugs switched on in settings (``BROKERS_ENABLED``; None/empty = all registered)."""
    configured = getattr(settings, "BROKERS_ENABLED", None)
    slugs = registered_slugs()
    if not configured:
        return slugs
    wanted = [s.strip() for s in (configured.split(",") if isinstance(configured, str) else configured) if s and s.strip()]
    return [s for s in slugs if s in wanted]


def credential_for(slug: str) -> Dict:
    """Decrypted payload of the active broker credential for ``slug`` (``{}`` when none)."""
    try:
        from YSE_App.models.credential_models import EncryptedCredential

        row = (
            EncryptedCredential.objects.filter(service=slug, is_active=True)
            .filter(kind__in=(EncryptedCredential.KIND_BROKER, EncryptedCredential.KIND_GENERIC))
            .order_by("-kind", "pk")
            .first()
        )
        if row is None or not row.has_secret:
            return {}
        return row.get_secret(touch=True) or {}
    except Exception as exc:  # noqa: BLE001 - table missing (migrations), key missing, ...
        logger.warning("broker credential for %s unavailable: %s", slug, exc)
        return {}


def broker_options(slug: str) -> Dict:
    opts = getattr(settings, "BROKER_OPTIONS", None) or {}
    return dict(opts.get(slug) or {})


def get_provider(slug: str, *, require_available: bool = False) -> Optional[BrokerProvider]:
    """Instantiate the provider for ``slug`` (None when unknown or disabled).

    With ``require_available`` a provider that cannot run here raises
    :class:`BrokerUnavailable` with the reason instead of being returned.
    """
    cls = provider_class(slug)
    if cls is None or slug not in enabled_slugs():
        return None
    provider = cls(credential=credential_for(slug), options=broker_options(slug))
    if require_available and not provider.available():
        raise BrokerUnavailable("%s: %s" % (slug, provider.unavailable_reason()))
    return provider


def all_providers() -> List[BrokerProvider]:
    """Every enabled provider, available or not (the UI greys out the latter)."""
    out = []
    for slug in enabled_slugs():
        provider = get_provider(slug)
        if provider is not None:
            out.append(provider)
    return out


def enabled_providers() -> List[BrokerProvider]:
    """Enabled providers that can run in this process."""
    return [p for p in all_providers() if p.available()]


def describe_all() -> List[Dict]:
    return [p.describe() for p in all_providers()]
