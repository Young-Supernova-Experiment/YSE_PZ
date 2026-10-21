"""Instrument log page (#310), telescope page and weather / SkyCam fragments (#311)."""

from __future__ import annotations

import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from YSE_App.models import Allocation, Instrument, InstrumentLog, Telescope
from YSE_App.services import instrument_logs as il
from YSE_App.services import weather as wx


def _parse_date(value, default):
    value = (value or "").strip()
    if not value:
        return default
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(value, fmt).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
    return default


# --- instrument logs -----------------------------------------------------------

@login_required
def instrument_logs(request, instrument_id):
    instrument = get_object_or_404(Instrument.objects.select_related("telescope", "telescope__observatory"), pk=instrument_id)
    today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = _parse_date(request.GET.get("start"), today - datetime.timedelta(days=7))
    end = _parse_date(request.GET.get("end"), today + datetime.timedelta(days=1))
    if request.GET.get("end") and "T" not in request.GET.get("end", "") and ":" not in request.GET.get("end", ""):
        end = end + datetime.timedelta(days=1)  # a bare date means "through that day"
    source = request.GET.get("source", "").strip()
    if source not in ("", InstrumentLog.SOURCE_MANUAL, InstrumentLog.SOURCE_API):
        source = ""
    logs = list(il.logs_between(instrument=instrument, start=start, end=end, source=source)[:500])
    rows = [{"log": row, "entries": row.entries} for row in logs]
    context = {
        "instrument": instrument,
        "telescope": instrument.telescope,
        "rows": rows,
        "start": start, "end": end,
        "start_str": start.strftime("%Y-%m-%d"),
        "end_str": (end - datetime.timedelta(seconds=1)).strftime("%Y-%m-%d"),
        "source": source,
        "can_add": il.can_add_logs(request.user),
        "can_pull": bool(request.user.is_staff or request.user.is_superuser) and bool(il.pull_allocations(instrument)),
        "pull_allocations": il.pull_allocations(instrument),
        "siblings": Instrument.objects.filter(telescope=instrument.telescope).exclude(pk=instrument.pk).order_by("name"),
        "now_str": timezone.now().strftime("%Y-%m-%dT%H:%M"),
        "counts": {"total": len(logs), "api": sum(1 for r in logs if r.source == InstrumentLog.SOURCE_API)},
    }
    return render(request, "YSE_App/instrument_logs.html", context)


@login_required
@require_POST
def instrument_log_add(request, instrument_id):
    instrument = get_object_or_404(Instrument, pk=instrument_id)
    if not il.can_add_logs(request.user):
        raise PermissionDenied("Only staff (or accounts with the add_instrumentlog permission) may add logs.")
    start = _parse_date(request.POST.get("start"), None)
    end = _parse_date(request.POST.get("end"), None)
    message = (request.POST.get("message") or "").strip()
    level = (request.POST.get("level") or "").strip()
    if start is None:
        messages.error(request, "A start date/time is required.")
        return redirect("instrument_logs", instrument_id=instrument.pk)
    if not message:
        messages.error(request, "A message is required.")
        return redirect("instrument_logs", instrument_id=instrument.pk)
    entries = [{"timestamp": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "message": message, "level": level or "info"}]
    row, created = il.add_log(instrument, request.user, start=start, end=end, message=message, entries=entries)
    if created:
        messages.success(request, "Log entry added for %s." % instrument.name)
    else:
        messages.info(request, "An identical log entry already exists (#%d)." % row.pk)
    return redirect("%s?start=%s&end=%s" % (
        reverse("instrument_logs", kwargs={"instrument_id": instrument.pk}),
        (row.start - datetime.timedelta(days=1)).strftime("%Y-%m-%d"), (row.end or row.start).strftime("%Y-%m-%d")))


@login_required
@require_POST
def instrument_log_pull(request, instrument_id):
    instrument = get_object_or_404(Instrument.objects.select_related("telescope"), pk=instrument_id)
    if not (request.user.is_staff or request.user.is_superuser):
        raise PermissionDenied("Only staff may pull logs from the facility.")
    end = _parse_date(request.POST.get("end"), timezone.now())
    start = _parse_date(request.POST.get("start"), end - datetime.timedelta(hours=24))
    if "T" not in (request.POST.get("end") or "") and request.POST.get("end"):
        end = end + datetime.timedelta(days=1)
    if not il.pull_allocations(instrument):
        messages.error(request, "No active allocation on %s has a facility API that publishes instrument logs." % instrument.name)
        return redirect("instrument_logs", instrument_id=instrument.pk)
    queued = il.enqueue_pull(instrument, start=start, end=end, user=request.user)
    if getattr(queued, "status", "") == "done":
        result = queued.result or {}
        created = sum(r.get("created", 0) for r in result.get("results", []))
        errors = [r.get("error") for r in result.get("results", []) if r.get("error")]
        if errors:
            messages.warning(request, "Pull finished with errors: %s" % "; ".join(errors)[:500])
        else:
            messages.success(request, "Pulled %d new log block(s) from the facility." % created)
    else:
        messages.info(request, "Queued a facility pull (job %s); the logs appear once the job runner has run." % queued.pk)
    return redirect("%s?start=%s&end=%s" % (reverse("instrument_logs", kwargs={"instrument_id": instrument.pk}),
                                             start.strftime("%Y-%m-%d"), (end - datetime.timedelta(seconds=1)).strftime("%Y-%m-%d")))


# --- telescope page and weather widget ------------------------------------------

@login_required
def telescope_detail(request, telescope_id):
    telescope = get_object_or_404(Telescope.objects.select_related("observatory"), pk=telescope_id)
    instruments = list(Instrument.objects.filter(telescope=telescope).order_by("name"))
    counts = {row["instrument_id"]: row["n"] for row in
              InstrumentLog.objects.filter(instrument__telescope=telescope).values("instrument_id")
              .order_by().annotate(n=Count("id"))}
    now = timezone.now()
    context = {
        "telescope": telescope,
        "observatory": telescope.observatory,
        "instruments": [{"i": i, "n_logs": counts.get(i.pk, 0)} for i in instruments],
        "recent_logs": il.recent_logs_for_telescope(telescope, days=7, limit=20),
        "allocations": Allocation.objects.filter(telescope=telescope, is_active=True, end_date__gte=now)
                                          .select_related("instrument", "principal_investigator").order_by("name"),
        "widget": wx.widget_context(telescope, user=request.user, allow_fetch=False),
        "telescopes": Telescope.objects.exclude(pk=telescope.pk).order_by("name"),
    }
    return render(request, "YSE_App/telescope_detail.html", context)


@login_required
def telescope_weather_fragment(request, telescope_id):
    """The weather widget body (HTML) or, with ``?format=json``, the snapshot; fetches when the cache is stale."""
    telescope = get_object_or_404(Telescope, pk=telescope_id)
    force = request.GET.get("refresh") == "1" and (request.user.is_staff or request.user.is_superuser)
    compact = request.GET.get("compact") == "1"
    ctx = wx.widget_context(telescope, user=request.user, allow_fetch=True, force=force, compact=compact)
    if request.GET.get("format") == "json":
        return JsonResponse({
            "telescope": telescope.pk, "name": telescope.name, "weather": ctx["weather"],
            "skycam_url": telescope.skycam_url, "weather_link": telescope.weather_link, "stale": ctx["stale"],
        })
    ctx["fragment"] = True
    return render(request, "YSE_App/partials/weather_widget_body.html", ctx)
