import re

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter(name="format_comment_text")
def format_comment_text(value):
    """Escape HTML and highlight @user and #instrument mentions for display."""
    if not value:
        return ""
    text = escape(value)
    text = re.sub(
        r"(?<![\w.])@(\w+)",
        r'<span class="yse-mention">@\1</span>',
        text,
    )
    text = re.sub(
        r"(?<![\w&])#([A-Za-z][\w.+\-]*)",
        r'<span class="yse-mention yse-mention-instrument">#\1</span>',
        text,
    )
    return mark_safe(text.replace("\n", "<br>"))
