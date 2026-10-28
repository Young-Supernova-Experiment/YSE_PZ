"""
Data imported from external transient catalogues, first the Open Supernova Catalog (OSC).

The OSC schema (https://github.com/astrocatalogs/schema) keeps every value with its sources:
several redshifts, types or host names per object, each with an error, unit, kind and citation.
YSE_PZ keeps one "best" value per field, so the catalogue's values live here, and the importer
(``YSE_App.catalog_import``) also fills empty ``Transient`` fields from them.

Photometry and spectra go into the usual tables (``TransientPhotometry`` / ``TransientPhotData``,
``TransientSpectrum`` / ``TransientSpecData``) so the pages and plots see them. Each of those rows
gets a one-to-one ``*CatalogInfo`` row with the catalogue fields our tables have no column for
(count rates, X-ray and radio fluxes, asymmetric errors, correction flags, units, ...).

Columns are named after the OSC keys. ``extra`` holds any key the importer has no column for,
so nothing in an entry is dropped.
"""

from django.db import models

from YSE_App.models.base import BaseModel
from YSE_App.models.fields import JSONTextField
from YSE_App.models.instrument_models import Instrument
from YSE_App.models.phot_models import TransientPhotData, TransientPhotometry
from YSE_App.models.spectra_models import TransientSpectrum
from YSE_App.models.transient_models import Transient


class CatalogSource(BaseModel):
    """A paper, survey or website cited by catalogue data (an OSC ``sources`` row).

    Shared by every entry that cites it: matched on bibcode, else DOI, else arXiv id, else
    name and URL.
    """

    name = models.CharField(max_length=512)
    bibcode = models.CharField(max_length=32, null=True, blank=True, db_index=True)
    doi = models.CharField(max_length=255, null=True, blank=True)
    arxivid = models.CharField(max_length=64, null=True, blank=True)
    url = models.CharField(max_length=1024, null=True, blank=True)
    reference = models.CharField(max_length=512, null=True, blank=True)
    acknowledgment = models.TextField(null=True, blank=True)
    # Second-hand: the source collected the data from another one (an aggregator).
    secondary = models.BooleanField(null=True, blank=True)
    private = models.BooleanField(null=True, blank=True)
    # A refereed, published paper (from the bibcode's journal; editable). Data from refereed papers
    # supersede matching YSE data on import (#888).
    refereed = models.BooleanField(null=True, blank=True)
    extra = JSONTextField(null=True)

    def __str__(self):
        return self.reference or self.name


class CatalogEntry(BaseModel):
    """One imported catalogue entry (e.g. the OSC file ``SN2011fe.json``) and the transient it went to."""

    transient = models.ForeignKey(Transient, on_delete=models.CASCADE, related_name='catalog_entries')
    catalog = models.CharField(max_length=32)
    entry_name = models.CharField(max_length=128)
    schema = models.CharField(max_length=512, null=True, blank=True)
    # Where the entry was read from (repository and path, or URL), and its version if known.
    origin = models.CharField(max_length=512, null=True, blank=True)
    origin_version = models.CharField(max_length=64, null=True, blank=True)
    sources = models.ManyToManyField(CatalogSource, through='CatalogEntrySource', blank=True)
    # Top-level keys of the entry that have no mapping.
    extra = JSONTextField(null=True)

    # The import created the transient, with these values (name, ra, dec, class, ...); the TNS
    # check (#889) changes a field only while it still has the imported value.
    created_transient = models.BooleanField(default=False)
    import_values = JSONTextField(null=True)
    TNS_PENDING, TNS_UPDATED, TNS_NOT_FOUND, TNS_CONFLICT, TNS_SKIPPED, TNS_ERROR = (
        'pending', 'updated', 'not_found', 'conflict', 'skipped', 'error')
    TNS_STATUS_CHOICES = [(s, s) for s in (TNS_PENDING, TNS_UPDATED, TNS_NOT_FOUND, TNS_CONFLICT, TNS_SKIPPED, TNS_ERROR)]
    tns_status = models.CharField(max_length=16, choices=TNS_STATUS_CHOICES, null=True, blank=True, db_index=True)
    tns_name = models.CharField(max_length=64, null=True, blank=True)
    tns_checked = models.DateTimeField(null=True, blank=True)
    tns_note = models.TextField(null=True, blank=True)
    # Host comparison (#893): {"status": ..., "distance_mpc": ..., ...}; see the importer.
    host_check = JSONTextField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('catalog', 'entry_name'), name='yse_catalog_entry_unique'),
        ]

    def __str__(self):
        return '%s %s' % (self.catalog, self.entry_name)


class CatalogEntrySource(models.Model):
    """A source of an entry with the entry's own alias for it (OSC data cite sources as "1,3")."""

    entry = models.ForeignKey(CatalogEntry, on_delete=models.CASCADE, related_name='entry_sources')
    source = models.ForeignKey(CatalogSource, on_delete=models.CASCADE, related_name='entry_sources')
    alias = models.CharField(max_length=16)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('entry', 'alias'), name='yse_catalog_entry_source_alias'),
        ]


class CatalogModelFit(BaseModel):
    """A model fit to the entry's data (OSC ``models``, e.g. MOSFiT): its outputs, not its chains (#892).

    ``parameter_summary``: ``{name: {"median", "p16", "p84", "n", "latex"}}`` over the realizations.
    ``light_curve``: ``[{"band", "telescope", "instrument", "system", "time": [...], "median": [...],
    "p16": [...], "p84": [...], "n": [...]}]``, magnitudes over the realizations at each time.
    The realizations themselves are not stored.
    """

    entry = models.ForeignKey(CatalogEntry, on_delete=models.CASCADE, related_name='model_fits')
    alias = models.CharField(max_length=16, null=True, blank=True)
    code = models.CharField(max_length=128, null=True, blank=True)
    name = models.CharField(max_length=256, null=True, blank=True)
    version = models.CharField(max_length=64, null=True, blank=True)
    date = models.CharField(max_length=32, null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    steps = models.CharField(max_length=32, null=True, blank=True)
    convergence = JSONTextField(null=True)
    score = JSONTextField(null=True)
    setup = JSONTextField(null=True)
    n_realizations = models.PositiveIntegerField(null=True, blank=True)
    parameter_summary = JSONTextField(null=True)
    light_curve = JSONTextField(null=True)
    sources = models.ManyToManyField(CatalogSource, blank=True, related_name='model_fits')
    extra = JSONTextField(null=True)

    def __str__(self):
        return '%s %s' % (self.code or 'model', self.name or self.alias)


class TransientCatalogQuantity(BaseModel):
    """One sourced value of a catalogue quantity (OSC ``redshift``, ``claimedtype``, ``host``, ...).

    ``key`` is the OSC key. ``value`` is kept as given (the OSC stores numbers as strings to keep
    their precision; lists are stored as JSON); ``value_float`` is the parsed number, if it is one.
    """

    entry = models.ForeignKey(CatalogEntry, on_delete=models.CASCADE, related_name='quantities')
    transient = models.ForeignKey(Transient, on_delete=models.CASCADE, related_name='catalog_quantities')
    key = models.CharField(max_length=32)
    value = models.TextField(null=True, blank=True)
    value_float = models.FloatField(null=True, blank=True)
    e_value = models.FloatField(null=True, blank=True)
    e_lower_value = models.FloatField(null=True, blank=True)
    e_upper_value = models.FloatField(null=True, blank=True)
    u_value = models.CharField(max_length=64, null=True, blank=True)
    u_e_value = models.CharField(max_length=64, null=True, blank=True)
    kind = models.CharField(max_length=128, null=True, blank=True)
    derived = models.BooleanField(null=True, blank=True)
    upperlimit = models.BooleanField(null=True, blank=True)
    lowerlimit = models.BooleanField(null=True, blank=True)
    probability = models.FloatField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    correlations = JSONTextField(null=True)
    model_fit = models.ForeignKey(CatalogModelFit, null=True, blank=True, on_delete=models.CASCADE, related_name='quantities')
    realization = models.CharField(max_length=16, null=True, blank=True)
    sources = models.ManyToManyField(CatalogSource, blank=True, related_name='quantities')
    extra = JSONTextField(null=True)

    class Meta:
        indexes = [
            models.Index(fields=['transient', 'key'], name='yse_catalog_qty_key_idx'),
        ]

    def __str__(self):
        return '%s %s = %s' % (self.transient.name, self.key, self.value)


class TransientPhotometryCatalogInfo(BaseModel):
    """Where a photometry set written by a catalogue import came from (its entry and sources)."""

    photometry = models.OneToOneField(TransientPhotometry, on_delete=models.CASCADE, related_name='catalog_info')
    entry = models.ForeignKey(CatalogEntry, on_delete=models.CASCADE, related_name='photometry_sets')
    sources = models.ManyToManyField(CatalogSource, blank=True, related_name='photometry_sets')


class TransientPhotDataCatalogInfo(models.Model):
    """The catalogue fields of one photometry point that ``TransientPhotData`` has no column for.

    No audit columns: a large import writes millions of these, and the point's own row has them.
    Units (``u_*``) are as given; times are MJD.
    """

    phot_data = models.OneToOneField(
        TransientPhotData, on_delete=models.CASCADE, primary_key=True, related_name='catalog_info')

    # Time: a start/stop pair, its errors, and the unit if it was not MJD.
    time_start = models.FloatField(null=True, blank=True)
    time_end = models.FloatField(null=True, blank=True)
    e_time = models.FloatField(null=True, blank=True)
    e_lower_time = models.FloatField(null=True, blank=True)
    e_upper_time = models.FloatField(null=True, blank=True)
    u_time = models.CharField(max_length=16, null=True, blank=True)

    # Where and how it was observed, as written in the catalogue.
    band = models.CharField(max_length=64, null=True, blank=True)
    bandset = models.CharField(max_length=64, null=True, blank=True)
    system = models.CharField(max_length=64, null=True, blank=True)
    telescope = models.CharField(max_length=128, null=True, blank=True)
    instrument = models.CharField(max_length=128, null=True, blank=True)
    observatory = models.CharField(max_length=128, null=True, blank=True)
    survey = models.CharField(max_length=128, null=True, blank=True)
    mode = models.CharField(max_length=128, null=True, blank=True)
    observer = models.TextField(null=True, blank=True)
    reducer = models.TextField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)

    # Magnitudes. The magnitude itself is TransientPhotData.mag; its error is mag_err except for
    # limits, whose error stays here so the light curve does not count them as detections.
    e_magnitude = models.FloatField(null=True, blank=True)
    e_lower_magnitude = models.FloatField(null=True, blank=True)
    e_upper_magnitude = models.FloatField(null=True, blank=True)
    zeropoint = models.FloatField(null=True, blank=True)
    upperlimit = models.BooleanField(null=True, blank=True)
    lowerlimit = models.BooleanField(null=True, blank=True)
    upperlimitsigma = models.FloatField(null=True, blank=True)

    # Counts, fluxes (X-ray), flux densities (radio) and luminosities.
    countrate = models.FloatField(null=True, blank=True)
    e_countrate = models.FloatField(null=True, blank=True)
    e_lower_countrate = models.FloatField(null=True, blank=True)
    e_upper_countrate = models.FloatField(null=True, blank=True)
    u_countrate = models.CharField(max_length=32, null=True, blank=True)
    flux = models.FloatField(null=True, blank=True)
    e_flux = models.FloatField(null=True, blank=True)
    e_lower_flux = models.FloatField(null=True, blank=True)
    e_upper_flux = models.FloatField(null=True, blank=True)
    u_flux = models.CharField(max_length=32, null=True, blank=True)
    unabsorbedflux = models.FloatField(null=True, blank=True)
    e_unabsorbedflux = models.FloatField(null=True, blank=True)
    e_lower_unabsorbedflux = models.FloatField(null=True, blank=True)
    e_upper_unabsorbedflux = models.FloatField(null=True, blank=True)
    fluxdensity = models.FloatField(null=True, blank=True)
    e_fluxdensity = models.FloatField(null=True, blank=True)
    e_lower_fluxdensity = models.FloatField(null=True, blank=True)
    e_upper_fluxdensity = models.FloatField(null=True, blank=True)
    u_fluxdensity = models.CharField(max_length=32, null=True, blank=True)
    luminosity = models.FloatField(null=True, blank=True)
    e_luminosity = models.FloatField(null=True, blank=True)
    e_lower_luminosity = models.FloatField(null=True, blank=True)
    e_upper_luminosity = models.FloatField(null=True, blank=True)
    u_luminosity = models.CharField(max_length=32, null=True, blank=True)

    # Spectral coordinate of the measurement.
    energy_low = models.FloatField(null=True, blank=True)
    energy_high = models.FloatField(null=True, blank=True)
    u_energy = models.CharField(max_length=32, null=True, blank=True)
    frequency = models.FloatField(null=True, blank=True)
    u_frequency = models.CharField(max_length=32, null=True, blank=True)
    wavelength = models.FloatField(null=True, blank=True)
    u_wavelength = models.CharField(max_length=32, null=True, blank=True)

    # X-ray reduction and exposure.
    photonindex = models.FloatField(null=True, blank=True)
    nhmw = models.FloatField(null=True, blank=True)
    exposuretime = models.FloatField(null=True, blank=True)
    u_exposuretime = models.CharField(max_length=32, null=True, blank=True)
    offaxisangle = models.FloatField(null=True, blank=True)
    u_offaxisangle = models.CharField(max_length=32, null=True, blank=True)
    extractionradius = models.FloatField(null=True, blank=True)
    u_extractionradius = models.CharField(max_length=32, null=True, blank=True)
    airmass = models.FloatField(null=True, blank=True)

    # What was done to the value.
    kcorrected = models.BooleanField(null=True, blank=True)
    scorrected = models.BooleanField(null=True, blank=True)
    mcorrected = models.BooleanField(null=True, blank=True)
    synthetic = models.BooleanField(null=True, blank=True)
    simulated = models.BooleanField(null=True, blank=True)
    restframe = models.BooleanField(null=True, blank=True)
    host = models.BooleanField(null=True, blank=True)
    includeshost = models.BooleanField(null=True, blank=True)
    hostnhcorr = models.BooleanField(null=True, blank=True)

    extra = JSONTextField(null=True)


class TransientSpectrumCatalogInfo(BaseModel):
    """The catalogue fields of one spectrum that ``TransientSpectrum`` has no column for."""

    spectrum = models.OneToOneField(TransientSpectrum, on_delete=models.CASCADE, related_name='catalog_info')
    entry = models.ForeignKey(CatalogEntry, on_delete=models.CASCADE, related_name='spectra')
    sources = models.ManyToManyField(CatalogSource, blank=True, related_name='spectra')

    # The catalogue gave no time: obs_date is a stand-in (see the importer).
    time_missing = models.BooleanField(null=True, blank=True)
    time_start = models.FloatField(null=True, blank=True)
    time_end = models.FloatField(null=True, blank=True)
    e_time = models.FloatField(null=True, blank=True)
    e_lower_time = models.FloatField(null=True, blank=True)
    e_upper_time = models.FloatField(null=True, blank=True)
    u_time = models.CharField(max_length=16, null=True, blank=True)

    u_wavelengths = models.CharField(max_length=64, null=True, blank=True)
    u_fluxes = models.CharField(max_length=64, null=True, blank=True)
    u_errors = models.CharField(max_length=64, null=True, blank=True)

    telescope = models.CharField(max_length=128, null=True, blank=True)
    instrument = models.CharField(max_length=128, null=True, blank=True)
    observatory = models.CharField(max_length=128, null=True, blank=True)
    survey = models.CharField(max_length=128, null=True, blank=True)
    observer = models.TextField(null=True, blank=True)
    reducer = models.TextField(null=True, blank=True)
    reduction = models.CharField(max_length=128, null=True, blank=True)
    filename = models.CharField(max_length=512, null=True, blank=True)
    airmass = models.FloatField(null=True, blank=True)
    snr = models.FloatField(null=True, blank=True)

    deredshifted = models.BooleanField(null=True, blank=True)
    dereddened = models.BooleanField(null=True, blank=True)
    vacuumwavelengths = models.BooleanField(null=True, blank=True)
    host = models.BooleanField(null=True, blank=True)
    includeshost = models.BooleanField(null=True, blank=True)
    # Wavelength ranges (Angstrom) the catalogue suggests leaving out: {"above": .., "below": .., "range": [..]}.
    exclude = JSONTextField(null=True)
    model_fit = models.ForeignKey(CatalogModelFit, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    realization = models.CharField(max_length=16, null=True, blank=True)

    extra = JSONTextField(null=True)


class CatalogDataMatch(models.Model):
    """A catalogue observation that matched one already in YSE_PZ, and what the import did (#888).

    Matched on time, instrument and filter (photometry) or time, instrument and wavelength range
    (spectra); ``weak`` when the catalogue gave no instrument YSE knows. Decisions:

    * ``kept_existing``: the catalogue copy was not imported (``catalog_record`` holds it);
    * ``superseded_existing``: the catalogue copy comes from a refereed paper; it was imported and
      the existing point or spectrum was flagged REJECTED (``previous_quality_mask`` restores it);
    * ``review``: imported next to the existing one, for a person to decide (weak match from a
      refereed paper).
    """

    KEPT_EXISTING, SUPERSEDED_EXISTING, REVIEW = 'kept_existing', 'superseded_existing', 'review'
    DECISION_CHOICES = [(d, d) for d in (KEPT_EXISTING, SUPERSEDED_EXISTING, REVIEW)]

    entry = models.ForeignKey(CatalogEntry, on_delete=models.CASCADE, related_name='matches')
    existing_phot_data = models.ForeignKey(TransientPhotData, null=True, blank=True, on_delete=models.CASCADE,
                                           related_name='catalog_matches')
    existing_spectrum = models.ForeignKey(TransientSpectrum, null=True, blank=True, on_delete=models.CASCADE,
                                          related_name='catalog_matches')
    catalog_phot_data = models.ForeignKey(TransientPhotData, null=True, blank=True, on_delete=models.CASCADE,
                                          related_name='+')
    catalog_spectrum = models.ForeignKey(TransientSpectrum, null=True, blank=True, on_delete=models.CASCADE,
                                         related_name='+')
    decision = models.CharField(max_length=24, choices=DECISION_CHOICES)
    weak = models.BooleanField(default=False)
    time_difference = models.FloatField(null=True, blank=True)
    previous_quality_mask = models.PositiveBigIntegerField(null=True, blank=True)
    catalog_record = JSONTextField(null=True)
    created_date = models.DateTimeField(auto_now_add=True)


class CatalogImportAttempt(models.Model):
    """The last download, parse and ingest outcome of one catalogue entry, for logging and retries (#895)."""

    STAGE_DOWNLOAD, STAGE_PARSE, STAGE_INGEST = 'download', 'parse', 'ingest'
    STAGE_CHOICES = [(s, s) for s in (STAGE_DOWNLOAD, STAGE_PARSE, STAGE_INGEST)]
    OK, FAILED = 'ok', 'failed'

    catalog = models.CharField(max_length=32)
    entry_key = models.CharField(max_length=128)
    # Where to get it again: "<repository>/<path>" or a local path.
    origin = models.CharField(max_length=512, null=True, blank=True)
    stage = models.CharField(max_length=16, choices=STAGE_CHOICES)
    status = models.CharField(max_length=8, choices=[(OK, OK), (FAILED, FAILED)], db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    error = models.TextField(null=True, blank=True)
    first_attempt = models.DateTimeField(auto_now_add=True)
    last_attempt = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('catalog', 'entry_key'), name='yse_catalog_attempt_unique'),
        ]


class CatalogInstrumentAlias(BaseModel):
    """A catalogue telescope / instrument string and the YSE instrument it means (#894).

    Loaded from ``catalog_import/osc_instruments.json`` by ``manage.py catalog_instruments``; may be
    edited by hand. ``telescope`` / ``instrument`` are the strings as written in the catalogue
    (blank matches a missing value); ``mjd_min`` / ``mjd_max`` limit it in time (e.g. P48 with
    CFH12k for PTF, then ZTF-Cam).
    """

    catalog = models.CharField(max_length=32, default='OSC')
    telescope = models.CharField(max_length=128, blank=True, default='')
    instrument = models.CharField(max_length=128, blank=True, default='')
    mjd_min = models.FloatField(null=True, blank=True)
    mjd_max = models.FloatField(null=True, blank=True)
    yse_instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE, related_name='catalog_aliases')

    class Meta:
        indexes = [models.Index(fields=['catalog', 'telescope', 'instrument'], name='yse_catalog_inst_alias_idx')]
