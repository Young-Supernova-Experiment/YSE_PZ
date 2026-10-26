"""Readable labels for analysis result keys on the Analysis tab (#816).

The result JSON keeps its machine keys (the run API returns them unchanged);
only the template shows these labels. Unknown keys fall back to the key.
"""
from django import template

register = template.Library()

RESULT_LABELS = {
    # shared
    "model": "Model",
    "method": "Fit method",
    "bands": "Bands",
    "reference_band": "Reference band",
    "n_points": "Data points",
    "chi2": "χ²",
    "dof": "Degrees of freedom",
    "ndof": "Degrees of freedom",
    "reduced_chi2": "χ²/dof",
    "t0": "t₀ (MJD)",
    "phase_today": "Phase today (days)",
    "z": "Redshift z",
    "z_source": "Redshift source",
    # Bazin
    "tau_rise": "τ_rise (days)",
    "tau_fall": "τ_fall (days)",
    "peak_mjd": "Peak (MJD)",
    "peak_mag": "Peak magnitude (mag)",
    "mag_today": "Magnitude today (mag)",
    "extrapolate_mjd": "Extrapolated to (MJD)",
    "mag_extrapolated": "Extrapolated magnitude (mag)",
    # sncosmo / SALT
    "x0": "x₀",
    "x1": "x₁ (stretch)",
    "c": "c (color)",
    "mB": "m_B (mag)",
    "amplitude": "Amplitude",
    "mwebv": "E(B−V)_MW (mag)",
    # NGSF
    "best_type": "Best type",
    "best_template": "Best template",
    "best_phase": "Best phase (days)",
    "best_z": "Best redshift z",
    "best_av": "Best A_V (mag)",
    "best_chi2_dof": "Best χ²/dof",
    "best_galaxy": "Best host template",
    "best_frac_sn": "Best SN fraction",
    "n_matches": "Matches",
    "matches": "Top matches",
    "spectrum_id": "Spectrum ID",
    "spectrum_mjd": "Spectrum date (MJD)",
    "spectrum_instrument": "Spectrum instrument",
    "z_start": "Redshift range start",
    "z_end": "Redshift range end",
}


@register.filter(name="analysis_label")
def analysis_label(key):
    """Readable label with units for a result key; unknown keys are shown as-is."""
    return RESULT_LABELS.get(key, key)
