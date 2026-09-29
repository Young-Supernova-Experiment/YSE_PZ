"""In-app notification list, badge count, mark-read, preferences and mention autocomplete (#266, #320)."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from django.conf import settings

from YSE_App.models.notification_models import CHANNELS, KIND_GROUPS, Notification, NotificationPreference
from YSE_App.services import notify as notify_service

CHANNEL_LABELS = {"in_app": "In-app", "email": "Email", "slack": "Slack"}


def matrix_field_name(group, channel):
    return "kind_%s_%s" % (group, channel)


class NotificationPreferenceForm(forms.ModelForm):
    """Global channel switches plus one checkbox per (kind group, channel) stored in ``kinds``."""

    class Meta:
        model = NotificationPreference
        fields = ("in_app", "email", "slack_webhook_url")
        widgets = {
            "slack_webhook_url": forms.URLInput(attrs={"class": "form-control", "placeholder": "https://hooks.slack.com/services/..."}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        matrix = (self.instance or NotificationPreference()).matrix()
        for group, label, _desc, _kinds in KIND_GROUPS:
            for channel in CHANNELS:
                name = matrix_field_name(group, channel)
                self.fields[name] = forms.BooleanField(
                    required=False, initial=matrix[group][channel],
                    label="%s: %s" % (label, CHANNEL_LABELS[channel]),
                    widget=forms.CheckboxInput(attrs={"class": "yse-pref-kind", "data-group": group,
                                                      "data-channel": channel}),
                )

    def matrix_rows(self):
        """[(group, label, description, [(channel, BoundField), ...]), ...] for the template."""
        rows = []
        for group, label, desc, _kinds in KIND_GROUPS:
            rows.append((group, label, desc, [(channel, self[matrix_field_name(group, channel)]) for channel in CHANNELS]))
        return rows

    def save(self, commit=True):
        obj = super().save(commit=False)
        for group, _label, _desc, _kinds in KIND_GROUPS:
            obj.set_group_channels(group, **{
                channel: bool(self.cleaned_data.get(matrix_field_name(group, channel))) for channel in CHANNELS
            })
        if commit:
            obj.save()
        return obj


def _safe_next(request, fallback):
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate and url_has_allowed_host_and_scheme(candidate, allowed_hosts={request.get_host()},
                                                     require_https=request.is_secure()):
        return candidate
    return fallback


@login_required
def notification_list(request):
    """The user's notifications, newest first; ``?unread=1`` shows only unread."""
    qs = Notification.objects.filter(recipient=request.user).select_related("transient")
    only_unread = request.GET.get("unread") == "1"
    if only_unread:
        qs = qs.filter(read_at__isnull=True)
    kind = (request.GET.get("kind") or "").strip()[:32]
    if kind:
        qs = qs.filter(kind=kind)
    page_size = int(getattr(settings, "NOTIFICATION_LIST_PAGE_SIZE", 50) or 50)
    page = Paginator(qs, page_size).get_page(request.GET.get("page"))
    kinds_present = list(
        Notification.objects.filter(recipient=request.user).order_by().values_list("kind", flat=True).distinct()
    )
    labels = dict(Notification.KIND_CHOICES)
    kind_choices = sorted(((k, labels.get(k) or k.replace("_", " ")) for k in kinds_present), key=lambda kv: kv[1])
    query_suffix = ("&unread=1" if only_unread else "") + ("&kind=%s" % kind if kind else "")
    return render(request, "YSE_App/notifications.html", {
        "page": page,
        "only_unread": only_unread,
        "kind": kind,
        "kind_label": labels.get(kind) or kind.replace("_", " "),
        "kind_choices": kind_choices,
        "query_suffix": query_suffix,
        "unread_count": notify_service.unread_count(request.user),
        "email_enabled": bool(getattr(settings, "NOTIFICATION_EMAIL_ENABLED", False)),
    })


@login_required
@require_GET
def notification_unread_count(request):
    """JSON ``{"unread": n}`` for the navbar badge."""
    return JsonResponse({"unread": notify_service.unread_count(request.user)})


@login_required
@require_POST
def notification_mark_read(request, notification_id):
    """Mark one notification read; redirects to its URL (``?follow=1``) or back to the list."""
    notification = get_object_or_404(Notification, pk=notification_id, recipient=request.user)
    notification.mark_read()
    if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.GET.get("format") == "json":
        return JsonResponse({"id": notification.pk, "read_at": notification.read_at.isoformat(),
                             "unread": notify_service.unread_count(request.user)})
    if request.POST.get("follow") == "1" and notification.url:
        target = notification.url
        if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()},
                                           require_https=request.is_secure()) or target.startswith("/"):
            return redirect(target)
    return redirect(_safe_next(request, reverse("notification_list")))


@login_required
@require_POST
def notification_mark_all_read(request):
    n = notify_service.mark_all_read(request.user)
    if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.GET.get("format") == "json":
        return JsonResponse({"marked": n, "unread": 0})
    if n:
        messages.success(request, "Marked %d notification%s as read." % (n, "" if n == 1 else "s"))
    return redirect(_safe_next(request, reverse("notification_list")))


@login_required
def notification_preferences(request):
    pref = NotificationPreference.objects.filter(user=request.user).first()
    if request.method == "POST":
        form = NotificationPreferenceForm(request.POST, instance=pref or NotificationPreference(user=request.user))
        if form.is_valid():
            obj = form.save(commit=False)
            obj.user = request.user
            obj.save()
            messages.success(request, "Notification preferences saved.")
            return redirect(reverse("notification_preferences"))
    elif request.method == "GET":
        form = NotificationPreferenceForm(instance=pref or NotificationPreference(user=request.user))
    else:
        return HttpResponseBadRequest("GET or POST")
    return render(request, "YSE_App/notification_preferences.html", {
        "form": form,
        "matrix_rows": form.matrix_rows(),
        "channel_labels": [(c, CHANNEL_LABELS[c]) for c in CHANNELS],
        "has_email": bool(request.user.email),
        "email_enabled": bool(getattr(settings, "NOTIFICATION_EMAIL_ENABLED", False)),
        "slack_enabled": bool(getattr(settings, "NOTIFICATION_SLACK_ENABLED", True)),
    })


@login_required
@require_GET
def mention_suggest(request):
    """JSON ``{"results": [{"type", "value", "label"}, ...]}`` for the comment-box autocomplete (#322).

    ``q`` is the text typed after ``@`` or ``#`` (the prefix itself may be
    included); ``type=user`` or ``type=instrument`` restricts the list.
    """
    from YSE_App.services.notifications import mention_suggestions

    q = (request.GET.get("q") or "")[:64]
    wanted = request.GET.get("type") or ""
    if not wanted and q[:1] in ("@", "#"):
        wanted = "user" if q[0] == "@" else "instrument"
    try:
        limit = max(1, min(25, int(request.GET.get("limit") or 10)))
    except ValueError:
        limit = 10
    results = mention_suggestions(q, limit=limit, users=wanted != "instrument", instruments=wanted != "user")
    return JsonResponse({"results": results})
