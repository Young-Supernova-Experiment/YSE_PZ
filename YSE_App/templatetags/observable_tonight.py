""""Observable tonight from <site>" control for shortlists (#854, UI preview only).

Usage in a preview template::

    {% load observable_tonight %}
    {% observable_tonight_control id_selector=".yse-dash-select" scope="..." watch="#yse-dash2" %}

``id_selector`` matches one element per row whose ``value`` or
``data-transient-id`` is the transient id; ``scope`` (optional CSS selector)
limits the rows, ``watch`` (optional) re-annotates when rows are loaded later.
``blocked`` replaces the control with a message (e.g. a search with no filters).
"""

from django import template

register = template.Library()


@register.inclusion_tag("YSE_App/observable_tonight_control.html")
def observable_tonight_control(id_selector, scope="", watch="", blocked=""):
    from YSE_App.services import observable_tonight as tonight
    from YSE_App.services.observability import telescopes_with_coordinates

    return {
        "telescopes": [] if blocked else list(telescopes_with_coordinates()),
        "id_selector": id_selector,
        "scope": scope,
        "watch": watch,
        "blocked": blocked,
        "max_objects": tonight.MAX_OBJECTS,
        "default_airmass": tonight.DEFAULT_AIRMASS,
        "min_airmass": tonight.MIN_AIRMASS_LIMIT,
        "max_airmass": tonight.MAX_AIRMASS_LIMIT,
    }
