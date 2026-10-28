"""
Telescopes and instruments of catalogue data (#894).

``--load``: create the observatories, telescopes, instruments and alias rows listed in
``YSE_App/catalog_import/osc_instruments.json`` (dry run unless ``--apply``).
``--census PATH...``: count the telescope / instrument strings in OSC entry files and show what
each resolves to, most frequent first, so the mapping file can be extended.

  python3 manage.py catalog_instruments --load
  python3 manage.py catalog_instruments --load --apply --user <USERNAME>
  python3 manage.py catalog_instruments --census <OSC_DIR>/sne-2010-2014 --top 100
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from YSE_App.catalog_import import instruments, osc, osc_fetch


class Command(BaseCommand):
    help = 'Create catalogue instruments from the mapping file, or count the instrument strings in OSC files.'

    def add_arguments(self, parser):
        parser.add_argument('--load', action='store_true', help='Load osc_instruments.json (dry run unless --apply).')
        parser.add_argument('--mapping', default=instruments.MAPPING_PATH)
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--user', default='', help='Username recorded as creator (needed with --apply).')
        parser.add_argument('--census', nargs='+', default=[], metavar='PATH', help='OSC entry files or directories.')
        parser.add_argument('--top', type=int, default=200, help='Census: show this many strings.')

    def handle(self, *args, **opts):
        if not opts['load'] and not opts['census']:
            raise CommandError('Give --load or --census PATH.')
        if opts['load']:
            user = get_user_model().objects.filter(username=opts['user']).first() if opts['user'] else None
            if opts['apply'] and user is None:
                raise CommandError('--apply needs --user <existing username>.')
            for line in instruments.ensure_instruments(user, instruments.load_mapping(opts['mapping']),
                                                       apply=opts['apply']):
                self.stdout.write(line)
        if opts['census']:
            def entries():
                for path in osc_fetch.entry_files(opts['census']):
                    try:
                        yield from osc.load_entries(path)
                    except (OSError, ValueError) as exc:
                        self.stderr.write('%s: unreadable: %s' % (path, exc))

            resolver = instruments.InstrumentResolver()
            counts = instruments.census(entries())
            for (kind, telescope, instrument), n in counts.most_common(opts['top']):
                try:
                    inst = resolver.resolve(telescope, instrument)
                    target = 'Unknown' if resolver.is_unknown(inst) else '%s / %s' % (inst.telescope.name, inst.name)
                except instruments.NoUnknownInstrument:
                    target = '-'
                self.stdout.write('%8d  %-10s %-30r %-30r -> %s' % (n, kind, telescope, instrument, target))
