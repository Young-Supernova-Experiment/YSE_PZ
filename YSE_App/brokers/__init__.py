"""Broker provider plugins (issue #272) and the polling ingest to candidates (#276).

::

    from YSE_App.brokers import get_provider, enabled_providers
    fink = get_provider("fink")
    fink.capabilities_list()      # ['query_alerts', 'get_alert', 'cutouts', ...]
    fink.cone_search(ra, dec, 5)  # [BrokerAlert, ...]

See docs/brokers.md.
"""

from YSE_App.brokers.base import (  # noqa: F401
    ALL_CAPABILITIES,
    CAPABILITY_LABELS,
    STREAM,
    BrokerAlert,
    BrokerError,
    BrokerProvider,
    BrokerUnavailable,
    CapabilityNotSupported,
)
from YSE_App.brokers.registry import (  # noqa: F401
    all_providers,
    describe_all,
    enabled_providers,
    enabled_slugs,
    get_provider,
    register,
    registered_slugs,
)

__all__ = [
    "ALL_CAPABILITIES", "CAPABILITY_LABELS", "STREAM", "BrokerAlert", "BrokerError", "BrokerProvider",
    "BrokerUnavailable", "CapabilityNotSupported", "all_providers", "describe_all", "enabled_providers",
    "enabled_slugs", "get_provider", "register", "registered_slugs",
]
