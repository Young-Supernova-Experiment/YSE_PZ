"""``{% glossary_help 'term' %}``: a small "?" link to the glossary (#862).

It renders only while the UI preview is on (the ``ui_preview`` context
variable), so pages look the same with the preview off. ``term`` must be one of
``glossary_views.GLOSSARY_TERMS``; anything else renders nothing. The link is
built with ``reverse``, so it keeps the stack's script prefix.
"""

from django import template
from django.urls import reverse
from django.utils.html import format_html

from YSE_App.glossary_views import GLOSSARY_TERMS

register = template.Library()

#: Search-form field name -> glossary anchor, for the Transient Search filters.
SEARCH_FIELD_TERMS = {
    'status': 'status',
    'peak_mag_min': 'magnitudes',
    'peak_mag_max': 'magnitudes',
    'latest_mag_min': 'magnitudes',
    'latest_mag_max': 'magnitudes',
    'deepest_limit_min': 'magnitudes',
    'deepest_limit_max': 'magnitudes',
    'days_since_disc_max': 'phase',
}


@register.simple_tag(takes_context=True)
def glossary_help(context, term, label=''):
    if not context.get('ui_preview') or term not in GLOSSARY_TERMS:
        return ''
    title = 'What does %s mean?' % label if label else 'Glossary'
    return format_html(
        '<a class="yse-glossary-help" href="{}#{}" title="{}" aria-label="{}">?</a>',
        reverse('glossary'), term, title, title,
    )


@register.simple_tag(takes_context=True)
def glossary_help_for_field(context, field_name, label=''):
    term = SEARCH_FIELD_TERMS.get(field_name)
    return glossary_help(context, term, label) if term else ''
