"""Feed providers by ``FeedSource.kind`` (#280).

The providers register with the broker registry (``YSE_App.brokers.registry``)
so candidates work; this module only maps a source's ``kind`` to the provider
class and builds the instance with the source's credential and config.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Type

from YSE_App.brokers import registry as broker_registry
from YSE_App.feeds.base import FeedProvider

FEED_PROVIDER_MODULES = ("YSE_App.feeds.hermes", "YSE_App.feeds.einstein_probe", "YSE_App.feeds.scout")


def feed_classes() -> Dict[str, Type[FeedProvider]]:
    """``{feed_kind: provider class}`` for every registered feed provider."""
    broker_registry.autodiscover()
    out = {}
    for slug in broker_registry.registered_slugs():
        cls = broker_registry.provider_class(slug)
        if cls is not None and issubclass(cls, FeedProvider) and cls.feed_kind:
            out[cls.feed_kind] = cls
    return out


def provider_class_for_kind(kind: str) -> Optional[Type[FeedProvider]]:
    return feed_classes().get(kind)


def provider_for(source) -> Optional[FeedProvider]:
    cls = provider_class_for_kind(source.kind)
    return cls.for_source(source) if cls is not None else None


def describe_all() -> List[Dict]:
    return [cls().describe() for _kind, cls in sorted(feed_classes().items())]
