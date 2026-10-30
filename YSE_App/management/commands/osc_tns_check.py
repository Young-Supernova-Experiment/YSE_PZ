"""
TNS check of transients created by the OSC import (#889, #905); see YSE_App/catalog_import/osc_tns.py.

Preferred: match against TNS's bulk file of all public objects (one download, no request per object;
keep the file and reuse it for the production run)::

  python3 manage.py osc_tns_check --download-tns-file tns_public_objects.csv.zip
  python3 manage.py osc_tns_check --tns-file tns_public_objects.csv.zip --max 0              # dry run, all
  python3 manage.py osc_tns_check --tns-file tns_public_objects.csv.zip --max 0 --apply --database <DB_NAME>

Fallback, one object at a time through the API (one request a minute)::

  python3 manage.py osc_tns_check --max 20
  python3 manage.py osc_tns_check --queue --database <DB_NAME>   # background sweep job
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from YSE_App.catalog_import import guard, osc_tns, tns_public


class Command(BaseCommand):
    help = 'Look up transients created by the OSC import on TNS (dry run unless --apply).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write the TNS name, position and class.')
        parser.add_argument('--max', type=int, default=20,
                            help='At most this many entries (default 20; 0 = all pending).')
        parser.add_argument('--tns-file', default='', metavar='FILE',
                            help="TNS's public objects file (.csv or .csv.zip): match against it, no API requests.")
        parser.add_argument('--download-tns-file', default='', metavar='FILE',
                            help="Download TNS's public objects file to FILE (one request) and exit.")
        parser.add_argument('--interval', type=float, default=None,
                            help='API mode: seconds between TNS requests (default OSC_TNS_REQUEST_INTERVAL_SECONDS, 60).')
        parser.add_argument('--queue', action='store_true', help='Queue the background API sweep job and exit.')
        guard.add_argument(parser)

    def handle(self, *args, **opts):
        if opts['download_tns_file']:
            from YSE_App.services import tns_resync

            path = tns_public.download(opts['download_tns_file'], tns_resync.default_client())
            self.stdout.write('saved %s; %d objects' % (path, len(tns_public.PublicObjects.load(path))))
            return
        guard.check(opts, opts['apply'] or opts['queue'], self.stdout)
        if opts['queue']:
            queued = osc_tns.queue_sweep()
            self.stdout.write('queued job %s' % (queued.pk if queued else '(failed; see the log)'))
            return
        objects = None
        if opts['tns_file']:
            try:
                objects = tns_public.PublicObjects.load(opts['tns_file'])
            except (OSError, ValueError) as exc:
                raise CommandError('cannot read %s: %s' % (opts['tns_file'], exc))
            self.stdout.write('TNS file %s: %d objects' % (opts['tns_file'], len(objects)))
        pending = osc_tns.pending_entries().count()
        self.stdout.write('%d entries wait for a TNS check' % pending)
        counts = osc_tns.run(opts['max'] or None, dry_run=not opts['apply'], interval=opts['interval'],
                             log=self.stdout.write, objects=objects)
        self.stdout.write('%s: %d checked, %d TNS requests' % (
            'Applied' if opts['apply'] else 'Dry run', counts['checked'], 0 if objects is not None else counts['requests']))
