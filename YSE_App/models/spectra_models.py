from django.db import models
from YSE_App.models.base import *
from YSE_App.models.enum_models import *
from YSE_App.models.instrument_models import *
from YSE_App.models.followup_models import *
from YSE_App.models.transient_models import *
from YSE_App.models.host_models import *
from django.contrib.auth.models import Group
from YSE_App.common.quality_mask import HostSubtraction, WavelengthMedium, apply_wavelength_medium

class Spectrum(BaseModel):
    class Meta:
        abstract = True

    ### Entity relationships ###
    # Required
    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE)
    obs_group = models.ForeignKey(ObservationGroup, on_delete=models.CASCADE)

    # Optional
    unit = models.ForeignKey(Unit, null=True, blank=True, on_delete=models.CASCADE)
    data_quality = models.ManyToManyField(DataQuality, blank=True)

    ### Properties ###
    # Required
    ra = models.FloatField()
    dec = models.FloatField()
    obs_date = models.DateTimeField()
    # Optional
    redshift = models.FloatField(null=True, blank=True)
    redshift_err = models.FloatField(null=True, blank=True)
    redshift_quality = models.BooleanField(null=True,blank=True)
    spec_plot_file = models.CharField(max_length=512, null=True, blank=True)
    spec_data_file = models.CharField(max_length=512, null=True, blank=True)
    spectrum_notes = models.TextField(null=True, blank=True)

    groups = models.ManyToManyField(Group, blank=True)
    reference = models.CharField(max_length=512, null=True, blank=True)

    # Data-quality bits, 0 = best (YSE_App/common/quality_mask.py, docs/quality-mask.md).
    # MySQL DEFAULT 0 too (migration 0036), so code that predates the column still inserts.
    quality_mask = models.PositiveBigIntegerField(default=0)
    # What was done; NULL (rows from code that predates the column) counts as unknown.
    # save() keeps WAVELENGTH_MEDIUM_UNKNOWN (bit 30) in step with it.
    wavelength_medium = models.CharField(
        max_length=16, choices=WavelengthMedium.choices, default=WavelengthMedium.UNKNOWN,
        null=True, blank=True)

    def save(self, *args, **kwargs):
        self.quality_mask = apply_wavelength_medium(self.quality_mask, self.wavelength_medium)
        update_fields = kwargs.get('update_fields')
        if update_fields is not None and 'wavelength_medium' in update_fields \
                and 'quality_mask' not in update_fields:
            kwargs['update_fields'] = list(update_fields) + ['quality_mask']
        super().save(*args, **kwargs)

class TransientSpectrum(Spectrum):
    ### Entity relationships ###
    # Required
    transient = models.ForeignKey(Transient, on_delete=models.CASCADE)
    # Optional
    followup = models.ForeignKey(TransientFollowup, null=True, blank=True, on_delete=models.SET_NULL) # Can by null if data is from external source
    spec_phase = models.FloatField(null=True, blank=True)

    ### Properties ###
    # Optional
    rlap = models.FloatField(null=True, blank=True)
    snid_plot_file = models.CharField(max_length=512, null=True, blank=True)
    host_subtraction = models.CharField(
        max_length=16, choices=HostSubtraction.choices, null=True, blank=True)

    # PIs of this dataset (#446); several are allowed. See YSE_App.services.data_pis.
    pis = models.ManyToManyField(
        'YSE_App.PrincipalInvestigator', blank=True, related_name='transient_spectra',
        help_text='Principal investigator(s) of this spectrum.')
    # The program (allocation) these data were taken under (#648); see TransientPhotometry.allocation.
    allocation = models.ForeignKey(
        'YSE_App.Allocation', null=True, blank=True, on_delete=models.SET_NULL, related_name='transient_spectra',
        help_text='Program (allocation) these data were taken under, if known.')

    def __str__(self):
        return 'Spectrum: %s - %s' % (self.transient.name, self.obs_date.strftime('%m/%d/%Y'))

class HostSpectrum(Spectrum):
    ### Entity relationships ###
    # Required
    host = models.ForeignKey(Host, on_delete=models.CASCADE)
    # Optional
    followup = models.ForeignKey(HostFollowup, null=True, blank=True, on_delete=models.SET_NULL) # Can by null if data is from external source

    ### Properties ###
    # Optional
    tdr = models.FloatField(null=True, blank=True)

    def __str__(self):
        return 'Spectrum: %s - %s' % (self.host.HostString(), self.obs_date.strftime('%m/%d/%Y'))


class SpecData(BaseModel):
    class Meta:
        abstract = True
    ### Properties ###
    # Required
    wavelength = models.FloatField()
    flux = models.FloatField()

    # Optional
    wavelength_err = models.FloatField(null=True, blank=True)
    flux_err = models.FloatField(null=True, blank=True)
    # Pixel flags (quality_mask.PixelFlag); any set bit excludes the pixel. MySQL DEFAULT 0 (0036).
    pixel_mask = models.PositiveSmallIntegerField(default=0)

class TransientSpecData(SpecData):
    ### Entity relationships ###
    # Required
    spectrum = models.ForeignKey(TransientSpectrum, on_delete=models.CASCADE)

    def __str__(self):
        return '%s - %s' % (self.wavelength, self.flux)

class HostSpecData(SpecData):
    ### Entity relationships ###
    # Required
    spectrum = models.ForeignKey(HostSpectrum, on_delete=models.CASCADE)

    def __str__(self):
        return '%s - %s' % (self.wavelength, self.flux)
