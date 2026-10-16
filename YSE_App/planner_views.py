"""AstroScheduler night planner pages (#659): the page in the YSE_PZ shell, and the planner
itself in a same-origin frame (its stylesheet resets selectors YSE_PZ's own CSS uses).

The frame solves through ``night_planner/<id>/api/schedule`` (the #656 solve view). Both
pages need a login and return 404 while ``ASTROSCHEDULER_ENABLED`` is off.
"""

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import render
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_sameorigin

from YSE_App.models import ClassicalObservingDate
from YSE_App.services import night_planner


def _night(night_id):
    if not night_planner.enabled():
        raise Http404("night planner is not enabled")
    night = (
        ClassicalObservingDate.objects
        .select_related("resource__telescope__observatory", "resource__principal_investigator", "night_type")
        .filter(pk=night_id)
        .first()
    )
    if night is None:
        raise Http404("no observing night %r" % night_id)
    return night


def _transient_url(request):
    def url_for(transient):
        return request.build_absolute_uri(
            reverse("transient_detail", kwargs={"slug": transient.slug or transient.name})
        )
    return url_for


@login_required
def night_planner_page(request, night_id):
    night = _night(night_id)
    telescope = night.resource.telescope
    data = night_planner.night_targets(night, request.user)
    pi = night.resource.principal_investigator
    observing_night_url = reverse("observing_night", kwargs={
        "telescope": telescope.name.replace(" ", "_"),
        "obs_date": data["night"]["date"],
        "pi_name": pi.name if pi else "None",
    })
    return render(request, "YSE_App/night_planner.html", {
        "night": night,
        "night_info": data["night"],
        "telescope": telescope,
        "instrument": data["instrument"],
        "n_targets": len(data["targets"]),
        "warnings": data["warnings"],
        "frame_url": reverse("night_planner_frame", kwargs={"night_id": night.id}),
        "observing_night_url": observing_night_url,
    })


@login_required
@xframe_options_sameorigin
def night_planner_frame(request, night_id):
    from astroscheduler import frontend

    night = _night(night_id)
    api_base = reverse("night_planner", kwargs={"night_id": night.id}).rstrip("/")
    config = night_planner.frame_config(
        night, request.user, api_base=api_base, csrf_token=get_token(request),
        url_for=_transient_url(request),
    )
    return HttpResponse(frontend.render_page(static("astroscheduler"), config))
