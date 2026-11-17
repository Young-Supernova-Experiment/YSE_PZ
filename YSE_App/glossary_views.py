"""Glossary page (#862): the terms new users meet on the transient pages.

Statuses and quality-flag bits come from the code that defines them
(``common.status_display`` and ``common.quality_mask``), so the page cannot
drift from what the site stores. The other entries are prose in
``templates/YSE_App/glossary.html``. Any signed-in user may open the page; the
"?" links that point at it show only with the UI preview on.
"""

from django.shortcuts import render

from YSE_App.common.quality_mask import PhotFlag, SpecFlag
from YSE_App.common.status_display import TRANSIENT_STATUS_DISPLAY

#: Anchors on the glossary page. ``glossary_help`` accepts only these.
GLOSSARY_TERMS = (
    'status',
    'real-bogus',
    'quality-flags',
    'mjd',
    'magnitudes',
    'phase',
    'bazin-salt',
)


def _flag_rows(flag_cls):
    return [(member.value.bit_length() - 1, member.name) for member in flag_cls]


def glossary(request):
    statuses = [
        {'name': name, 'label': label, 'definition': definition}
        for name, (label, definition) in TRANSIENT_STATUS_DISPLAY.items()
    ]
    return render(request, 'YSE_App/glossary.html', {
        'statuses': statuses,
        'phot_flags': _flag_rows(PhotFlag),
        'spec_flags': _flag_rows(SpecFlag),
    })
