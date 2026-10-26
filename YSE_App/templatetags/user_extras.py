from django import template
from ..models import *

register = template.Library()

@register.filter
def has_group(user, group):
    return len(user.groups.filter(name='YSE'))


@register.simple_tag(takes_context=True)
def explorer_link_allowed(context):
    """True when the user may open the Query Explorer index (sidebar link, #802)."""
    from types import SimpleNamespace

    from YSE_App.common.authz import explorer_view_allowed

    request = context.get("request")
    user = getattr(request, "user", None)
    if user is None:
        return False
    # Ask about the page the link points to, not the page being rendered.
    target = SimpleNamespace(
        user=user, resolver_match=SimpleNamespace(url_name="explorer_index", kwargs={})
    )
    return explorer_view_allowed(target)
