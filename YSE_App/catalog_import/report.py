"""
Summary of an ``import_osc --report`` file (#906), for review before writing anything.

The report has one JSON line per entry (``entry``, ``counts``, ``warnings`` and, from a dry run with a
database, ``preview``: action, instruments, duplicate decisions, host check; from ``--apply``,
``result``) and one line per file that failed to download or parse (``stage``, ``error``).
:func:`summarise` adds them up; :func:`write_review_files` writes CSV lists for a person to check.
Pure Python: no database.
"""

from __future__ import annotations

import csv
import json
import os
import random
import re
from collections import Counter
from typing import Dict, Iterable, Iterator, List

from YSE_App.catalog_import import matching

_SPECTRUM_PREFIX = re.compile(r'^spectrum \d+ \([^)]*\): ')
_DETAIL = re.compile(r'[:(].*$')


def read(path: str) -> Iterator[dict]:
    with open(path) as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except ValueError:
                    yield {'entry': 'line %d' % number, 'stage': 'report', 'error': 'unreadable report line'}


def warning_kind(text: str) -> str:
    """``spectrum 3 (x.flm): 2 non-numeric pixels dropped`` -> ``non-numeric pixels dropped``."""
    text = _SPECTRUM_PREFIX.sub('', text)
    text = re.sub(r'^\d+ ', '', text)
    return _DETAIL.sub('', text).strip()


def _action(line: dict) -> str:
    if 'result' in line:
        return 'created' if line['result'].get('created') else 'added data'
    action = (line.get('preview') or {}).get('action', '')
    for prefix, label in (('create', 'would create'), ('add data', 'would add data'), ('skip', 'would skip')):
        if action.startswith(prefix):
            return label
    return 'offline (no database)' if 'counts' in line else 'failed'


def summarise(lines: Iterable[dict]) -> dict:
    out = {
        'entries': 0, 'failures_by_stage': Counter(), 'actions': Counter(), 'totals': Counter(),
        'photometry_by_kind': Counter(), 'model_rows_skipped': Counter(), 'warnings': Counter(),
        'photometry_decisions': Counter(), 'spectrum_decisions': Counter(), 'host': Counter(),
        'unknown_instruments': Counter(),
    }
    for line in lines:
        if 'counts' not in line:
            out['failures_by_stage'][line.get('stage', '?')] += 1
            continue
        out['entries'] += 1
        if line.get('error'):
            out['failures_by_stage']['ingest'] += 1
        out['actions'][_action(line)] += 1
        c = line['counts']
        for key in ('sources', 'quantities', 'photometry_sets', 'photometry_points', 'spectra', 'spectrum_pixels'):
            out['totals'][key] += c.get(key, 0) or 0
        out['photometry_by_kind'].update(c.get('photometry_by_kind') or {})
        out['model_rows_skipped'].update(c.get('model_rows_skipped') or {})
        out['warnings'].update(warning_kind(w) for w in line.get('warnings') or [])
        decided = line.get('preview') or line.get('result') or {}
        out['photometry_decisions'].update(decided.get('photometry') or {})
        out['spectrum_decisions'].update(decided.get('spectra') or {})
        host = decided.get('host')
        if host:
            out['host'][host['status'] if isinstance(host, dict) else host] += 1
        for catalog_name, yse_name in ((line.get('preview') or {}).get('instruments') or {}).items():
            if yse_name == 'Unknown' and catalog_name != '- / -':
                out['unknown_instruments'][catalog_name] += 1
    return out


def format_summary(summary: dict, top: int = 25) -> str:
    def block(title, counter, limit=None):
        items = counter.most_common(limit)
        rows = ['%s:' % title] + ['  %9d  %s' % (n, k) for k, n in items] if items else ['%s: none' % title]
        if limit and len(counter) > limit:
            rows.append('  ... %d more' % (len(counter) - limit))
        return rows

    lines = ['entries: %d' % summary['entries']]
    lines += block('failures by stage', summary['failures_by_stage'])
    lines += block('actions', summary['actions'])
    lines += block('totals', summary['totals'])
    lines += block('photometry by kind', summary['photometry_by_kind'])
    lines += block('model data skipped', summary['model_rows_skipped'])
    lines += block('photometry duplicate decisions', summary['photometry_decisions'])
    lines += block('spectrum duplicate decisions', summary['spectrum_decisions'])
    lines += block('host check', summary['host'])
    lines += block('warnings', summary['warnings'], top)
    lines += block('instrument strings resolving to Unknown (entries)', summary['unknown_instruments'], top)
    return '\n'.join(lines)


def write_review_files(lines: List[dict], out_dir: str, *, sample: int = 50, seed: int = 1) -> Dict[str, int]:
    """CSV lists to check by hand; returns rows per file."""
    os.makedirs(out_dir, exist_ok=True)
    review, hosts, failures, matched = [], [], [], []
    for line in lines:
        if 'counts' not in line or line.get('error'):
            failures.append([line.get('entry'), line.get('stage', 'ingest'), line.get('origin', ''), line.get('error')])
            continue
        decided = line.get('preview') or line.get('result') or {}
        phot, spec = decided.get('photometry') or {}, decided.get('spectra') or {}
        flagged = {k: phot.get(k, 0) + spec.get(k, 0)
                   for k in (matching.SUPERSEDED_EXISTING, matching.REVIEW, matching.KEPT_EXISTING)}
        if flagged[matching.SUPERSEDED_EXISTING] or flagged[matching.REVIEW]:
            review.append([line['entry'], decided.get('action', ''), flagged[matching.SUPERSEDED_EXISTING],
                           flagged[matching.REVIEW], flagged[matching.KEPT_EXISTING]])
        host = decided.get('host')
        if isinstance(host, dict) and host.get('status') == 'discrepant':
            hosts.append([line['entry'], host.get('osc_host'), host.get('yse_host'), host.get('separation_arcsec'),
                          host.get('distance_mpc')])
        if _action(line) in ('would add data', 'added data'):
            matched.append([line['entry'], (line.get('preview') or {}).get('action') or line['result'].get('matched_by')])
    random.Random(seed).shuffle(matched)
    files = {
        'review_duplicates.csv': (['entry', 'action', 'superseded_existing', 'review', 'kept_existing'], review),
        'host_discrepancies.csv': (['entry', 'osc_host', 'yse_host', 'separation_arcsec', 'distance_mpc'], hosts),
        'failures.csv': (['entry', 'stage', 'origin', 'error'], failures),
        'sample_matches.csv': (['entry', 'match'], matched[:sample]),
    }
    counts = {}
    for name, (header, rows) in files.items():
        with open(os.path.join(out_dir, name), 'w', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)
        counts[name] = len(rows)
    return counts
