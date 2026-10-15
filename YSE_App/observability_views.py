"""Observability page and its JSON endpoint (#307): where can this transient be observed tonight?"""

from __future__ import annotations

import datetime

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from YSE_App.models import Transient
from YSE_App.services import observability as svc

# Twilight bands drawn behind the altitude curve, sunset -> dark (fill alpha per band).
_TWILIGHT_BANDS = (("civil", 0.10), ("nautical", 0.18), ("astronomical", 0.26))
PLOT_WIDTH = 330
PLOT_HEIGHT = 230


def _night_from_request(request):
    """(night date, error text): a bad ``?date=`` falls back to the default night."""
    raw = request.GET.get("date", "")
    try:
        return svc.parse_night_date(raw), None
    except ValueError:
        return svc.default_night_date(), "'%s' is not a YYYY-MM-DD date; showing the default night." % raw


def _plot_json(payload):
    """A Bokeh ``json_item`` of the night at one telescope for ``Bokeh.embed.embed_item``.

    Altitude (left axis, 0-90 deg) against UT, the moon's altitude dashed, the
    twilight bands shaded from sunset to astronomical darkness, the 30 degree
    (airmass 2) line dotted. Built here rather than in the browser so the plot
    matches the Bokeh figures elsewhere in the app.
    """
    from bokeh.embed import json_item
    from bokeh.models import BoxAnnotation, ColumnDataSource, HoverTool, Span
    from bokeh.plotting import figure

    samples, night, summary = payload["samples"], payload["night"], payload["summary"]
    tel = payload["telescope"]
    times = [datetime.datetime.strptime(t, "%Y-%m-%dT%H:%M") for t in samples["t"]]
    alt = [a if a is not None and a > 0 else float("nan") for a in samples["alt"]]
    moon_alt = [a if a is not None and a > 0 else float("nan") for a in samples["moon_alt"]]
    source = ColumnDataSource(data={
        "t": times,
        "alt": alt,
        "airmass": [a if a is not None else float("nan") for a in samples["airmass"]],
        "moon_alt": moon_alt,
        "moon_sep": [s if s is not None else float("nan") for s in samples["moon_sep"]],
    })
    title = tel["name"]
    if summary["never_rises"]:
        title += "  (never rises)"
    fig = figure(
        title=title, x_axis_type="datetime", plot_width=PLOT_WIDTH, plot_height=PLOT_HEIGHT,
        y_range=(0, 90), toolbar_location=None, tools="",
        x_range=(times[0], times[-1]),
    )
    fig.title.text_font_size = "11pt"
    fig.yaxis.axis_label = "Altitude (deg)"
    fig.xaxis.axis_label = "UT"
    fig.xaxis.axis_label_text_font_size = fig.yaxis.axis_label_text_font_size = "9pt"
    fig.xaxis.major_label_text_font_size = fig.yaxis.major_label_text_font_size = "8pt"
    fig.yaxis.ticker = [0, 30, 60, 90]
    fig.min_border_left = 40

    def _dt(iso):
        return datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M") if iso else None

    sunset, sunrise = _dt(night["sunset"]), _dt(night["sunrise"])
    # Daylight (before sunset / after sunrise) is the brightest band.
    if sunset:
        fig.add_layout(BoxAnnotation(right=sunset, fill_color="#f0ad4e", fill_alpha=0.25, line_alpha=0))
    if sunrise:
        fig.add_layout(BoxAnnotation(left=sunrise, fill_color="#f0ad4e", fill_alpha=0.25, line_alpha=0))
    prev_e, prev_m = sunset, sunrise
    for name, alpha in _TWILIGHT_BANDS:
        e, m = _dt(night["twilight"].get("evening_%s" % name)), _dt(night["twilight"].get("morning_%s" % name))
        if prev_e and e:
            fig.add_layout(BoxAnnotation(left=prev_e, right=e, fill_color="#5b7db1", fill_alpha=alpha, line_alpha=0))
        if prev_m and m:
            fig.add_layout(BoxAnnotation(left=m, right=prev_m, fill_color="#5b7db1", fill_alpha=alpha, line_alpha=0))
        prev_e, prev_m = e or prev_e, m or prev_m
    fig.add_layout(Span(location=svc.OBSERVABLE_ALT_DEG, dimension="width", line_color="#888888",
                        line_dash="dotted", line_width=1))
    now = datetime.datetime.utcnow()
    if times[0] <= now <= times[-1]:
        fig.add_layout(Span(location=now, dimension="height", line_color="#d9534f", line_dash="dashed", line_width=1))

    fig.line("t", "moon_alt", source=source, line_color="#999999", line_dash="dashed", line_width=1.2,
             legend_label="Moon")
    target = fig.line("t", "alt", source=source, line_color="#1f77b4", line_width=2.2,
                      legend_label=payload["transient"]["name"])
    fig.legend.location = "top_left"
    fig.legend.label_text_font_size = "8pt"
    fig.legend.padding = 2
    fig.legend.spacing = 0
    fig.legend.background_fill_alpha = 0.6
    fig.add_tools(HoverTool(
        renderers=[target], mode="vline",
        tooltips=[("UT", "@t{%H:%M}"), ("alt", "@alt{0.0} deg"), ("airmass", "@airmass{0.00}"),
                  ("moon", "@moon_sep{0} deg away")],
        formatters={"@t": "datetime"},
    ))
    return json_item(fig, target="obs_plot_%s" % tel["id"])


def _telescopes_from_request(request):
    mine = request.GET.get("mine") == "1"
    telescopes = svc.telescopes_for_user(request.user, mine=mine)
    raw = request.GET.get("telescope", "").strip()
    if raw:
        ids = [int(part) for part in raw.split(",") if part.strip().isdigit()]
        telescopes = telescopes.filter(pk__in=ids)
    return mine, telescopes


@login_required
@require_GET
def observability_page(request, transient_id):
    transient = get_object_or_404(Transient, pk=transient_id)
    night, date_error = _night_from_request(request)
    mine, telescopes = _telescopes_from_request(request)
    telescopes = list(telescopes)
    return render(request, "YSE_App/observability.html", {
        "transient": transient,
        "night": night,
        "night_iso": night.isoformat(),
        "prev_night": (night - datetime.timedelta(days=1)).isoformat(),
        "next_night": (night + datetime.timedelta(days=1)).isoformat(),
        "tonight": svc.default_night_date().isoformat(),
        "date_error": date_error,
        "mine": mine,
        "telescopes": telescopes,
        "observable_alt": int(svc.OBSERVABLE_ALT_DEG),
        "data_url": reverse("observability_data", kwargs={"transient_id": transient.pk}),
    })


@login_required
@require_GET
def observability_data(request, transient_id):
    """JSON for the page: ``?date=YYYY-MM-DD&telescope=1,2&mine=1&plots=0``.

    One entry per telescope with the cached ephemeris, its summary row and
    (unless ``plots=0``) the Bokeh plot item; ``rows`` repeats the summaries
    sorted by hours observable. The page asks for one telescope per request so
    the plots appear as each answer arrives.
    """
    transient = get_object_or_404(Transient, pk=transient_id)
    night, date_error = _night_from_request(request)
    if date_error:
        return JsonResponse({"error": date_error}, status=400)
    _, telescopes = _telescopes_from_request(request)
    want_plots = request.GET.get("plots", "1") != "0"
    results = []
    for telescope in telescopes:
        payload = dict(svc.night_ephemeris(transient, telescope, night))
        if want_plots:
            payload["plot"] = _plot_json(payload)
        results.append(payload)
    return JsonResponse({
        "transient": {"id": transient.pk, "name": transient.name, "ra": transient.ra, "dec": transient.dec},
        "date": night.isoformat(),
        "observable_alt": svc.OBSERVABLE_ALT_DEG,
        "results": results,
        "rows": svc.summary_rows(results),
    })
