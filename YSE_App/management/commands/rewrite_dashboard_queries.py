"""
Replace slow personal-dashboard saved queries with result-identical rewrites.

The rewrites live in ``YSE_App/queries/dashboard_saved_queries.py`` together
with the exact original text each one is proven against. A saved query
(``explorer_query`` row, matched by title) is changed only when its current
SQL is one of those originals; a query whose text differs is printed with a
diff against the expected original and left alone. Dry run is the default::

  python manage.py rewrite_dashboard_queries --dry-run          # show diffs
  python manage.py rewrite_dashboard_queries --apply            # write, with backup
  python manage.py rewrite_dashboard_queries --apply --title "Fast & Young Target Search"
  python manage.py rewrite_dashboard_queries --revert <backup.json> --apply

``--apply`` first writes the previous SQL of every query it changes to a
timestamped JSON file under ``--backup-dir`` (default
``[site_settings] DASHBOARD_QUERY_BACKUP_DIR``) and prints the path;
``--revert`` restores the text recorded in such a file. Both are idempotent:
a query that already carries the target text is reported and skipped.
"""

from __future__ import annotations

import datetime
import difflib
import json
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from YSE_App.queries.dashboard_saved_queries import (
    REVIEW_TITLES,
    REWRITES,
    classify_saved_sql,
    normalize_sql,
    rewrite_for,
)


def _diff(old, new, label):
    return "\n".join(
        difflib.unified_diff(
            (old or "").splitlines(), (new or "").splitlines(),
            fromfile=f"{label} (current)", tofile=f"{label} (rewrite)", lineterm="",
        )
    )


class Command(BaseCommand):
    help = "Rewrite slow dashboard saved queries (explorer_query rows) to faster, result-identical SQL."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true", default=True,
                          help="Report what would change (default).")
        mode.add_argument("--apply", action="store_true",
                          help="Write the changes; the previous SQL is saved to a backup file first.")
        parser.add_argument("--title", action="append", default=[],
                            help="Only this saved-query title (repeatable; case-insensitive).")
        parser.add_argument("--backup-dir", default=None,
                            help="Directory for the JSON backup (default: settings.DASHBOARD_QUERY_BACKUP_DIR).")
        parser.add_argument("--revert", metavar="BACKUP_JSON", default=None,
                            help="Restore the previous SQL recorded in this backup file instead of rewriting.")

    # ------------------------------------------------------------------ util

    def _titles_wanted(self, options):
        return {t.strip().lower() for t in options["title"] if t.strip()}

    def _wanted(self, options, title):
        wanted = self._titles_wanted(options)
        return not wanted or title.strip().lower() in wanted

    def _backup_dir(self, options):
        return options["backup_dir"] or getattr(
            settings, "DASHBOARD_QUERY_BACKUP_DIR",
            os.path.join(getattr(settings, "BASE_DIR", os.getcwd()), "backups", "saved_queries"),
        )

    def _write_backup(self, options, action, changes):
        backup_dir = self._backup_dir(options)
        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        path = os.path.join(backup_dir, f"saved_queries_{action}_{stamp}.json")
        payload = {
            "created_utc": datetime.datetime.utcnow().isoformat() + "Z",
            "action": action,
            "command": "manage.py rewrite_dashboard_queries",
            "queries": [
                {"id": q.id, "title": q.title, "previous_sql": q.sql, "new_sql": new_sql}
                for q, new_sql in changes
            ],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, ensure_ascii=False)
        return path

    def _apply(self, changes):
        from explorer.models import Query

        for q, new_sql in changes:
            Query.objects.filter(pk=q.pk).update(sql=new_sql)

    # --------------------------------------------------------------- handle

    def handle(self, *args, **options):
        apply = options["apply"]
        if options["revert"]:
            changes = self._plan_revert(options)
            action = "revert"
        else:
            changes = self._plan_rewrite(options)
            action = "rewrite"

        if not changes:
            self.stdout.write(self.style.SUCCESS("Nothing to change."))
            return
        self.stdout.write(f"{len(changes)} saved quer{'y' if len(changes) == 1 else 'ies'} to {action}: "
                          + ", ".join(f"{q.id} ({q.title})" for q, _ in changes))
        if not apply:
            self.stdout.write(self.style.WARNING("Dry run — no changes made. Re-run with --apply to write."))
            return
        path = self._write_backup(options, action, changes)
        self.stdout.write(f"Previous SQL saved to {path}")
        self._apply(changes)
        self.stdout.write(self.style.SUCCESS(f"Updated {len(changes)} saved quer{'y' if len(changes) == 1 else 'ies'}."))
        if action == "rewrite":
            self.stdout.write(f"Revert with: manage.py rewrite_dashboard_queries --revert {path} --apply")

    # ------------------------------------------------------------- planning

    def _plan_rewrite(self, options):
        from explorer.models import Query

        changes = []
        for rw in REWRITES:
            if not self._wanted(options, rw.title):
                continue
            rows = list(Query.objects.filter(title__iexact=rw.title).order_by("id"))
            if not rows:
                self.stdout.write(f"NOT FOUND  {rw.title!r}: no saved query with this title")
                continue
            for q in rows:
                state = classify_saved_sql(rw, q.sql)
                if state == "rewritten":
                    self.stdout.write(f"SKIP       {q.id} {q.title}: already rewritten")
                    continue
                if state == "unknown":
                    self.stdout.write(self.style.WARNING(
                        f"SKIP       {q.id} {q.title}: current SQL is not the text this rewrite was proven "
                        f"against; review by hand (diff against the expected original below)"))
                    self.stdout.write(_diff(q.sql, rw.originals[0], f"{q.id}") or "  (differs only in whitespace? no)")
                    continue
                new_sql = rewrite_for(rw, q.sql)
                self.stdout.write(f"REWRITE    {q.id} {q.title}")
                self.stdout.write(_diff(q.sql, new_sql, f"{q.id}"))
                changes.append((q, new_sql))

        review = [t for t in REVIEW_TITLES if self._wanted(options, t)]
        if review:
            for q in Query.objects.filter(title__in=review).order_by("id"):
                self.stdout.write(f"REVIEW     {q.id} {q.title}: no rewrite shipped for this title; current SQL:")
                self.stdout.write("    " + (q.sql or "").replace("\n", "\n    "))
        return changes

    def _plan_revert(self, options):
        from explorer.models import Query

        path = options["revert"]
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as exc:
            raise CommandError(f"cannot read backup {path!r}: {exc}")
        entries = payload.get("queries") or []
        if not entries:
            raise CommandError(f"{path!r} holds no queries")
        changes = []
        for entry in entries:
            if not self._wanted(options, entry.get("title", "")):
                continue
            try:
                q = Query.objects.get(pk=entry["id"])
            except Query.DoesNotExist:
                self.stdout.write(self.style.WARNING(f"SKIP       {entry['id']} {entry.get('title')}: no such saved query"))
                continue
            previous, recorded_new = entry["previous_sql"], entry.get("new_sql")
            if normalize_sql(q.sql) == normalize_sql(previous):
                self.stdout.write(f"SKIP       {q.id} {q.title}: already holds the backed-up text")
                continue
            if recorded_new is not None and normalize_sql(q.sql) != normalize_sql(recorded_new):
                self.stdout.write(self.style.WARNING(
                    f"SKIP       {q.id} {q.title}: SQL changed since the backup was written; not reverting"))
                self.stdout.write(_diff(recorded_new, q.sql, f"{q.id} since backup"))
                continue
            self.stdout.write(f"REVERT     {q.id} {q.title}")
            self.stdout.write(_diff(q.sql, previous, f"{q.id}"))
            changes.append((q, previous))
        return changes
