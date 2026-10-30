"""
Summarise an ``import_osc --report`` file for review (#906). Reads only the file, no database.

  python3 manage.py osc_report_summary osc-dry-run.jsonl
  python3 manage.py osc_report_summary osc-dry-run.jsonl --review-dir osc-review --sample 100
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from YSE_App.catalog_import import report


class Command(BaseCommand):
    help = 'Summarise an import_osc --report file; optionally write review lists.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('report', help='The JSON-lines file written by import_osc --report.')
        parser.add_argument('--review-dir', default='', help='Write review CSV files here.')
        parser.add_argument('--sample', type=int, default=50, help='Matched entries to sample for a hand check.')
        parser.add_argument('--top', type=int, default=25, help='Rows per warning / instrument list.')

    def handle(self, *args, **opts):
        lines = list(report.read(opts['report']))
        self.stdout.write(report.format_summary(report.summarise(lines), top=opts['top']))
        if opts['review_dir']:
            for name, n in report.write_review_files(lines, opts['review_dir'], sample=opts['sample']).items():
                self.stdout.write('wrote %s (%d rows)' % (name, n))
