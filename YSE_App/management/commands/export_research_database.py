"""Write the anonymized research database (#746) to a new SQLite file.

Server only (a management command; nothing is reachable over the web). The output holds no
usernames, names, emails, ORCID iDs or credentials, and every identifier is a random string drawn
fresh for this export. Keep the file off shared drives: it is research data under the consent
rules of #744. Rules and tables: ``YSE_App/services/research_export.py``.

    manage.py export_research_database --out /secure/path/research-2026-10-21.sqlite
"""

from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError

from YSE_App.services import research_export


class Command(BaseCommand):
    help = "Write the anonymized research database (SQLite) under the #744 consent rules."

    def add_arguments(self, parser):
        parser.add_argument("--out", required=True, help="Path of the new SQLite file.")
        parser.add_argument("--force", action="store_true", help="Overwrite an existing file.")

    def handle(self, *args, **options):
        path = options["out"]
        if os.path.exists(path):
            if not options["force"]:
                raise CommandError("%s exists; pass --force to overwrite it" % path)
            os.remove(path)
        # Create the file readable by its owner only.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        counts = research_export.export(path)
        for name, n in counts.items():
            self.stdout.write("%-24s %d rows" % (name, n))
        self.stdout.write("Wrote %s" % path)
