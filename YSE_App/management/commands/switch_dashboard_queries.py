"""
Point dashboard sections at viewer-scoped saved Python queries instead of saved SQL (#720).

Since #716 a saved SQL query that reads photometry, spectra or their statistics
is refused for anyone who cannot see all data. The same selections exist as
Python queries (``YSE_App/queries/yse_python_queries.py``) that select only from
the viewer's visible data. A dashboard section is a ``UserQuery`` row; this
command repoints rows from an Explorer query (``query``) to a Python query
(``python_query``). The SQL stays in Explorer. Dry run is the default::

  python manage.py switch_dashboard_queries                      # report only
  python manage.py switch_dashboard_queries --apply              # write, with backup
  python manage.py switch_dashboard_queries --map "Auto Ignore=my_python_query" --apply
  python manage.py switch_dashboard_queries --mapping-file map.json --apply
  python manage.py switch_dashboard_queries --revert <backup.json> --apply

Built-in mapping (by saved-query title, case-insensitive):
Magnitude-Limited Sample, Fast & Young Target Search, New Transients Last Two
Days and Interesting New Targets. Any other query is mapped with ``--map
KEY=PYTHON_NAME`` (KEY is a query id or title; repeatable) or a JSON file of
``{"<id or title>": "<python name>"}``. Sections whose SQL is still refused for
viewers who do not see all data and that have no mapping are listed as
UNMAPPED so someone with the production SQL can write the Python query.

``--apply`` writes a JSON backup (the previous query of every changed row) and
prints its path; ``--revert`` restores it. Run ``dedupe_dashboard_queries``
afterwards if a user already had the Python query on their dashboard.
"""

from __future__ import annotations

import datetime
import json
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

#: saved-query title (lower case) -> registered Python query
DEFAULT_MAPPING = {
    "yse magnitude-limited sample (min mag < 18.6)": "yse_magnitude_limited_sample",
    "fast & young target search": "fast_and_young_targets",
    "new transients last two days": "new_transients_last_two_days",
    "interesting new targets": "interesting_new_targets",
}


def parse_map_arg(text):
    key, sep, name = (text or "").rpartition("=")
    if not sep or not key.strip() or not name.strip():
        raise CommandError(f"--map expects KEY=PYTHON_NAME, got {text!r}")
    return key.strip(), name.strip()


def resolve_queries(key):
    """Explorer ``Query`` rows for a mapping key: an id, else a title (case-insensitive)."""
    from explorer.models import Query

    key = str(key).strip()
    if key.isdigit():
        return list(Query.objects.filter(pk=int(key)))
    return list(Query.objects.filter(title__iexact=key).order_by("id"))


class Command(BaseCommand):
    help = "Repoint dashboard sections from saved SQL queries to viewer-scoped Python queries (dry run by default)."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true", default=True, help="Report only (default).")
        mode.add_argument("--apply", action="store_true", help="Write the changes after saving a backup.")
        parser.add_argument("--map", action="append", default=[], metavar="KEY=PYTHON_NAME",
                            help="Map a saved query (id or title) to a Python query. Repeatable.")
        parser.add_argument("--mapping-file", default=None, help="JSON file of {id or title: python name}.")
        parser.add_argument("--no-defaults", action="store_true", help="Ignore the built-in mapping.")
        parser.add_argument("--user", default=None, help="Only this username's dashboard.")
        parser.add_argument("--backup-dir", default=None,
                            help="Backup directory (default: settings.DASHBOARD_QUERY_BACKUP_DIR).")
        parser.add_argument("--revert", metavar="BACKUP_JSON", default=None,
                            help="Restore the sections recorded in this backup file.")

    # ----------------------------------------------------------------- mapping

    def _mapping(self, options):
        from YSE_App.queries.yse_python_queries import python_query_reg

        mapping = {} if options["no_defaults"] else dict(DEFAULT_MAPPING)
        if options["mapping_file"]:
            try:
                with open(options["mapping_file"], encoding="utf-8") as fh:
                    loaded = json.load(fh)
            except (OSError, ValueError) as exc:
                raise CommandError(f"cannot read mapping file: {exc}")
            if not isinstance(loaded, dict):
                raise CommandError("mapping file must be a JSON object of {id or title: python name}")
            mapping.update({str(k).strip().lower(): str(v).strip() for k, v in loaded.items()})
        for text in options["map"]:
            key, name = parse_map_arg(text)
            mapping[key.lower()] = name
        unknown = sorted({n for n in mapping.values() if n not in python_query_reg.all})
        if unknown:
            raise CommandError("not registered Python queries: " + ", ".join(unknown))
        return mapping

    def _python_name_for(self, mapping, query):
        return mapping.get(str(query.pk)) or mapping.get((query.title or "").strip().lower())

    # ---------------------------------------------------------------- planning

    def _sections(self, options):
        from YSE_App.models import UserQuery

        rows = UserQuery.objects.filter(query__isnull=False).select_related("user", "query").order_by("id")
        if options["user"]:
            rows = rows.filter(user__username=options["user"])
        return list(rows)

    def _plan(self, options):
        from YSE_App.services.page_query_scope import disallowed_columns, disallowed_tables

        mapping = self._mapping(options)
        changes, unmapped = [], {}
        for row in self._sections(options):
            name = self._python_name_for(mapping, row.query)
            if name:
                changes.append((row, name))
                continue
            bad = disallowed_tables(row.query.sql) + disallowed_columns(row.query.sql)
            if bad:
                entry = unmapped.setdefault(row.query_id, [row.query.title, bad, 0])
                entry[2] += 1
        # A mapping key that matches no saved query is worth saying out loud.
        missing = [k for k in mapping if not resolve_queries(k)]
        return changes, unmapped, missing

    def _plan_revert(self, options):
        from YSE_App.models import UserQuery

        try:
            with open(options["revert"], encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as exc:
            raise CommandError(f"cannot read backup: {exc}")
        out = []
        for item in payload.get("sections", []):
            row = UserQuery.objects.filter(pk=item["id"]).first()
            if row is None:
                self.stdout.write(f"SKIP       section {item['id']}: no longer exists")
                continue
            out.append((row, item["previous_query_id"]))
        return out

    # ------------------------------------------------------------------ output

    def _backup_dir(self, options):
        return options["backup_dir"] or getattr(
            settings, "DASHBOARD_QUERY_BACKUP_DIR",
            os.path.join(getattr(settings, "BASE_DIR", os.getcwd()), "backups", "saved_queries"),
        )

    def _write_backup(self, options, changes):
        directory = self._backup_dir(options)
        os.makedirs(directory, exist_ok=True)
        stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        path = os.path.join(directory, f"dashboard_sections_switch_{stamp}.json")
        payload = {
            "created_utc": datetime.datetime.utcnow().isoformat() + "Z",
            "command": "manage.py switch_dashboard_queries",
            "sections": [
                {"id": row.id, "previous_query_id": row.query_id, "previous_python_query": row.python_query,
                 "python_query": name}
                for row, name in changes
            ],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)
        return path

    def handle(self, *args, **options):
        from YSE_App.models import UserQuery

        apply = options["apply"]
        if options["revert"]:
            restore = self._plan_revert(options)
            self.stdout.write(f"{len(restore)} section(s) to restore")
            if not apply:
                self.stdout.write(self.style.WARNING("Dry run: no changes made. Re-run with --apply to write."))
                return
            with transaction.atomic():
                for row, query_id in restore:
                    UserQuery.objects.filter(pk=row.pk).update(query_id=query_id, python_query=None)
            self.stdout.write(self.style.SUCCESS(f"Restored {len(restore)} section(s)."))
            return

        changes, unmapped, missing = self._plan(options)
        for row, name in changes:
            self.stdout.write(f"SWITCH     section {row.id} ({row.user.username}): "
                              f"SQL {row.query_id} {row.query.title!r} -> python {name!r}")
        for key in missing:
            self.stdout.write(self.style.WARNING(f"NOT FOUND  no saved query matches mapping key {key!r}"))
        for query_id, (title, bad, count) in sorted(unmapped.items()):
            self.stdout.write(self.style.WARNING(
                f"UNMAPPED   SQL {query_id} {title!r} ({count} section(s)) reads {', '.join(sorted(set(bad)))}; "
                "needs a Python query and a --map entry"))
        self.stdout.write(f"{len(changes)} section(s) to switch; {len(unmapped)} unmapped saved quer"
                          f"{'y' if len(unmapped) == 1 else 'ies'} still refused for viewers who do not see all data.")
        if not changes:
            return
        if not apply:
            self.stdout.write(self.style.WARNING("Dry run: no changes made. Re-run with --apply to write."))
            return
        path = self._write_backup(options, changes)
        self.stdout.write(f"Previous sections saved to {path}")
        with transaction.atomic():
            for row, name in changes:
                UserQuery.objects.filter(pk=row.pk).update(query=None, python_query=name)
        self.stdout.write(self.style.SUCCESS(f"Switched {len(changes)} section(s). The SQL queries stay in Explorer."))
        self.stdout.write(f"Revert with: manage.py switch_dashboard_queries --revert {path} --apply")
