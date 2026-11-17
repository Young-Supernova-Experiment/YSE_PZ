"""
Store spectra as arrays instead of one row per pixel (#891).

Dry run by default; ``--apply`` writes. For each transient and/or host spectrum that has per-pixel
``TransientSpecData`` / ``HostSpecData`` rows and no ``TransientSpectrumArrays`` / ``HostSpectrumArrays``
row, write one arrays row (``YSE_App.common.spectrum_arrays``), in batches of ``--batch`` spectra, one
transaction per batch. It is resumable: spectra already converted are skipped. Each written row is
verified (``n_pixels`` equals the spectrum's pixel-row count); a spectrum that fails (e.g. a non-finite
pixel that ``build`` drops) is not converted and is reported.

Pixel rows are never deleted by the conversion. ``--delete-rows`` together with ``--apply`` deletes the
pixel rows of spectra whose arrays row exists and whose ``n_pixels`` matches the row count; without
``--apply`` it only counts them. Readers use the arrays row whenever there is one, so the rows are only
a fallback once a spectrum is converted.

Run against the default database::

  docker exec ysepz_web_container python3 manage.py convert_spectra_to_arrays
  docker exec ysepz_web_container python3 manage.py convert_spectra_to_arrays --apply
  docker exec ysepz_web_container python3 manage.py convert_spectra_to_arrays --apply --transient --batch 200 --max 1000
  docker exec ysepz_web_container python3 manage.py convert_spectra_to_arrays --apply --delete-rows
"""

from __future__ import annotations

from typing import Dict, List

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Exists, OuterRef

from YSE_App.common import spectrum_arrays
from YSE_App.models import (
    HostSpecData,
    HostSpectrum,
    HostSpectrumArrays,
    TransientSpecData,
    TransientSpectrum,
    TransientSpectrumArrays,
)

KINDS = {
    # kind: (spectrum model, pixel-row model, arrays model, host flag for spectrum_arrays)
    "transient": (TransientSpectrum, TransientSpecData, TransientSpectrumArrays, False),
    "host": (HostSpectrum, HostSpecData, HostSpectrumArrays, True),
}


def row_counts(rows_model, spectrum_ids) -> Dict[int, int]:
    """``{spectrum id: number of pixel rows}``."""
    return dict(
        rows_model.objects.filter(spectrum_id__in=list(spectrum_ids))
        .values("spectrum_id").annotate(n=Count("id")).values_list("spectrum_id", "n")
    )


def unconverted_spectra(kind):
    """Spectra of ``kind`` with pixel rows and no arrays row, by id."""
    spec_model, rows_model, arrays_model, _host = KINDS[kind]
    return (
        spec_model.objects.filter(Exists(rows_model.objects.filter(spectrum_id=OuterRef("pk"))))
        .exclude(Exists(arrays_model.objects.filter(spectrum_id=OuterRef("pk"))))
        .order_by("pk")
    )


def convert_batch(kind, spectrum_ids: List[int]):
    """Write arrays rows for these spectra in one transaction. Returns ``(converted, failed)`` id lists."""
    spec_model, rows_model, arrays_model, host = KINDS[kind]
    converted, failed = [], []
    with transaction.atomic():
        # skip any converted meanwhile (resumable / concurrent runs)
        done = set(arrays_model.objects.filter(spectrum_id__in=spectrum_ids).values_list("spectrum_id", flat=True))
        todo = [i for i in spectrum_ids if i not in done]
        counts = row_counts(rows_model, todo)
        pixels = spectrum_arrays.arrays_for(todo, host=host)
        spectra = spec_model.objects.in_bulk(todo)
        new_rows = []
        for sid in todo:
            if sid not in pixels or sid not in spectra:
                failed.append(sid)
                continue
            wave, flux, err = pixels[sid]
            try:
                row = spectrum_arrays.build(spectra[sid], wave, flux, err, host=host)
            except ValueError:
                failed.append(sid)
                continue
            if row.n_pixels != counts.get(sid, 0):
                failed.append(sid)
                continue
            new_rows.append(row)
        arrays_model.objects.bulk_create(new_rows)
        # verify what was written against the rows, inside the same transaction
        written = dict(arrays_model.objects.filter(spectrum_id__in=[r.spectrum_id for r in new_rows])
                       .values_list("spectrum_id", "n_pixels"))
        counts = row_counts(rows_model, written)
        bad = [sid for sid, n in written.items() if counts.get(sid, 0) != n]
        if bad:
            arrays_model.objects.filter(spectrum_id__in=bad).delete()
            failed.extend(bad)
        converted = [sid for sid in written if sid not in bad]
    return converted, failed


def verified_ids(kind, spectrum_ids: List[int]) -> Dict[int, int]:
    """Of these spectra (which have an arrays row), ``{id: row count}`` of those whose pixel rows
    exist and number exactly the arrays row's ``n_pixels``."""
    _spec, rows_model, arrays_model, _host = KINDS[kind]
    n_pixels = dict(arrays_model.objects.filter(spectrum_id__in=spectrum_ids).values_list("spectrum_id", "n_pixels"))
    counts = row_counts(rows_model, spectrum_ids)
    return {sid: n for sid, n in counts.items() if n and n_pixels.get(sid) == n}


class Command(BaseCommand):
    help = (
        "Write one arrays row per spectrum that is stored as per-pixel rows (#891). Dry run by default; "
        "--apply writes; --delete-rows (with --apply) deletes the pixel rows of verified conversions."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write the changes. Without it this is a dry run that only reports counts.")
        parser.add_argument("--batch", type=int, default=500, help="Spectra per transaction (default 500).")
        parser.add_argument("--max", type=int, default=None,
                            help="At most this many spectra of each kind are converted (and, with --delete-rows, "
                                 "have their rows deleted) in this run.")
        parser.add_argument("--transient", action="store_true", help="Transient spectra (default: both kinds).")
        parser.add_argument("--host", action="store_true", help="Host spectra (default: both kinds).")
        parser.add_argument("--delete-rows", action="store_true",
                            help="With --apply: delete the pixel rows of spectra whose arrays row exists and "
                                 "whose n_pixels matches the row count. Without --apply: only count them.")

    def handle(self, *args, **options):
        apply = options["apply"]
        batch = options["batch"]
        limit = options["max"]
        if batch < 1:
            raise CommandError("--batch must be at least 1")
        if limit is not None and limit < 0:
            raise CommandError("--max must not be negative")
        kinds = [k for k in ("transient", "host") if options[k]] or ["transient", "host"]
        if not apply:
            self.stdout.write("Dry run: nothing is written. Use --apply to convert.")

        for kind in kinds:
            self._convert(kind, apply, batch, limit)
            if options["delete_rows"]:
                self._delete_rows(kind, apply, batch, limit)

    def _convert(self, kind, apply, batch, limit):
        candidates = unconverted_spectra(kind)
        total = candidates.count()
        planned = total if limit is None else min(total, limit)
        if not apply:
            self.stdout.write(f"{kind} spectra to convert: {planned} (of {total} without arrays)")
            return
        n_done = n_failed = 0
        last_id = 0
        while n_done + n_failed < planned:
            size = min(batch, planned - n_done - n_failed)
            ids = list(candidates.filter(pk__gt=last_id).values_list("pk", flat=True)[:size])
            if not ids:
                break
            last_id = ids[-1]
            converted, failed = convert_batch(kind, ids)
            n_done += len(converted)
            n_failed += len(failed)
            self.stdout.write(f"  {kind}: converted {n_done}, not converted {n_failed} (up to id {last_id})")
            if failed:
                self.stdout.write(f"  {kind} spectra not converted (pixel count mismatch or no finite pixels): "
                                  f"{sorted(failed)}")
        self.stdout.write(f"{kind} spectra converted: {n_done}; not converted: {n_failed}")

    def _delete_rows(self, kind, apply, batch, limit):
        _spec, rows_model, arrays_model, _host = KINDS[kind]
        with_rows = arrays_model.objects.filter(
            Exists(rows_model.objects.filter(spectrum_id=OuterRef("spectrum_id")))).order_by("spectrum_id")
        n_spectra = n_rows = n_mismatch = 0
        last_id = 0
        while limit is None or n_spectra < limit:
            size = batch if limit is None else min(batch, limit - n_spectra)
            ids = list(with_rows.filter(spectrum_id__gt=last_id).values_list("spectrum_id", flat=True)[:size])
            if not ids:
                break
            last_id = ids[-1]
            if apply:
                with transaction.atomic():
                    ok = verified_ids(kind, ids)
                    deleted = rows_model.objects.filter(spectrum_id__in=list(ok)).delete()[0] if ok else 0
                n_rows += deleted
            else:
                ok = verified_ids(kind, ids)
                n_rows += sum(ok.values())
            n_spectra += len(ok)
            n_mismatch += len(ids) - len(ok)
        verb = "deleted" if apply else "would delete"
        self.stdout.write(f"{kind} pixel rows {verb}: {n_rows} rows of {n_spectra} spectra; "
                          f"kept (arrays n_pixels does not match row count): {n_mismatch}")
