"""Repeat follow-up requests in bulk (#853), for UI preview users only (404 otherwise).

"Repeat follow-up…" in the bulk menus (preview dashboard, search, a Selection's results,
the combined list) posts the ticked transients to ``POST followup/repeat/``:

1. **Review** (no ``confirm``): a summary ("Repeat for 8 of 10 selected; 2 skipped"), the
   objects that will get a request with their most recent request, the skipped ones with
   the reason, and the options: resource, priority, date window, collaboration groups and
   an optional comment. Defaults come from the most recent requests where sensible.
2. **Confirm** (``confirm=1``): one request per eligible object, each validated by
   ``TransientFollowupForm`` and saved by ``form_views.save_followup_request``: the same
   code path as a single request (authorized resources, group eligibility for the resource
   (#794), ``modified_by``, the transient status, notifications through the model signals).

Bulk requesting is only for objects someone already follows: a transient is eligible only
when it already has a follow-up request the user can see. Others are listed as skipped and
never get a first request here; new targets are requested from their own page.
"""

from __future__ import annotations

import datetime

from django import forms
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group
from django.db import transaction
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from YSE_App.common.ui_preview import ui_preview_enabled

#: At most this many ticked objects per bulk request (each is a full form validation).
MAX_OBJECTS = 100
#: Window length used for ToO / queued requests when the last request has none usable.
DEFAULT_WINDOW = datetime.timedelta(days=1)
MAX_WINDOW = datetime.timedelta(days=30)

SKIP_NO_REQUEST = 'no existing request: request individually from its page'
SKIP_NOT_VISIBLE = 'not found, or not listed for you'

RESOURCE_FIELDS = (
    ('classical_resource', 'Classical nights'),
    ('too_resource', 'ToO'),
    ('queued_resource', 'Queued'),
)


def _require_preview(request):
    if not ui_preview_enabled(request):
        raise Http404('Repeat follow-up is part of the new layout preview.')


def _resource_of(followup):
    for field_name, _label in RESOURCE_FIELDS:
        resource = getattr(followup, field_name, None)
        if resource is not None:
            return field_name, resource
    return None, None


def _resource_key(field_name, resource):
    return '%s:%s' % (field_name, resource.pk)


def safe_next(request, target):
    """``target`` if it is a local URL, else the dashboard."""
    if target and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return target
    return reverse('dashboard')


def posted_ids(request):
    """The ``transient_id`` values posted, as ints, in order, without repeats."""
    seen = []
    for value in request.POST.getlist('transient_id'):
        value = str(value).strip()
        if value.isdigit() and int(value) not in seen:
            seen.append(int(value))
    return seen


class RepeatRow:
    """One eligible object: the transient and its most recent visible follow-up request."""

    def __init__(self, transient, last_followup):
        self.transient = transient
        self.last = last_followup
        self.last_field, self.last_resource = _resource_of(last_followup)
        children = sorted(last_followup.requests.all(), key=lambda row: (row.requested_at, row.id))
        self.last_priority = None
        if children:
            self.last_priority = children[-1].priority
        if self.last_priority is None:
            self.last_priority = last_followup.priority
        from YSE_App.services.followup_requests import DEFAULT_PRIORITY

        if self.last_priority is None:
            self.last_priority = DEFAULT_PRIORITY
        self.last_group_ids = [group.pk for group in last_followup.groups.all()]

    @property
    def last_window(self):
        start, stop = self.last.valid_start, self.last.valid_stop
        if start and stop and stop > start:
            return min(stop - start, MAX_WINDOW)
        return DEFAULT_WINDOW


def split_eligible(user, transient_ids):
    """``(rows, skipped)`` for the posted ids.

    ``rows``: one :class:`RepeatRow` per listed transient that has a follow-up request the
    user can see, in the posted order. ``skipped``: ``(transient or None, reason)`` for the
    others. Transients the user may not see listed are counted without their names.
    """
    from YSE_App.models import Transient, TransientFollowup
    from YSE_App.services.followup_requests import requests_prefetch
    from YSE_App.services.visibility import (
        filter_listed_transients,
        filter_transient_followups_for_user,
    )

    listed = {t.pk: t for t in filter_listed_transients(user, Transient.objects.filter(pk__in=transient_ids))}
    latest = {}
    if listed:
        visible_ids = filter_transient_followups_for_user(
            TransientFollowup.objects.filter(transient_id__in=list(listed)), user,
        ).values_list('pk', flat=True)
        followups = (
            TransientFollowup.objects.filter(pk__in=list(visible_ids))
            .select_related('status', 'classical_resource__telescope', 'too_resource__telescope',
                            'queued_resource__telescope')
            .prefetch_related(requests_prefetch(), 'groups')
            .order_by('-id')
        )
        for followup in followups:
            latest.setdefault(followup.transient_id, followup)
    rows, skipped = [], []
    for pk in transient_ids:
        transient = listed.get(pk)
        if transient is None:
            skipped.append((None, SKIP_NOT_VISIBLE))
        elif pk not in latest:
            skipped.append((transient, SKIP_NO_REQUEST))
        else:
            rows.append(RepeatRow(transient, latest[pk]))
    return rows, skipped


class RepeatFollowupOptionsForm(forms.Form):
    """The options applied to every eligible object; blanks fall back to each object's last request."""

    resource = forms.ChoiceField(label='Telescope / resource')
    priority = forms.FloatField(
        required=False, min_value=1.0, max_value=5.0, label='Priority',
        help_text="1.0 highest, 5.0 lowest. Leave blank to keep each object's last priority.",
        widget=forms.NumberInput(attrs={'step': '0.1', 'min': '1.0', 'max': '5.0'}))
    valid_start = forms.DateTimeField(required=False, label='Window start (UTC)')
    valid_stop = forms.DateTimeField(required=False, label='Window end (UTC)')
    audience_groups = forms.ModelMultipleChoiceField(
        queryset=Group.objects.none(), required=False, label='Collaboration groups',
        widget=forms.CheckboxSelectMultiple)
    comment = forms.CharField(required=False, max_length=500, label='Comment',
                              widget=forms.TextInput(attrs={'autocomplete': 'off'}))

    def __init__(self, *args, user, rows=(), **kwargs):
        super().__init__(*args, **kwargs)
        from YSE_App.forms import TransientFollowupForm

        self._user = user
        # The single-request form's authorized, upcoming resources (same querysets, same order).
        self.base_form = TransientFollowupForm(user=user)
        self.resources = {}
        choices = [('', 'Choose a resource…')]
        for field_name, label in RESOURCE_FIELDS:
            options = []
            for resource in self.base_form.fields[field_name].queryset.select_related('telescope'):
                key = _resource_key(field_name, resource)
                self.resources[key] = (field_name, resource)
                options.append((key, str(resource)))
            if options:
                choices.append((label, options))
        self.fields['resource'].choices = choices
        self.fields['audience_groups'].queryset = user.groups.order_by('name')
        self.resource_audience_map = self.base_form.resource_audience_map

        if not self.is_bound:
            self._set_initial(list(rows))

    def _set_initial(self, rows):
        # Resource: the most recent requests' resource when they all share one that is
        # still offered; otherwise the single form's default (the soonest classical night).
        last_keys = {_resource_key(r.last_field, r.last_resource) for r in rows if r.last_resource is not None}
        initial_key = None
        if len(last_keys) == 1 and next(iter(last_keys)) in self.resources:
            initial_key = next(iter(last_keys))
        elif self.base_form.fields['classical_resource'].initial is not None:
            initial_key = _resource_key('classical_resource', self.base_form.fields['classical_resource'].initial)
        if initial_key in self.resources:
            self.fields['resource'].initial = initial_key
        resource = self.resources[initial_key][1] if initial_key in self.resources else None
        # Groups: the groups of the most recent requests that the resource allows, else the
        # single form's defaults for that resource.
        from YSE_App.services.audience import build_followup_audience_choices

        choices = build_followup_audience_choices(self._user, resource)
        enabled = {c['group'].pk for c in choices if c['enabled']}
        last_groups = set()
        for row in rows:
            last_groups.update(row.last_group_ids)
        wanted = [pk for pk in last_groups if pk in enabled]
        self.fields['audience_groups'].initial = wanted or [c['group'].pk for c in choices if c['checked']]

    def audience_choices(self):
        """``build_followup_audience_choices`` for the chosen resource, for the template."""
        from YSE_App.services.audience import build_followup_audience_choices

        key = self.data.get('resource') if self.is_bound else self.fields['resource'].initial
        resource = self.resources.get(key, (None, None))[1]
        choices = build_followup_audience_choices(self._user, resource)
        if self.is_bound:
            picked = {str(v) for v in self.data.getlist('audience_groups')}
        else:
            picked = {str(v) for v in (self.fields['audience_groups'].initial or [])}
        for choice in choices:
            choice['checked'] = str(choice['group'].pk) in picked and choice['enabled']
        return choices

    def clean(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        from YSE_App.services.audience import resolve_followup_audience

        cleaned = super().clean()
        if 'resource' in self.errors:
            return cleaned
        key = cleaned.get('resource')
        if key not in self.resources:
            raise forms.ValidationError({'resource': 'Choose one of the resources offered.'})
        field_name, resource = self.resources[key]
        cleaned['resource_field'] = field_name
        cleaned['resource_obj'] = resource
        start, stop = cleaned.get('valid_start'), cleaned.get('valid_stop')
        if field_name != 'classical_resource':
            if bool(start) != bool(stop):
                raise forms.ValidationError('Give both ends of the date window, or leave both blank.')
            if start and stop and stop <= start:
                raise forms.ValidationError({'valid_stop': 'The window must end after it starts.'})
        try:
            resolve_followup_audience(
                self._user, 0,
                audience_groups=list(cleaned.get('audience_groups') or []),
                linked_resource=resource, explicit_audience=True)
        except DRFValidationError as exc:
            detail = exc.detail
            if isinstance(detail, dict):
                detail = detail.get('audience_groups', detail)
            raise forms.ValidationError({'audience_groups': detail}) from exc
        return cleaned


def followup_form_data(options, row, status, now):
    """The ``TransientFollowupForm`` POST data for one object and the chosen options."""
    field_name = options['resource_field']
    resource = options['resource_obj']
    priority = options.get('priority')
    if priority is None:
        priority = row.last_priority
    data = {
        'status': status.pk,
        'transient': row.transient.pk,
        field_name: resource.pk,
        'priority': priority,
        'comment': options.get('comment') or '',
        'audience_groups': [group.pk for group in options.get('audience_groups') or []],
    }
    if field_name == 'classical_resource':
        start, stop = resource.begin_date_valid, resource.end_date_valid
    elif options.get('valid_start') and options.get('valid_stop'):
        start, stop = options['valid_start'], options['valid_stop']
    else:
        # Each object keeps the length of its last window, starting now.
        start, stop = now, now + row.last_window
    data['valid_start'] = start.isoformat()
    data['valid_stop'] = stop.isoformat()
    return data


def _form_errors_text(form):
    parts = []
    for field, errors in form.errors.items():
        label = '' if field == '__all__' else '%s: ' % field.replace('_', ' ')
        parts.append(label + ' '.join(str(e) for e in errors))
    return '; '.join(parts)


def create_repeats(user, rows, options):
    """One request per row through the single-request path. Returns ``(created, failed)``.

    ``created``: ``(row, parent, created_parent)``; ``failed``: ``(row, reason)``.
    """
    from YSE_App.form_views import save_followup_request
    from YSE_App.forms import TransientFollowupForm
    from YSE_App.models import FollowupStatus

    status = FollowupStatus.objects.filter(name='Requested').first()
    created, failed = [], []
    if status is None:
        return created, [(row, 'the "Requested" follow-up status is missing') for row in rows]
    now = timezone.now()
    for row in rows:
        form = TransientFollowupForm(data=followup_form_data(options, row, status, now), user=user)
        if not form.is_valid():
            failed.append((row, _form_errors_text(form)))
            continue
        with transaction.atomic():
            parent, created_parent = save_followup_request(user, form.cleaned_data)
        created.append((row, parent, created_parent))
    return created, failed


def summary_text(n_eligible, n_selected, n_skipped):
    text = 'Repeat for %d of %d selected' % (n_eligible, n_selected)
    if n_skipped:
        text += '; %d skipped' % n_skipped
    return text + '.'


def render_review(request, transient_ids, next_url, options_form=None, too_many=False):
    """The review page for ``transient_ids`` (also used by the search page's bulk menu)."""
    rows, skipped = split_eligible(request.user, transient_ids)
    if options_form is None:
        options_form = RepeatFollowupOptionsForm(user=request.user, rows=rows)
    return render(request, 'YSE_App/followup/repeat_bulk.html', {
        'step': 'review',
        'rows': rows,
        'skipped': skipped,
        'n_selected': len(transient_ids),
        'summary': summary_text(len(rows), len(transient_ids), len(skipped)),
        'form': options_form,
        'audience_choices': options_form.audience_choices(),
        'next_url': next_url,
        'too_many': too_many,
        'max_objects': MAX_OBJECTS,
    })


@login_required
@require_POST
def repeat_followup(request):
    """``POST followup/repeat/``: review (no ``confirm``) or create (``confirm=1``)."""
    _require_preview(request)
    next_url = safe_next(request, request.POST.get('next', ''))
    transient_ids = posted_ids(request)
    if len(transient_ids) > MAX_OBJECTS:
        return render_review(request, [], next_url, too_many=True)
    if request.POST.get('confirm') != '1':
        return render_review(request, transient_ids, next_url)

    rows, skipped = split_eligible(request.user, transient_ids)
    form = RepeatFollowupOptionsForm(request.POST, user=request.user, rows=rows)
    if not rows or not form.is_valid():
        return render_review(request, transient_ids, next_url, options_form=form)
    created, failed = create_repeats(request.user, rows, form.cleaned_data)
    return render(request, 'YSE_App/followup/repeat_bulk.html', {
        'step': 'done',
        'created': created,
        'failed': failed,
        'skipped': skipped,
        'n_selected': len(transient_ids),
        'resource': form.cleaned_data['resource_obj'],
        'next_url': next_url,
    })
