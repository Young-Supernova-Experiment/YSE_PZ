""""Repeat follow-up…" bulk control for shortlists (#853, UI preview only).

Usage in a template::

    {% load repeat_followup %}
    {% repeat_followup_control id_selector=".yse-dash-select" scope="#yse-dash2 .yse-dash-panel:not([hidden])" %}

``id_selector`` matches the row tick boxes (their ``value`` is the transient id); ``scope``
(optional CSS selector) limits them. The control posts the ticked ids to
``followup/repeat/``, which shows a review page before anything is created. Renders
nothing for users without the preview.
"""

from django import template

register = template.Library()


@register.inclusion_tag("YSE_App/followup/repeat_control.html", takes_context=True)
def repeat_followup_control(context, id_selector, scope=""):
    request = context.get("request")
    from YSE_App.common.ui_preview import ui_preview_enabled

    return {
        "show": ui_preview_enabled(request),
        "csrf_token": context.get("csrf_token", ""),
        "id_selector": id_selector,
        "scope": scope,
        "next_url": request.get_full_path() if request is not None else "",
    }
