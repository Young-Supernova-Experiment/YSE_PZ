"""Registry of facility adapters keyed by slug (#299)."""

from __future__ import annotations

import importlib
import logging
import threading
from typing import Dict, List, Optional, Tuple, Type

from django.conf import settings

from YSE_App.facilities.base import FacilityAPI

logger = logging.getLogger(__name__)

DEFAULT_MODULES = ("YSE_App.facilities.generic", "YSE_App.facilities.lco")

_registry: Dict[str, FacilityAPI] = {}
_lock = threading.Lock()
_discovered = False


def register(cls: Type[FacilityAPI]) -> Type[FacilityAPI]:
    """Class decorator: make ``cls`` the adapter for ``cls.slug``."""
    if not getattr(cls, "slug", ""):
        raise ValueError("facility adapter %s needs a slug" % cls.__name__)
    with _lock:
        _registry[cls.slug] = cls()
    return cls


def unregister(slug: str) -> None:
    with _lock:
        _registry.pop(slug, None)


def facility_modules() -> List[str]:
    modules = list(DEFAULT_MODULES)
    for name in getattr(settings, "FACILITY_API_MODULES", None) or ():
        if name and name not in modules:
            modules.append(name)
    return modules


def autodiscover(force: bool = False) -> None:
    global _discovered
    if _discovered and not force:
        return
    _discovered = True
    for name in facility_modules():
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 - one broken adapter must not hide the others
            logger.exception("facility module %s failed to import", name)


def get_facility(slug: str) -> Optional[FacilityAPI]:
    if not slug:
        return None
    adapter = _registry.get(slug)
    if adapter is None and not _discovered:
        autodiscover()
        adapter = _registry.get(slug)
    return adapter


def registered_slugs() -> List[str]:
    autodiscover()
    return sorted(_registry)


def facility_choices(blank_label: str = "(manual, no facility API)") -> List[Tuple[str, str]]:
    autodiscover()
    choices = [("", blank_label)]
    for slug in sorted(_registry):
        choices.append((slug, _registry[slug].name or slug))
    return choices
