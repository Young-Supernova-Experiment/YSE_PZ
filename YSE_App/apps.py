from django.apps import AppConfig


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
    if hasattr(iers.conf, "iers_degraded_accuracy"):
        iers.conf.iers_degraded_accuracy = "ignore"


class YseAppConfig(AppConfig):
    name = 'YSE_App'

    def ready(self):
        import YSE_App.signals  # noqa: F401
        astropy_offline()
