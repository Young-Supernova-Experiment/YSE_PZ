"""One scheduled SALT / Bazin refit pass by hand (#846).

Lists (dry run, default) or queues (``--apply``) refits of transients whose newest Bazin or
SALT fit is out of date: a new detection since that run, or a non-detection within
``FIT_REFIT_WINDOW_DAYS`` (7) after the last detection. The runs go on the job queue as the
previous run's user. ``--apply`` needs ``FIT_REFIT_ENABLED``; the ``FitRefit`` cron does the
same pass daily when it is on::

  python manage.py refit_fits                 # what would be refitted
  python manage.py refit_fits --apply         # queue the runs
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from YSE_App.services import fit_tables


class Command(BaseCommand):
    help = "Refit out-of-date Bazin / SALT fits on the job queue (dry run unless --apply; needs FIT_REFIT_ENABLED)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Queue the refit runs (default: only list them).")
        parser.add_argument("--limit", type=int, default=None, help="At most this many refits (default FIT_REFIT_LIMIT).")

    def handle(self, *args, **options):
        apply = options["apply"]
        if apply and not fit_tables.refit_enabled():
            raise CommandError("FIT_REFIT_ENABLED is off; set [site_settings] FIT_REFIT_ENABLED = True first.")
        if not apply:
            candidates = fit_tables.refit_candidates(options["limit"])
            for c in candidates:
                self.stdout.write("transient %s, %s: %s" % (c.transient_id, c.profile.service.slug, c.reason))
            self.stdout.write("dry run: %d refit(s) due. Run again with --apply to queue them." % len(candidates))
            return
        summary = fit_tables.queue_refits(apply=True, limit=options["limit"])
        self.stdout.write("%(candidates)d refit(s) due: %(queued)d queued, %(skipped)d skipped" % summary)
