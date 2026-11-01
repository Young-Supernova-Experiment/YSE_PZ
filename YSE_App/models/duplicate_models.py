"""
What the duplicate clean-up (#890, ``manage.py clean_duplicate_data``) did, so it can be undone.

One row per point or spectrum the clean-up flagged REJECTED as a copy of another one of the same
transient (a repeat upload, or our own report that came back in a TNS import). Only the
``quality_mask`` of the flagged row changes; ``--revert`` clears the REJECTED bit again where
``previous_quality_mask`` did not have it.
"""

from django.db import models

from YSE_App.models.phot_models import TransientPhotData
from YSE_App.models.spectra_models import TransientSpectrum
from YSE_App.models.transient_models import Transient


class DuplicateDataDecision(models.Model):
    """A point or spectrum flagged as a duplicate, the copy kept instead, and why that one was kept.

    Reasons: ``tns_copy`` (the flagged copy came back from TNS, the kept one is ours),
    ``no_errors`` (the kept copy has errors, the flagged one has none) and ``later_upload``
    (otherwise the earliest upload is kept).
    """

    PHOTOMETRY, SPECTRUM = 'photometry', 'spectrum'
    KIND_CHOICES = [(k, k) for k in (PHOTOMETRY, SPECTRUM)]
    TNS_COPY, NO_ERRORS, LATER_UPLOAD = 'tns_copy', 'no_errors', 'later_upload'
    REASON_CHOICES = [(r, r) for r in (TNS_COPY, NO_ERRORS, LATER_UPLOAD)]

    transient = models.ForeignKey(Transient, on_delete=models.CASCADE, related_name='duplicate_decisions')
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    phot_data = models.ForeignKey(TransientPhotData, null=True, blank=True, on_delete=models.CASCADE,
                                  related_name='duplicate_decisions')
    spectrum = models.ForeignKey(TransientSpectrum, null=True, blank=True, on_delete=models.CASCADE,
                                 related_name='duplicate_decisions')
    kept_phot_data = models.ForeignKey(TransientPhotData, null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name='+')
    kept_spectrum = models.ForeignKey(TransientSpectrum, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name='+')
    reason = models.CharField(max_length=16, choices=REASON_CHOICES)
    # Either copy has no instrument YSE knows (matched on time and filter or wavelength range only).
    weak = models.BooleanField(default=False)
    time_difference = models.FloatField(null=True, blank=True)
    previous_quality_mask = models.PositiveBigIntegerField()
    created_date = models.DateTimeField(auto_now_add=True)
    reverted_date = models.DateTimeField(null=True, blank=True)
