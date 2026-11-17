"""
Telescopes and instruments of catalogue data (#894, #1003).

``--census PATH...`` counts the telescope / instrument strings in OSC entry files and shows what each
resolves to: an existing YSE instrument, a new one (by a rule of ``catalog_import/osc_instruments.json``
or automatically, Telescope/Instrument from the strings), or Unknown. Reads the database only.

``--load`` (with ``--census``) creates the new telescopes and instruments and one alias row per
string: a dry run unless ``--apply``.

  python3 manage.py catalog_instruments --census <OSC_DIR>/sne-* --top 300
  python3 manage.py catalog_instruments --census <OSC_DIR>/sne-* --load --apply --user <USERNAME> --database <DB_NAME>
"""

from __future__ import annotations

from collections import Counter

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from YSE_App.catalog_import import guard, instruments, osc, osc_fetch


class Command(BaseCommand):
    help = 'Plan (and with --load --apply create) the YSE instruments of every catalogue telescope/instrument string.'

    def add_arguments(self, parser):
        parser.add_argument('--census', nargs='+', default=[], metavar='PATH', help='OSC entry files or directories.')
        parser.add_argument('--load', action='store_true',
                            help='Create the planned telescopes, instruments and alias rows (dry run unless --apply).')
        parser.add_argument('--mapping', default=instruments.MAPPING_PATH, help='The rules file.')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--user', default='', help='Username recorded as creator (needed with --apply).')
        parser.add_argument('--top', type=int, default=200, help='Show this many strings (0: all).')
        guard.add_argument(parser)

    def handle(self, *args, **opts):
        if not opts['census']:
            raise CommandError('Give --census PATH... (the OSC entry files or directories).')
        write = opts['load'] and opts['apply']
        guard.check(opts, write, self.stdout)
        user = None
        if write:
            user = get_user_model().objects.filter(username=opts['user']).first() if opts['user'] else None
            if user is None:
                raise CommandError('--apply needs --user <existing username>.')

        def entries():
            for path in osc_fetch.entry_files(opts['census']):
                try:
                    yield from osc.load_entries(path)
                except (OSError, ValueError) as exc:
                    self.stderr.write('%s: unreadable: %s' % (path, exc))

        rows = instruments.plan(instruments.census(entries()), instruments.Rules.load(opts['mapping']))
        for row in rows[:opts['top'] or None]:
            self.stdout.write(instruments.describe(row))
        totals = Counter()
        for row in rows:
            totals[row.action] += 1
            totals[row.action + ' rows'] += row.rows
        new = {(r.target.telescope.lower(), r.target.instrument.lower()) for r in rows if r.action == 'create'}
        self.stdout.write('%d strings: %d to existing instruments (%d rows), %d to %d new instruments (%d rows), '
                          '%d Unknown (%d rows)' % (
                              len(rows), totals['use'], totals['use rows'], totals['create'], len(new),
                              totals['create rows'], totals['unknown'], totals['unknown rows']))
        if not opts['load']:
            return
        if not write:
            self.stdout.write('Dry run: --load --apply creates them.')
            return
        with transaction.atomic():
            done = instruments.apply_plan(rows, user)
        self.stdout.write('Created: %s' % ', '.join('%s %d' % kv for kv in sorted(done.items())))
