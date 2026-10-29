"""
TNS check of transients created by the OSC import (#889); see YSE_App/catalog_import/osc_tns.py.

Dry run by default (still queries TNS, throttled, but writes nothing):

  python3 manage.py osc_tns_check --max 20
  python3 manage.py osc_tns_check --max 200 --apply
  python3 manage.py osc_tns_check --queue      # one background sweep job instead
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from YSE_App.catalog_import import osc_tns


class Command(BaseCommand):
    help = 'Look up transients created by the OSC import on TNS (dry run unless --apply).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write the TNS name, position and class.')
        parser.add_argument('--max', type=int, default=20, help='At most this many entries (default 20).')
        parser.add_argument('--interval', type=float, default=None,
                            help='Seconds between TNS requests (default OSC_TNS_REQUEST_INTERVAL_SECONDS, 60).')
        parser.add_argument('--queue', action='store_true', help='Queue the background sweep job and exit.')

    def handle(self, *args, **opts):
        if opts['queue']:
            queued = osc_tns.queue_sweep()
            self.stdout.write('queued job %s' % (queued.pk if queued else '(failed; see the log)'))
            return
        pending = osc_tns.pending_entries().count()
        self.stdout.write('%d entries wait for a TNS check' % pending)
        counts = osc_tns.run(opts['max'], dry_run=not opts['apply'], interval=opts['interval'], log=self.stdout.write)
        self.stdout.write('%s: %d checked, %d TNS requests' % (
            'Applied' if opts['apply'] else 'Dry run', counts['checked'], counts['requests']))
