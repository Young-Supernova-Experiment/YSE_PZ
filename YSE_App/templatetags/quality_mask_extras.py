"""Template helpers for decoded quality_mask bits (#85)."""

from django import template

from YSE_App.common.quality_mask import PhotFlag, flags_from_mask

register = template.Library()


@register.filter(name='phot_mask_flags')
def phot_mask_flags(mask):
    """Names of the photometry flags set in ``mask`` (least serious first)."""
    try:
        return flags_from_mask(mask, PhotFlag)
    except (TypeError, ValueError):
        return []
