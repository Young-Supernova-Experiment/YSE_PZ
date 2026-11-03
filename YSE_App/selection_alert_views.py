"""Alert pages for Selections (#844), for UI preview users only.

* ``GET|POST selections/<slug>/alert/``: "Alert me" on a Selection. The first submit shows the
  volume estimate for the last 30 days; only the second ("Save alert", ``confirm=1``) saves.
* ``GET selection-alerts/``: "My alerts".
* ``GET|POST selection-alerts/<id>/edit/`` and ``POST selection-alerts/<id>/delete/``.

Users without the preview get 404 on every page here. The alert rules (who may alert a team)
live in ``YSE_App.services.selection_alerts`` and are checked there, not here.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from YSE_App.common.ui_preview import ui_preview_enabled
from YSE_App.models import SelectionAlert
from YSE_App.services import selection_alerts as svc
from YSE_App.services.selections import SelectionError


def _require_preview(request):
    if not ui_preview_enabled(request):
        raise Http404('Selection alerts are part of the new layout preview.')


def _values_from_post(post):
    return {
        'label': post.get('label', ''),
        'target': post.get('target', SelectionAlert.TARGET_ME),
        'team': post.get('team', ''),
        'slack_channel': post.get('slack_channel', ''),
        'on_new_match': post.get('on_new_match') == '1',
        'on_new_spectrum': post.get('on_new_spectrum') == '1',
        'on_class_change': post.get('on_class_change') == '1',
        'mag_threshold': post.get('mag_threshold', '') if post.get('on_mag') == '1' else None,
        'mag_direction': post.get('mag_direction', SelectionAlert.BRIGHTER),
        'frequency': post.get('frequency', SelectionAlert.FREQUENCY_DAILY),
    }


def _form_values(alert):
    return {
        'label': alert.label,
        'target': alert.target,
        'team': str(alert.team_id or ''),
        'slack_channel': alert.slack_channel,
        'on_new_match': alert.on_new_match,
        'on_new_spectrum': alert.on_new_spectrum,
        'on_class_change': alert.on_class_change,
        'mag_threshold': '' if alert.mag_threshold is None else '%g' % alert.mag_threshold,
        'on_mag': alert.mag_threshold is not None,
        'mag_direction': alert.mag_direction,
        'frequency': alert.frequency,
    }


def _estimate(selection, user, values):
    return svc.estimate_volume(
        selection, user, on_new_match=values['on_new_match'], on_new_spectrum=values['on_new_spectrum'],
        on_class_change=values['on_class_change'], mag_threshold=values['mag_threshold'],
        mag_direction=values['mag_direction'], frequency=values['frequency'])


def _form(request, selection, *, alert=None):
    """The shared create / edit flow: estimate first, then save on ``confirm=1``."""
    user = request.user
    error, estimate = None, None
    if request.method == 'POST':
        values = _values_from_post(request.POST)
        form = dict(values, on_mag=request.POST.get('on_mag') == '1',
                    mag_threshold=request.POST.get('mag_threshold', ''))
        try:
            # The same checks as saving, so an alert that would be refused never shows an estimate.
            svc.validate_alert(user, selection, alert=alert, **values)
            if request.POST.get('confirm') == '1':
                if alert is None:
                    svc.create_alert(user, selection, **values)
                    messages.success(request, 'Alert set on "%s".' % selection.name)
                else:
                    svc.update_alert(user, alert, **values)
                    messages.success(request, 'Saved the alert on "%s".' % selection.name)
                return HttpResponseRedirect(reverse('selection_alert_list'))
            estimate = _estimate(selection, user, values)
        except (svc.AlertError, SelectionError) as exc:
            error = str(exc)
    else:
        form = _form_values(alert) if alert is not None else _form_values(SelectionAlert())
    return render(request, 'YSE_App/selections/alert_form.html', {
        'selection': selection,
        'alert': alert,
        'form': form,
        'error': error,
        'estimate': estimate,
        'teams': svc.owned_teams(user),
        'frequency_choices': SelectionAlert.FREQUENCY_CHOICES,
        'direction_choices': SelectionAlert.MAG_DIRECTION_CHOICES,
        'target_choices': SelectionAlert.TARGET_CHOICES,
    })


@login_required
@require_http_methods(['GET', 'POST'])
def selection_alert_new(request, slug):
    _require_preview(request)
    selection = svc.selection_for_alerts(request.user, slug)
    if selection is None:
        raise Http404('No such Selection.')
    return _form(request, selection)


def _own_alert_or_404(request, pk):
    alert = get_object_or_404(SelectionAlert.objects.select_related('selection', 'team'), pk=pk)
    if not svc.can_change(request.user, alert):
        raise Http404('No such alert.')
    return alert


@login_required
@require_http_methods(['GET', 'POST'])
def selection_alert_edit(request, pk):
    _require_preview(request)
    alert = _own_alert_or_404(request, pk)
    return _form(request, alert.selection, alert=alert)


@login_required
@require_POST
def selection_alert_delete(request, pk):
    _require_preview(request)
    alert = _own_alert_or_404(request, pk)
    name = alert.display_name
    svc.delete_alert(request.user, alert)
    messages.success(request, 'Deleted the alert on "%s".' % name)
    return HttpResponseRedirect(reverse('selection_alert_list'))


@login_required
@require_GET
def selection_alert_list(request):
    """My alerts: every alert I set, with its triggers, recipients and frequency."""
    _require_preview(request)
    return render(request, 'YSE_App/selections/alerts.html', {
        'alerts': list(svc.user_alerts(request.user)),
        'enabled': svc.alerts_enabled(),
    })
