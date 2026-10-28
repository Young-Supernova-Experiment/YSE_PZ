"""
Write an OSC import plan (``osc.parse_entry``) to the database, by Ryan's rules (2026-10-27).

1. **Find the YSE_PZ transient**: the one the entry was imported to before, else one named like the
   entry or an alias (``SN2011fe`` also matches ``2011fe``) or carrying one as an alternate name,
   else one within ``match_radius`` arcsec.
2. **Existing transient**: its coordinates, classification, redshift, host and other fields do not
   change; only data are added (and the OSC names as alternate names, for tracking).
3. **No duplicates** (#888): each OSC point and spectrum is compared with the transient's data by
   origin (``matching``: time, instrument, filter or wavelength range). On a match YSE_PZ's copy is
   kept and the OSC copy is not imported, unless it comes from a refereed paper: then it is imported
   and YSE_PZ's copy is flagged REJECTED (undone by a re-import or ``--revert``). Every match is a
   ``CatalogDataMatch`` row.
4. **New transient**: name, position, dates, class and redshift from the OSC; status ``new_status``.
5. **TNS** (#889): a new transient waits (``tns_status`` pending) for the throttled TNS check
   (``osc_tns``), which takes the TNS name, position and class.

Host (#893): beyond 20 Mpc the OSC host is never linked (Prost/Blast decide; Prost is queued when
enabled); at 20 Mpc or closer a new transient gets the OSC host, and for an existing transient a
different host is only flagged (``CatalogEntry.host_check``).

Re-importing an entry undoes and deletes what its last import wrote, then writes it anew.
Photometry and spectra get the ``OSC`` observation group and the Public group (OSC data are
published). Spectra are stored as arrays (#492). Model fits keep their outputs, not chains (#892).

Import is historical: automatic TNS publishing, the minor-planet screen and annotation runs are off
for transients created here, rows are written with ``bulk_create`` or raw saves (no per-row
new-observation signals), and photometry statistics are recomputed once per transient.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional, Tuple

from django.contrib.auth.models import Group
from django.db import transaction
from django.db.models import Q
from django.test.utils import override_settings
from django.utils import timezone

from YSE_App.catalog_import import matching, osc
from YSE_App.catalog_import.instruments import InstrumentResolver, NoUnknownInstrument
from YSE_App.common import spectrum_arrays
from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.common.quality_mask import PhotFlag, SpecFlag, apply_wavelength_medium, mask_from_flags
from YSE_App.models import (
    AlternateTransientNames,
    Host,
    Instrument,
    MagSystem,
    ObservationGroup,
    PhotometricBand,
    Transient,
    TransientClass,
    TransientPhotData,
    TransientPhotometry,
    TransientSpectrum,
    TransientStatus,
    Unit,
)
from YSE_App.models.catalog_models import (
    CatalogDataMatch,
    CatalogEntry,
    CatalogEntrySource,
    CatalogModelFit,
    CatalogSource,
    TransientCatalogQuantity,
    TransientPhotDataCatalogInfo,
    TransientPhotometryCatalogInfo,
    TransientSpectrumCatalogInfo,
)
from YSE_App.services import change_log, photstat, redshift_precedence

logger = logging.getLogger(__name__)

BULK_BATCH = 2000
DEFAULT_MATCH_RADIUS_ARCSEC = 2.0
HOST_MATCH_RADIUS_ARCSEC = 3.0
# An OSC host this far from YSE's host (or with another name, without coordinates) is a discrepancy.
HOST_DISCREPANCY_ARCSEC = 10.0
MAX_REFERENCE_LENGTH = 512
# Change-log service name of this importer.
SERVICE = 'osc-import'
PHOT_REJECTED = int(PhotFlag.REJECTED)
SPEC_REJECTED = int(SpecFlag.REJECTED)


class EntryNotImported(Exception):
    """The entry cannot be imported (reported, not raised past the command)."""


# ---- read-only lookups -----------------------------------------------------------------------

def _box(ra: float, dec: float, radius_arcsec: float) -> Q:
    import math

    d_dec = radius_arcsec / 3600.0
    d_ra = d_dec / max(math.cos(math.radians(dec)), 1e-3)
    return Q(ra__gte=ra - d_ra, ra__lte=ra + d_ra, dec__gte=dec - d_dec, dec__lte=dec + d_dec)


def _separation_arcsec(ra1, dec1, ra2, dec2) -> float:
    import math

    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    c = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c)))) * 3600.0


def candidate_names(plan: dict) -> List[str]:
    t = plan['transient']
    names = [t['name'], plan['entry_name']] + list(t.get('iau_names') or [])
    for alias in t['aliases']:
        names.extend([alias, osc.yse_name(alias)])
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def match_transient(plan: dict, match_radius: float = DEFAULT_MATCH_RADIUS_ARCSEC) -> Tuple[Optional[Transient], str]:
    """Step 1: the existing transient for this entry, and how it was found ('' when there is none)."""
    entry = CatalogEntry.objects.filter(catalog=plan['catalog'], entry_name=plan['entry_name']).select_related('transient').first()
    if entry is not None:
        return entry.transient, 'imported before'
    names = candidate_names(plan)
    found = list(Transient.objects.filter(name__in=names).order_by('id')[:2])
    if found:
        return found[0], 'name %s' % found[0].name
    alt = AlternateTransientNames.objects.filter(name__in=names).select_related('transient').order_by('id').first()
    if alt is not None:
        return alt.transient, 'alternate name %s' % alt.name
    t = plan['transient']
    if t['ra'] is not None and t['dec'] is not None and match_radius > 0:
        best, best_sep = None, None
        for cand in Transient.objects.filter(_box(t['ra'], t['dec'], match_radius)).only('id', 'name', 'ra', 'dec'):
            sep = _separation_arcsec(t['ra'], t['dec'], cand.ra, cand.dec)
            if sep <= match_radius and (best_sep is None or sep < best_sep):
                best, best_sep = cand, sep
        if best is not None:
            return Transient.objects.get(pk=best.pk), 'position (%.2f arcsec from %s)' % (best_sep, best.name)
    return None, ''


def _set_refereed(plan: dict, phot_set: dict) -> bool:
    by_alias = {s['alias']: s for s in plan['sources']}
    return any((by_alias.get(a) or {}).get('refereed') for a in phot_set['source_aliases'])


def _median_mjd(points) -> Optional[float]:
    times = sorted(p['mjd'] for p in points)
    return times[len(times) // 2] if times else None


class Planner:
    """Instrument resolution and same-origin matching, shared by the dry run and the writer."""

    def __init__(self, resolver: InstrumentResolver, *, phot_tolerance=matching.PHOT_TIME_TOLERANCE,
                 spec_tolerance=matching.SPEC_TIME_TOLERANCE):
        self.resolver = resolver
        self.phot_tolerance = phot_tolerance
        self.spec_tolerance = spec_tolerance

    def set_instrument(self, phot_set: dict) -> Instrument:
        return self.resolver.resolve(phot_set['telescope'], phot_set['instrument'], _median_mjd(phot_set['points']))

    def spectrum_instrument(self, spectrum: dict) -> Instrument:
        return self.resolver.resolve(spectrum['telescope'], spectrum['instrument'], spectrum['mjd'])

    def photometry_decisions(self, plan: dict, transient: Optional[Transient]) -> Dict[tuple, tuple]:
        """``{(set index, point index): (decision, Match or None)}`` against the transient's data."""
        new, refereed = [], {}
        for si, phot_set in enumerate(plan['photometry_sets']):
            inst = self.set_instrument(phot_set)
            refereed[si] = _set_refereed(plan, phot_set)
            for pi, p in enumerate(phot_set['points']):
                new.append(matching.Obs(key=(si, pi), mjd=p['mjd'], instrument_id=inst.id, band=p['band'], mag=p['mag']))
        found = {}
        if transient is not None and transient.pk:
            existing = matching.existing_photometry(transient.pk)
            for m in matching.match_photometry(new, existing, self.resolver.unknown().id, tolerance=self.phot_tolerance):
                found[m.new.key] = m
        return {o.key: (matching.decide(found.get(o.key), refereed[o.key[0]]), found.get(o.key)) for o in new}

    def spectrum_decisions(self, plan: dict, transient: Optional[Transient]) -> Dict[int, tuple]:
        by_alias = {s['alias']: s for s in plan['sources']}
        new = []
        for i, s in enumerate(plan['spectra']):
            waves = [p[0] for p in s['pixels']]
            new.append(matching.Obs(key=i, mjd=s['mjd'], instrument_id=self.spectrum_instrument(s).id,
                                    wavelength_min=min(waves), wavelength_max=max(waves)))
        found = {}
        if transient is not None and transient.pk:
            existing = matching.existing_spectra(transient.pk)
            for m in matching.match_spectra(new, existing, self.resolver.unknown().id, tolerance=self.spec_tolerance):
                found[m.new.key] = m
        out = {}
        for o in new:
            refereed = any((by_alias.get(a) or {}).get('refereed') for a in plan['spectra'][o.key]['source_aliases'])
            out[o.key] = (matching.decide(found.get(o.key), refereed), found.get(o.key))
        return out


def host_check(plan: dict, transient: Optional[Transient], created: bool) -> dict:
    """The host rule (#893): what to do with the OSC host. Reads only."""
    t = plan['transient']
    h = t['host']
    out = {'distance_mpc': t.get('distance_mpc'), 'distance_basis': t.get('distance_basis'), 'osc_host': h['name'],
           'osc_host_ra': h['ra'], 'osc_host_dec': h['dec']}
    if not (h['name'] or h['ra'] is not None):
        return dict(out, status='no_osc_host')
    d = t.get('distance_mpc')
    if d is None:
        return dict(out, status='distance_unknown')
    if d > osc.HOST_DISTANCE_LIMIT_MPC:
        return dict(out, status='prost_blast_precedence')
    if created:
        return dict(out, status='link_osc_host' if h['ra'] is not None else 'osc_host_has_no_position')
    if transient is None or not transient.host_id:
        return dict(out, status='yse_has_no_host')
    yse = transient.host
    out.update(yse_host=yse.name, yse_host_id=yse.id)
    if h['ra'] is not None and h['dec'] is not None:
        sep = _separation_arcsec(h['ra'], h['dec'], yse.ra, yse.dec)
        out['separation_arcsec'] = round(sep, 2)
        return dict(out, status='discrepant' if sep > HOST_DISCREPANCY_ARCSEC else 'consistent')
    same = (h['name'] or '').replace(' ', '').lower() == (yse.name or '').replace(' ', '').lower()
    return dict(out, status='consistent' if same else 'discrepant')


def preview_entry(plan: dict, match_radius: float = DEFAULT_MATCH_RADIUS_ARCSEC, resolver=None) -> dict:
    """What ``apply_entry`` would do, from reads only."""
    from collections import Counter

    transient, how = match_transient(plan, match_radius)
    resolver = resolver or InstrumentResolver()
    planner = Planner(resolver)
    t = plan['transient']
    out = {'transient': transient, 'instruments': {}, 'photometry': {}, 'spectra': {}, 'host': None}
    if transient is None and (t['ra'] is None or t['dec'] is None):
        out['action'] = 'skip: no match and no position'
        return out
    out['action'] = 'add data to %s (matched by %s)' % (transient.name, how) if transient else 'create %s' % t['name']
    try:
        for item in plan['photometry_sets']:
            inst = planner.set_instrument(item)
            out['instruments']['%s / %s' % (item['telescope'] or '-', item['instrument'] or '-')] = inst.name
        for item in plan['spectra']:
            inst = planner.spectrum_instrument(item)
            out['instruments']['%s / %s' % (item['telescope'] or '-', item['instrument'] or '-')] = inst.name
        out['photometry'] = dict(Counter(d for d, _ in planner.photometry_decisions(plan, transient).values()))
        out['spectra'] = dict(Counter(d for d, _ in planner.spectrum_decisions(plan, transient).values()))
    except NoUnknownInstrument as exc:
        out['action'] += ' (cannot resolve instruments: %s)' % exc
    out['host'] = host_check(plan, transient, transient is None)
    return out


def restore_superseded(entry: CatalogEntry) -> int:
    """Clear the REJECTED flag this entry's import put on YSE data (where it was not set before)."""
    restored = 0
    for match in entry.matches.filter(decision=CatalogDataMatch.SUPERSEDED_EXISTING):
        was_rejected = bool((match.previous_quality_mask or 0) & (PHOT_REJECTED if match.existing_phot_data_id else SPEC_REJECTED))
        if was_rejected:
            continue
        if match.existing_phot_data_id:
            row = TransientPhotData.objects.filter(pk=match.existing_phot_data_id).first()
            bit = PHOT_REJECTED
        else:
            row = TransientSpectrum.objects.filter(pk=match.existing_spectrum_id).first()
            bit = SPEC_REJECTED
        if row is not None and row.quality_mask & bit:
            type(row).objects.filter(pk=row.pk).update(quality_mask=row.quality_mask & ~bit)
            restored += 1
    return restored


def delete_entry_data(entry: CatalogEntry) -> None:
    """Undo and delete what an import of this entry wrote; the CatalogEntry cascade takes the rest."""
    restore_superseded(entry)
    TransientPhotometry.objects.filter(catalog_info__entry=entry).delete()
    TransientSpectrum.objects.filter(catalog_info__entry=entry).delete()
    entry.delete()


# ---- writing ---------------------------------------------------------------------------------

class Writer:
    """Applies plans for one user; caches the lookups shared by every entry."""

    def __init__(self, user, *, new_status: str = 'Ignore', match_radius: float = DEFAULT_MATCH_RADIUS_ARCSEC,
                 with_spectra: bool = True, with_models: bool = True, tns_check: bool = True,
                 phot_tolerance: float = matching.PHOT_TIME_TOLERANCE,
                 spec_tolerance: float = matching.SPEC_TIME_TOLERANCE):
        self.user = user
        self.audit = {'created_by': user, 'modified_by': user}
        self.new_status_name = new_status
        self.match_radius = match_radius
        self.with_spectra = with_spectra
        self.with_models = with_models
        self.tns_check = tns_check
        self.phot_tolerance = phot_tolerance
        self.spec_tolerance = spec_tolerance
        self._forget()

    def _forget(self) -> None:
        self.instruments = InstrumentResolver()
        self.planner = Planner(self.instruments, phot_tolerance=self.phot_tolerance, spec_tolerance=self.spec_tolerance)
        self._bands: Dict[tuple, PhotometricBand] = {}
        self._mag_systems: Dict[str, Optional[MagSystem]] = {}
        self._units: Dict[str, Optional[Unit]] = {}
        self._sources: Dict[tuple, CatalogSource] = {}
        self._obs_group = self._public = self._status = None

    # Shared rows, created on first use.
    def obs_group(self) -> ObservationGroup:
        if self._obs_group is None:
            self._obs_group, _ = ObservationGroup.objects.get_or_create(name=osc.OBS_GROUP_NAME, defaults=self.audit)
        return self._obs_group

    def public_group(self) -> Group:
        if self._public is None:
            self._public, _ = Group.objects.get_or_create(name=PUBLIC_COLLABORATION_GROUP_NAME)
        return self._public

    def new_status(self) -> TransientStatus:
        if self._status is None:
            self._status = TransientStatus.objects.filter(name=self.new_status_name).order_by('id').first()
            if self._status is None:
                raise EntryNotImported('no transient status named %r' % self.new_status_name)
        return self._status

    def band(self, instrument: Instrument, name: str) -> PhotometricBand:
        key = (instrument.id, name)
        if key not in self._bands:
            band = PhotometricBand.objects.filter(instrument=instrument, name=name).order_by('id').first()
            if band is None:
                band = PhotometricBand.objects.create(instrument=instrument, name=name, **self.audit)
            self._bands[key] = band
        return self._bands[key]

    def mag_system(self, name: Optional[str]) -> Optional[MagSystem]:
        if not name:
            return None
        if name not in self._mag_systems:
            self._mag_systems[name] = MagSystem.objects.filter(name__iexact=name).order_by('id').first()
        return self._mag_systems[name]

    def unit(self, name: Optional[str]) -> Optional[Unit]:
        if not name:
            return None
        if name not in self._units:
            self._units[name] = Unit.objects.filter(name__iexact=name).order_by('id').first()
        return self._units[name]

    def source(self, raw: dict) -> CatalogSource:
        """The shared CatalogSource for an entry's source row: by bibcode, DOI, arXiv id, else name and URL."""
        for field in ('bibcode', 'doi', 'arxivid'):
            if raw.get(field):
                key = (field, raw[field])
                break
        else:
            key = ('name', raw['name'], raw.get('url') or '')
        if key not in self._sources:
            if key[0] == 'name':
                qs = CatalogSource.objects.filter(name=raw['name'], url=raw.get('url'))
                if not raw.get('url'):
                    qs = CatalogSource.objects.filter(name=raw['name']).filter(Q(url__isnull=True) | Q(url=''))
            else:
                qs = CatalogSource.objects.filter(**{key[0]: key[1]})
            src = qs.order_by('id').first()
            if src is None:
                src = CatalogSource.objects.create(
                    **{f: raw.get(f) for f in osc.SOURCE_FIELDS}, extra=raw.get('extra'), **self.audit)
            else:
                # Fill what an earlier entry's copy of the source lacked.
                changed = [f for f in osc.SOURCE_FIELDS if getattr(src, f) in (None, '') and raw.get(f) not in (None, '')]
                for f in changed:
                    setattr(src, f, raw[f])
                if changed:
                    src.save(update_fields=changed + ['modified_date'])
            self._sources[key] = src
        return self._sources[key]

    # ---- one entry --------------------------------------------------------------------------

    def apply_entry(self, plan: dict) -> dict:
        """Write one entry; returns a summary dict. Raises EntryNotImported for entries that cannot go in."""
        try:
            with override_settings(SHARING_AUTOPUBLISH_ON_SAVE=False, FEEDS_MPC_SCREEN_ON_CREATE=False,
                                   ANNOTATION_AUTORUN_SERVICES=''):
                with transaction.atomic(), photstat.deferred_updates():
                    return self._apply(plan)
        except NoUnknownInstrument as exc:
            self._forget()
            raise EntryNotImported(str(exc))
        except Exception:
            # Rows cached during the rolled-back entry (a new band, source or group) no longer exist.
            self._forget()
            raise

    def _apply(self, plan: dict) -> dict:
        transient, how = match_transient(plan, self.match_radius)
        old = CatalogEntry.objects.filter(catalog=plan['catalog'], entry_name=plan['entry_name']).first()
        carried = {}
        if old is not None:
            carried = {f: getattr(old, f) for f in ('created_transient', 'import_values', 'tns_status', 'tns_name',
                                                     'tns_checked', 'tns_note')}
            delete_entry_data(old)
        created = transient is None
        if created:
            transient = self._create_transient(plan)
            t = transient
            carried = {
                'created_transient': True,
                'import_values': {'name': t.name, 'ra': t.ra, 'dec': t.dec, 'best_spec_class_id': t.best_spec_class_id,
                                  'iau_names': plan['transient'].get('iau_names') or [],
                                  'disc_year': t.disc_date.year if t.disc_date else None},
                'tns_status': CatalogEntry.TNS_PENDING if self.tns_check else CatalogEntry.TNS_SKIPPED,
            }
        check = host_check(plan, transient, created)
        entry = CatalogEntry.objects.create(
            transient=transient, catalog=plan['catalog'], entry_name=plan['entry_name'][:128],
            schema=plan['schema'], origin=(plan['origin'] or '')[:512] or None, extra=plan['extra'],
            host_check=check, **carried, **self.audit)

        by_alias: Dict[str, CatalogSource] = {}
        links = []
        for raw in plan['sources']:
            src = self.source(raw)
            alias = (raw.get('alias') or str(len(links) + 1))[:16]
            if alias in by_alias:
                continue
            by_alias[alias] = src
            links.append(CatalogEntrySource(entry=entry, source=src, alias=alias))
        CatalogEntrySource.objects.bulk_create(links)

        self._add_alternate_names(transient, plan)
        if created:
            self._new_transient_host(transient, plan, check)
        fits = self._write_models(entry, plan, by_alias) if self.with_models else {}
        n_quantities = self._write_quantities(entry, transient, plan, by_alias, fits)
        phot = self._write_photometry(entry, transient, plan, by_alias)
        spec = self._write_spectra(entry, transient, plan, by_alias, fits) if self.with_spectra else {}
        photstat.schedule_recompute(transient.id)
        return {
            'transient': transient.name, 'created': created, 'matched_by': how, 'entry_id': entry.id,
            'quantities': n_quantities, 'photometry': phot, 'spectra': spec, 'model_fits': len(fits),
            'host': check['status'],
        }

    def _create_transient(self, plan: dict) -> Transient:
        """Step 4: everything from the OSC."""
        t = plan['transient']
        if t['ra'] is None or t['dec'] is None:
            raise EntryNotImported('%s: no transient matches it and it has no position' % plan['entry_name'])
        transient = Transient(
            name=t['name'][:64], ra=t['ra'], dec=t['dec'], status=self.new_status(), obs_group=self.obs_group(),
            disc_date=t['disc_date'] if t['disc_date_precision'] == 'day' else None, mw_ebv=t['mw_ebv'],
            abs_mag_peak=t['abs_mag_peak'], abs_mag_peak_date=t['abs_mag_peak_date'], **self.audit)
        if t['spec_class']:
            transient.best_spec_class = (
                TransientClass.objects.filter(name=t['spec_class']).order_by('id').first()
                or TransientClass.objects.filter(name__iexact=t['spec_class']).order_by('id').first())
        transient.save()
        if t['redshift'] is not None:
            redshift_precedence.apply_redshift(transient, t['redshift'], t['redshift_err'], osc.CATALOG,
                                               user=self.user, service=SERVICE)
        return transient

    def _add_alternate_names(self, transient: Transient, plan: dict) -> None:
        names = [n for n in [plan['entry_name']] + plan['transient']['aliases'] if n and n != transient.name]
        existing = set(AlternateTransientNames.objects.filter(transient=transient).values_list('name', flat=True))
        for name in dict.fromkeys(names):
            if name[:64] not in existing:
                AlternateTransientNames.objects.create(
                    transient=transient, name=name[:64], obs_group=self.obs_group(),
                    description='Open Supernova Catalog alias', **self.audit)

    def _new_transient_host(self, transient: Transient, plan: dict, check: dict) -> None:
        """#893: link the OSC host within 20 Mpc; beyond, leave it to Prost (queued when enabled)."""
        if check['status'] == 'prost_blast_precedence' or check['status'] == 'distance_unknown':
            try:
                from YSE_App.services import prost_hosts

                prost_hosts.queue_match(transient, created_by=self.user)
            except Exception:  # noqa: BLE001 - optional enrichment must not fail the import
                logger.exception('could not queue a Prost host match for %s', transient.name)
            return
        if check['status'] != 'link_osc_host':
            return
        h = plan['transient']['host']
        host = None
        for cand in Host.objects.filter(_box(h['ra'], h['dec'], HOST_MATCH_RADIUS_ARCSEC)):
            if _separation_arcsec(h['ra'], h['dec'], cand.ra, cand.dec) <= HOST_MATCH_RADIUS_ARCSEC:
                host = cand
                break
        if host is None:
            host = Host.objects.create(ra=h['ra'], dec=h['dec'], name=h['name'], **self.audit)
            if h['redshift'] is not None:
                redshift_precedence.apply_redshift(host, h['redshift'], h['redshift_err'], osc.CATALOG,
                                                   user=self.user, service=SERVICE)
        transient.host = host
        transient.save(update_fields=['host', 'modified_by', 'modified_date'])
        change_log.record_change(transient, 'host', change_log.host_label(None), change_log.host_label(host.pk),
                                 service=SERVICE, category='host', source=osc.CATALOG, force=True)

    @staticmethod
    def _sources_for(aliases: Iterable[str], by_alias: Dict[str, CatalogSource]) -> List[CatalogSource]:
        return [by_alias[a] for a in aliases if a in by_alias]

    @staticmethod
    def _reference(sources: List[CatalogSource]) -> str:
        names = ', '.join(dict.fromkeys(s.bibcode or s.reference or s.name for s in sources))
        return ('OSC: ' + names)[:MAX_REFERENCE_LENGTH] if names else 'OSC'

    def _write_models(self, entry, plan, by_alias) -> Dict[str, CatalogModelFit]:
        fits = {}
        for m in plan['models']:
            fit = CatalogModelFit.objects.create(
                entry=entry, **{f: (m.get(f) or None) for f in osc.MODEL_FIELDS},
                **{f: m.get(f) for f in osc.MODEL_JSON_FIELDS + osc.MODEL_OUTPUT_FIELDS},
                extra=m.get('extra'), **self.audit)
            fit.sources.set(self._sources_for(m['source_aliases'], by_alias))
            fits[m['alias'] or str(len(fits) + 1)] = fit
        return fits

    def _write_quantities(self, entry, transient, plan, by_alias, fits) -> int:
        through = TransientCatalogQuantity.sources.through
        links = []
        for q in plan['quantities']:
            row = TransientCatalogQuantity.objects.create(
                entry=entry, transient=transient, key=q['key'][:32], value=q['value'], value_float=q['value_float'],
                kind=q['kind'], correlations=q['correlations'], model_fit=fits.get(q['model_alias']),
                extra=q['extra'],
                **{f: q[f] for f in osc.QUANTITY_FLOAT_FIELDS + osc.QUANTITY_TEXT_FIELDS + osc.QUANTITY_BOOL_FIELDS},
                **self.audit)
            links.extend(through(transientcatalogquantity_id=row.id, catalogsource_id=s.id)
                         for s in self._sources_for(q['source_aliases'], by_alias))
        through.objects.bulk_create(links, batch_size=BULK_BATCH, ignore_conflicts=True)
        return len(plan['quantities'])

    @staticmethod
    def _record(point: dict) -> dict:
        """The catalogue point as kept in a CatalogDataMatch when it is not imported."""
        out = {k: point[k] for k in ('mjd', 'band', 'mag', 'mag_err', 'flux', 'flux_err', 'flux_zero_point')}
        out['info'] = {k: v for k, v in point['info'].items() if v is not None}
        out['source_aliases'] = list(point['source_aliases'])
        return out

    def _write_photometry(self, entry, transient, plan, by_alias) -> dict:
        from collections import Counter

        decisions = self.planner.photometry_decisions(plan, transient)
        counts = Counter()
        matches, supersede = [], []
        for si, phot_set in enumerate(plan['photometry_sets']):
            keep = []
            for pi, p in enumerate(phot_set['points']):
                decision, m = decisions[(si, pi)]
                counts[decision] += 1
                if decision == matching.KEPT_EXISTING:
                    matches.append(CatalogDataMatch(
                        entry=entry, existing_phot_data_id=m.existing.key, decision=decision, weak=m.weak,
                        time_difference=m.time_difference, catalog_record=self._record(p)))
                else:
                    keep.append((p, decision, m))
            if not keep:
                continue
            instrument = self.planner.set_instrument(phot_set)
            sources = self._sources_for(phot_set['source_aliases'], by_alias)
            header = TransientPhotometry.objects.create(
                transient=transient, instrument=instrument, obs_group=self.obs_group(),
                reference=self._reference(sources), **self.audit)
            header.groups.add(self.public_group())
            info = TransientPhotometryCatalogInfo.objects.create(photometry=header, entry=entry, **self.audit)
            info.sources.set(sources)
            TransientPhotData.objects.bulk_create([
                TransientPhotData(
                    photometry=header, band=self.band(instrument, p['band']), mag_sys=self.mag_system(p['mag_sys']),
                    obs_date=osc.mjd_to_datetime(p['mjd']), mag=p['mag'], mag_err=p['mag_err'], flux=p['flux'],
                    flux_err=p['flux_err'], flux_zero_point=p['flux_zero_point'],
                    quality_mask=mask_from_flags(p['quality_flags'], PhotFlag), **self.audit)
                for p, _, _ in keep], batch_size=BULK_BATCH)
            # MySQL returns no ids from bulk_create; the header is new, so its points in id order are ours.
            ids = list(TransientPhotData.objects.filter(photometry=header).order_by('id').values_list('id', flat=True))
            if len(ids) != len(keep):
                raise EntryNotImported('%s: wrote %d points but found %d' % (plan['entry_name'], len(keep), len(ids)))
            TransientPhotDataCatalogInfo.objects.bulk_create([
                TransientPhotDataCatalogInfo(phot_data_id=pk, **{f: p['info'].get(f) for f in osc.PHOT_INFO_FIELDS})
                for pk, (p, _, _) in zip(ids, keep)], batch_size=BULK_BATCH)
            for pk, (p, decision, m) in zip(ids, keep):
                if m is not None:
                    matches.append(CatalogDataMatch(
                        entry=entry, existing_phot_data_id=m.existing.key, catalog_phot_data_id=pk, decision=decision,
                        weak=m.weak, time_difference=m.time_difference))
                    if decision == matching.SUPERSEDED_EXISTING:
                        supersede.append(matches[-1])
        if supersede:
            masks = dict(TransientPhotData.objects.filter(pk__in=[x.existing_phot_data_id for x in supersede])
                         .values_list('id', 'quality_mask'))
            for x in supersede:
                x.previous_quality_mask = masks.get(x.existing_phot_data_id)
                TransientPhotData.objects.filter(pk=x.existing_phot_data_id).update(
                    quality_mask=(x.previous_quality_mask or 0) | PHOT_REJECTED)
        CatalogDataMatch.objects.bulk_create(matches, batch_size=BULK_BATCH)
        return dict(counts)

    def _write_spectra(self, entry, transient, plan, by_alias, fits) -> dict:
        from collections import Counter

        decisions = self.planner.spectrum_decisions(plan, transient)
        counts = Counter()
        for i, s in enumerate(plan['spectra']):
            decision, m = decisions[i]
            counts[decision] += 1
            if decision == matching.KEPT_EXISTING:
                CatalogDataMatch.objects.create(
                    entry=entry, existing_spectrum_id=m.existing.key, decision=decision, weak=m.weak,
                    time_difference=m.time_difference,
                    catalog_record={'mjd': s['mjd'], 'n_pixels': len(s['pixels']),
                                    'info': {k: v for k, v in s['info'].items() if v is not None},
                                    'source_aliases': list(s['source_aliases'])})
                continue
            instrument = self.planner.spectrum_instrument(s)
            sources = self._sources_for(s['source_aliases'], by_alias)
            mask = apply_wavelength_medium(mask_from_flags(s['quality_flags'], SpecFlag), s['wavelength_medium'])
            spectrum = TransientSpectrum(
                transient=transient, instrument=instrument, obs_group=self.obs_group(),
                unit=self.unit(s['info'].get('u_fluxes')), ra=transient.ra, dec=transient.dec,
                obs_date=osc.mjd_to_datetime(s['mjd']), redshift=s['redshift'],
                wavelength_medium=s['wavelength_medium'], host_subtraction=s['host_subtraction'],
                quality_mask=mask, reference=self._reference(sources), **self.audit)
            # save_base(raw=True): the per-spectrum signals (favourite-transient notices, follow-up
            # success, program links) are for new observations, not a historical import. A raw save
            # skips auto_now and Spectrum.save (its wavelength-medium bit is set above).
            spectrum.created_date = spectrum.modified_date = timezone.now()
            spectrum.save_base(raw=True)
            spectrum.groups.add(self.public_group())
            info = TransientSpectrumCatalogInfo.objects.create(
                spectrum=spectrum, entry=entry, model_fit=fits.get(s['model_alias']),
                **{f: s['info'].get(f) for f in osc.SPEC_INFO_FIELDS}, **self.audit)
            info.sources.set(sources)
            spectrum_arrays.save(spectrum, [p[0] for p in s['pixels']], [p[1] for p in s['pixels']],
                                 [p[2] for p in s['pixels']])
            if m is not None:
                previous = None
                if decision == matching.SUPERSEDED_EXISTING:
                    previous = TransientSpectrum.objects.filter(pk=m.existing.key).values_list('quality_mask', flat=True).first()
                    TransientSpectrum.objects.filter(pk=m.existing.key).update(quality_mask=(previous or 0) | SPEC_REJECTED)
                CatalogDataMatch.objects.create(
                    entry=entry, existing_spectrum_id=m.existing.key, catalog_spectrum=spectrum, decision=decision,
                    weak=m.weak, time_difference=m.time_difference, previous_quality_mask=previous)
        return dict(counts)

