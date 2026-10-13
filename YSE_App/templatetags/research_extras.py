"""Template helpers for the researcher profile (#627)."""

from django import template

from YSE_App.services import research_crypto

register = template.Library()


@register.simple_tag
def research_key_missing():
    """True when no usable research public key is configured."""
    return not research_crypto.key_available()
