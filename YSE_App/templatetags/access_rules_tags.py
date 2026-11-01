"""``{% access_rules_enabled as flag %}``: whether read-time access rules are on (#648, PR 5)."""

from django import template

register = template.Library()


@register.simple_tag
def access_rules_enabled():
    from YSE_App.services import access_rules

    return access_rules.enabled()
