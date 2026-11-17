import warnings

from django.apps import AppConfig


def _no_iers_range_check(self, *args, **kwargs):
    """Stand-in for astropy's IERS range check on versions without ``iers_degraded_accuracy``."""
    return None


def astropy_offline():
    """Never download astropy's IERS (Earth-rotation, leap-second) tables in this process.

    A page that computes twilight or airmass for a date past the bundled table
    otherwise waits on the download (504s on every observing night with astropy
    5.2 on a host whose outbound HTTPS stalls). UT1 refinement is milliseconds,
    which does not matter here. astropy >= 5.1 also raises outside the bundled
    table unless told otherwise; ``iers_quiet`` in services/observability.py does
    the same for one block.
    """
    try:
        from astropy.utils import iers
    except ImportError:
        return
    iers.conf.auto_download = False
    # Never treat the bundled table as stale (no refresh attempts).
    iers.conf.auto_max_age = None
    if hasattr(iers.conf, "iers_degraded_accuracy"):
        iers.conf.iers_degraded_accuracy = "ignore"
    else:
        # astropy 5.0 (Ziggy's venv) has no switch: dates past the bundled table
        # (any future observing night) raise IERSRangeError converting UTC->UT1,
        # a 500 on the observing-night page. Drop the range check; astropy has
        # already clipped the indices, so such times get the last tabulated value.
        for name in ("IERS", "IERS_A", "IERS_B", "IERS_Auto"):
            cls = getattr(iers, name, None)
            if cls is not None and "_check_interpolate_indices" in vars(cls):
                cls._check_interpolate_indices = _no_iers_range_check
    iers_warning = getattr(iers, "IERSWarning", None)
    if iers_warning is not None:
        warnings.filterwarnings("ignore", category=iers_warning)


class YseAppConfig(AppConfig):
    name = 'YSE_App'

    def ready(self):
        import YSE_App.signals  # noqa: F401
        from YSE_App.explorer_scope_hooks import install as install_explorer_scope

        install_explorer_scope()  # viewer-scoped SQL Explorer queries (#723)
        astropy_offline()
