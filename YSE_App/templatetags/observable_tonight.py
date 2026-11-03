""""Observable tonight from <site>" control for shortlists (#854, UI preview only).

Usage in a preview template::

    {% load observable_tonight %}
    {% observable_tonight_control id_selector=".yse-dash-select" scope="..." watch="#yse-dash2" %}

``id_selector`` matches one element per row whose ``value`` or
``data-transient-id`` is the transient id; ``scope`` (optional CSS selector)
limits the rows, ``watch`` (optional) re-annotates when rows are loaded later.
``blocked`` replaces the control with a message (e.g. a search with no filters).
The site menu starts from the user's saved default site (#959), then the
browser's last choice, then the first telescope.
"""

from django import template

register = template.Library()


@register.inclusion_tag("YSE_App/observable_tonight_control.html", takes_context=True)
def observable_tonight_control(context, id_selector, scope="", watch="", blocked=""):
    from YSE_App.observability_views import default_site_for
    from YSE_App.services import observable_tonight as tonight
    from YSE_App.services.observability import telescopes_with_coordinates

    telescopes = [] if blocked else list(telescopes_with_coordinates())
    user = getattr(context.get("request"), "user", None) or context.get("user")
    default_site = default_site_for(user) if telescopes and user is not None else None
    return {
        "telescopes": telescopes,
        # The user's saved default (#959) wins over the browser's last choice.
        "default_site_id": default_site.pk if default_site else "",
        "csrf_token": context.get("csrf_token", ""),
        "id_selector": id_selector,
        "scope": scope,
        "watch": watch,
        "blocked": blocked,
        "max_objects": tonight.MAX_OBJECTS,
        "default_airmass": tonight.DEFAULT_AIRMASS,
        "min_airmass": tonight.MIN_AIRMASS_LIMIT,
        "max_airmass": tonight.MAX_AIRMASS_LIMIT,
    }
