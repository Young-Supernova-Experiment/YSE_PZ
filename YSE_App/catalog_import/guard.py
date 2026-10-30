"""Name the database before writing (#904).

The test stacks' settings once pointed at production (2026-10-22). The catalogue import commands
print the database they use, and with ``--apply`` refuse to run unless ``--database NAME`` names it.
"""

from __future__ import annotations

from django.core.management.base import CommandError
from django.db import connections


def database_name(alias: str = 'default') -> str:
    return str(connections[alias].settings_dict.get('NAME') or '')


def describe(alias: str = 'default') -> str:
    settings_dict = connections[alias].settings_dict
    return 'database %s on %s' % (settings_dict.get('NAME') or '?', settings_dict.get('HOST') or 'localhost')


def add_argument(parser) -> None:
    parser.add_argument('--database', default='', metavar='NAME',
                        help='With --apply: the name of the database to write to; must match the settings.')


def check(opts, write: bool, stdout=None) -> None:
    """Print the database; with ``write``, require ``--database`` to name it."""
    if stdout is not None:
        stdout.write('Using %s' % describe())
    if not write:
        return
    expected = (opts.get('database') or '').strip()
    actual = database_name()
    if not expected:
        raise CommandError('--apply writes to %s: repeat its name with --database %s to confirm.' % (describe(), actual))
    if expected != actual:
        raise CommandError('--database %s does not match the configured database %s; nothing written.'
                           % (expected, describe()))
