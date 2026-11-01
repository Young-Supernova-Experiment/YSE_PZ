"""Optional worker-start preload of the heavy plot stack (#489).

Importing bokeh / matplotlib / astropy / astroplan / sncosmo lazily costs several
seconds on the FIRST plot request of every Apache worker. Calling preload() once at
worker start moves that cost out of the request path. It changes no rendering.

Use from a mod_wsgi import script (see docs/ziggy-runbook.md, "Performance audit
2026-10-10: server settings"); it is never imported by the app itself.
"""
import logging

logger = logging.getLogger(__name__)


def preload():
    """Import the plot stack now; log and carry on if anything fails."""
    import django

    django.setup()
    from YSE_App import view_utils

    try:
        view_utils._load_heavy_plot_stack()
        logger.info('YSE plot stack preloaded')
    except Exception:  # never stop a worker from starting
        logger.exception('YSE plot stack preload failed; it will load on first use')
