"""
Write an OSC import plan (``osc.parse_entry``) to the database.

``preview_entry`` only reads: which transient the entry would go to and which instruments its
data would use. ``apply_entry`` writes one entry in one transaction:

* The transient: the one the entry was imported to before, else a transient named like the entry
  or one of its aliases, else one within ``match_radius`` arcsec, else a new one (status
  ``new_status``). Only empty fields of an existing transient are filled, except the redshift,
  which follows the redshift precedence list (``OSC`` ranks as another catalogue; see
  docs/redshift-precedence.md). Redshift, classification and host-link changes go in the change log.
* Re-importing an entry first deletes what the last import of it wrote (its photometry sets,
  spectra, quantities and model fits); nothing else on the transient is touched.
* Photometry and spectra get the ``OSC`` observation group and the Public group (OSC data are
  published). Instruments are matched by name; data from instruments YSE does not know go to the
  ``Unknown`` instrument, with the band kept by name and the catalogue's telescope / instrument
  strings in the ``*CatalogInfo`` row.

Import is historical: automatic publishing to TNS and the minor-planet screen are switched off
for transients created here, and photometry and spectra are written with ``bulk_create`` (no
per-row signals; the photometry statistics are recomputed once per transient).
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional, Tuple

from django.contrib.auth.models import Group
from django.db import transaction
from django.db.models import Q
from django.test.utils import override_settings
from django.utils import timezone

from YSE_App.catalog_import import osc
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
    TransientSpecData,
    TransientSpectrum,
    TransientStatus,
    Unit,
)
from YSE_App.models.catalog_models import (
    CatalogEntry,
    CatalogEntrySource,
    CatalogModelFit,
    CatalogModelRealization,
    CatalogSource,
    TransientCatalogQuantity,
    TransientPhotDataCatalogInfo,
    TransientPhotometryCatalogInfo,
    TransientSpectrumCatalogInfo,
)
from YSE_App.services import change_log, photstat, redshift_precedence

logger = logging.getLogger(__name__)

UNKNOWN = 'Unknown'
BULK_BATCH = 2000
DEFAULT_MATCH_RADIUS_ARCSEC = 2.0
HOST_MATCH_RADIUS_ARCSEC = 3.0
MAX_REFERENCE_LENGTH = 512
# Change-log service name of this importer.
SERVICE = 'osc-import'


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
    names = [t['name'], plan['entry_name']]
    for alias in t['aliases']:
        names.extend([alias, osc.yse_name(alias)])
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def match_transient(plan: dict, match_radius: float = DEFAULT_MATCH_RADIUS_ARCSEC) -> Tuple[Optional[Transient], str]:
    """The existing transient for this entry, and how it was found ('' when there is none)."""
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


class InstrumentResolver:
    """Catalogue telescope / instrument strings -> YSE Instrument (cached); ``Unknown`` otherwise."""

    def __init__(self):
        self._cache: Dict[tuple, Instrument] = {}
        self._unknown = None

    def unknown(self) -> Instrument:
        if self._unknown is None:
            self._unknown = Instrument.objects.filter(name=UNKNOWN).order_by('id').first()
            if self._unknown is None:
                raise EntryNotImported("no instrument named 'Unknown'; it is needed for data from unknown instruments")
        return self._unknown

    def resolve(self, telescope: Optional[str], instrument: Optional[str]) -> Instrument:
        key = (telescope or '', instrument or '')
        if key not in self._cache:
            self._cache[key] = self._lookup(telescope, instrument) or self.unknown()
        return self._cache[key]

    @staticmethod
    def _lookup(telescope, instrument) -> Optional[Instrument]:
        if instrument:
            qs = Instrument.objects.filter(name__iexact=instrument)
            if telescope:
                on_telescope = qs.filter(telescope__name__iexact=telescope)
                if on_telescope.exists():
                    qs = on_telescope
            if qs.count() == 1:
                return qs.first()
        return None


def preview_entry(plan: dict, match_radius: float = DEFAULT_MATCH_RADIUS_ARCSEC) -> dict:
    """What ``apply_entry`` would do, from reads only."""
    transient, how = match_transient(plan, match_radius)
    resolver = InstrumentResolver()
    instruments = {}
    for item in plan['photometry_sets'] + plan['spectra']:
        try:
            inst = resolver.resolve(item['telescope'], item['instrument'])
            label = inst.name
        except EntryNotImported:
            label = '(no Unknown instrument)'
        instruments['%s / %s' % (item['telescope'] or '-', item['instrument'] or '-')] = label
    t = plan['transient']
    action = 'update %s (matched by %s)' % (transient.name, how) if transient else (
        'create %s' % t['name'] if t['ra'] is not None and t['dec'] is not None else 'skip: no match and no position')
    return {'action': action, 'transient': transient, 'instruments': instruments}


# ---- writing ---------------------------------------------------------------------------------

class Writer:
    """Applies plans for one user; caches the lookups shared by every entry."""

    def __init__(self, user, *, new_status: str = 'Ignore', match_radius: float = DEFAULT_MATCH_RADIUS_ARCSEC,
                 with_spectra: bool = True, with_models: bool = True):
        self.user = user
        self.audit = {'created_by': user, 'modified_by': user}
        self.new_status_name = new_status
        self.match_radius = match_radius
        self.with_spectra = with_spectra
        self.with_models = with_models
        self.instruments = InstrumentResolver()
        self._bands: Dict[tuple, PhotometricBand] = {}
        self._mag_systems: Dict[str, Optional[MagSystem]] = {}
        self._units: Dict[str, Optional[Unit]] = {}
        self._sources: Dict[tuple, CatalogSource] = {}
        self._obs_group = None
        self._public = None
        self._status = None

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
                unknown = self.instruments.unknown()
                band = PhotometricBand.objects.filter(instrument=unknown, name=name).order_by('id').first()
                if band is None:
                    band = PhotometricBand.objects.create(instrument=unknown, name=name, **self.audit)
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
        except Exception:
            # Rows cached during the rolled-back entry (a new band, source or group) no longer exist.
            self._forget()
            raise

    def _forget(self) -> None:
        self.instruments = InstrumentResolver()
        self._bands.clear()
        self._mag_systems.clear()
        self._units.clear()
        self._sources.clear()
        self._obs_group = self._public = self._status = None

    def _apply(self, plan: dict) -> dict:
        transient, how = match_transient(plan, self.match_radius)
        created = transient is None
        if created:
            transient = self._create_transient(plan)
        else:
            self._fill_transient(transient, plan)
        t = plan['transient']
        if t['redshift'] is not None:
            redshift_precedence.apply_redshift(transient, t['redshift'], t['redshift_err'], osc.CATALOG,
                                               user=self.user, service=SERVICE)
        old = CatalogEntry.objects.filter(catalog=plan['catalog'], entry_name=plan['entry_name']).first()
        if old is not None:
            self._delete_entry_data(old)
        entry = CatalogEntry.objects.create(
            transient=transient, catalog=plan['catalog'], entry_name=plan['entry_name'][:128],
            schema=plan['schema'], origin=(plan['origin'] or '')[:512] or None, extra=plan['extra'], **self.audit)

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
        self._attach_host(transient, plan)
        fits = self._write_models(entry, plan, by_alias) if self.with_models else {}
        n_quantities = self._write_quantities(entry, transient, plan, by_alias, fits)
        n_points = self._write_photometry(entry, transient, plan, by_alias)
        n_spectra = self._write_spectra(entry, transient, plan, by_alias, fits) if self.with_spectra else 0
        photstat.schedule_recompute(transient.id)
        return {
            'transient': transient.name, 'created': created, 'matched_by': how, 'entry_id': entry.id,
            'quantities': n_quantities, 'photometry_points': n_points, 'spectra': n_spectra,
            'model_fits': len(fits),
        }

    def _create_transient(self, plan: dict) -> Transient:
        t = plan['transient']
        if t['ra'] is None or t['dec'] is None:
            raise EntryNotImported('%s: no transient matches it and it has no position' % plan['entry_name'])
        transient = Transient(
            name=t['name'][:64], ra=t['ra'], dec=t['dec'], status=self.new_status(), obs_group=self.obs_group(),
            **self.audit)
        self._set_fields(transient, t, only_empty=False)
        transient.save()
        return transient

    def _fill_transient(self, transient: Transient, plan: dict) -> None:
        changed = self._set_fields(transient, plan['transient'], only_empty=True)
        if changed:
            transient.save(update_fields=changed + ['modified_by', 'modified_date'])
        if 'best_spec_class' in changed:
            change_log.record_change(transient, 'best_spec_class', None, transient.best_spec_class.name,
                                     service=SERVICE, category='classification', source=osc.CATALOG)

    def _set_fields(self, transient: Transient, t: dict, only_empty: bool) -> List[str]:
        wanted = {
            'disc_date': t['disc_date'] if t['disc_date_precision'] == 'day' else None,
            'mw_ebv': t['mw_ebv'],
            'abs_mag_peak': t['abs_mag_peak'],
            'abs_mag_peak_date': t['abs_mag_peak_date'],
        }
        if t['spec_class']:
            cls = TransientClass.objects.filter(name=t['spec_class']).order_by('id').first() or \
                TransientClass.objects.filter(name__iexact=t['spec_class']).order_by('id').first()
            if cls is not None:
                wanted['best_spec_class'] = cls
        changed = []
        for field, value in wanted.items():
            if value is None:
                continue
            current = getattr(transient, field + '_id' if field == 'best_spec_class' else field)
            if only_empty and current not in (None, ''):
                continue
            setattr(transient, field, value)
            changed.append(field)
        if changed:
            transient.modified_by = self.user
        return changed

    def _add_alternate_names(self, transient: Transient, plan: dict) -> None:
        names = [n for n in [plan['entry_name']] + plan['transient']['aliases'] if n and n != transient.name]
        existing = set(AlternateTransientNames.objects.filter(transient=transient).values_list('name', flat=True))
        for name in dict.fromkeys(names):
            if name[:64] not in existing:
                AlternateTransientNames.objects.create(
                    transient=transient, name=name[:64], obs_group=self.obs_group(),
                    description='Open Supernova Catalog alias', **self.audit)

    def _attach_host(self, transient: Transient, plan: dict) -> None:
        h = plan['transient']['host']
        if transient.host_id or h['ra'] is None or h['dec'] is None:
            return
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

    def _delete_entry_data(self, entry: CatalogEntry) -> None:
        """What the last import of this entry wrote; the CatalogEntry cascade takes quantities and model fits."""
        TransientPhotometry.objects.filter(catalog_info__entry=entry).delete()
        TransientSpectrum.objects.filter(catalog_info__entry=entry).delete()
        entry.delete()

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
                **{f: m.get(f) for f in osc.MODEL_JSON_FIELDS}, extra=m.get('extra'), **self.audit)
            fit.sources.set(self._sources_for(m['source_aliases'], by_alias))
            CatalogModelRealization.objects.bulk_create([
                CatalogModelRealization(
                    model_fit=fit, alias=r['alias'], score=r['score'], weight=r['weight'],
                    parameters=r['parameters'], photometry=r['photometry'] or None, extra=r['extra'], **self.audit)
                for r in m['realizations'].values()], batch_size=200)
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

    def _write_photometry(self, entry, transient, plan, by_alias) -> int:
        total = 0
        for phot_set in plan['photometry_sets']:
            instrument = self.instruments.resolve(phot_set['telescope'], phot_set['instrument'])
            sources = self._sources_for(phot_set['source_aliases'], by_alias)
            header = TransientPhotometry.objects.create(
                transient=transient, instrument=instrument, obs_group=self.obs_group(),
                reference=self._reference(sources), **self.audit)
            header.groups.add(self.public_group())
            info = TransientPhotometryCatalogInfo.objects.create(photometry=header, entry=entry, **self.audit)
            info.sources.set(sources)

            points = phot_set['points']
            TransientPhotData.objects.bulk_create([
                TransientPhotData(
                    photometry=header, band=self.band(instrument, p['band']), mag_sys=self.mag_system(p['mag_sys']),
                    obs_date=osc.mjd_to_datetime(p['mjd']), mag=p['mag'], mag_err=p['mag_err'], flux=p['flux'],
                    flux_err=p['flux_err'], flux_zero_point=p['flux_zero_point'],
                    quality_mask=mask_from_flags(p['quality_flags'], PhotFlag), **self.audit)
                for p in points], batch_size=BULK_BATCH)
            # MySQL returns no ids from bulk_create; the header is new, so its points in id order are ours.
            ids = list(TransientPhotData.objects.filter(photometry=header).order_by('id').values_list('id', flat=True))
            if len(ids) != len(points):
                raise EntryNotImported('%s: wrote %d points but found %d' % (plan['entry_name'], len(points), len(ids)))
            TransientPhotDataCatalogInfo.objects.bulk_create([
                TransientPhotDataCatalogInfo(phot_data_id=pk, **{f: p['info'].get(f) for f in osc.PHOT_INFO_FIELDS})
                for pk, p in zip(ids, points)], batch_size=BULK_BATCH)
            total += len(points)
        return total

    def _write_spectra(self, entry, transient, plan, by_alias, fits) -> int:
        for s in plan['spectra']:
            instrument = self.instruments.resolve(s['telescope'], s['instrument'])
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
            TransientSpecData.objects.bulk_create([
                TransientSpecData(spectrum=spectrum, wavelength=w, flux=f, flux_err=e, **self.audit)
                for w, f, e in s['pixels']], batch_size=BULK_BATCH)
        return len(plan['spectra'])

