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

from YSE_App.models import ClassicalObservingDate, NightPlan
from YSE_App.services import night_planner, night_plans


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


def _plan_for(night, request):
    """The saved version named by ``?plan=`` (404 if it is not a plan of this night)."""
    raw = request.GET.get("plan")
    if not raw:
        return None
    plan = NightPlan.objects.filter(pk=raw if str(raw).isdigit() else 0, night=night).first()
    if plan is None:
        raise Http404("no such plan for this night")
    return plan


def _transient_url(request):
    def url_for(transient):
        return request.build_absolute_uri(
            reverse("transient_detail", kwargs={"slug": transient.slug or transient.name})
        )
    return url_for


@login_required
def night_planner_page(request, night_id):
    night = _night(night_id)
    plan = _plan_for(night, request)
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
        "frame_url": reverse("night_planner_frame", kwargs={"night_id": night.id})
                     + ("?plan=%d" % plan.id if plan else ""),
        "observing_night_url": observing_night_url,
        "plan": plan,
        "plans": night_plans.plan_summaries(night, request.user),
        "can_approve": night_plans.can_approve(night, request.user),
        "plans_api": reverse("api-night-plans", kwargs={"night_id": night.id}),
        "planner_url": reverse("night_planner", kwargs={"night_id": night.id}),
    })


@login_required
@xframe_options_sameorigin
def night_planner_frame(request, night_id):
    from astroscheduler import frontend

    night = _night(night_id)
    plan = _plan_for(night, request)
    api_base = reverse("night_planner", kwargs={"night_id": night.id}).rstrip("/")
    config = night_planner.frame_config(
        night, request.user, api_base=api_base, csrf_token=get_token(request),
        url_for=_transient_url(request), plan=plan,
    )
    return HttpResponse(frontend.render_page(static("astroscheduler"), config))


@login_required
def night_plan_target_list(request, plan_id):
    """A saved plan in the observing target-list format, in planned order (#77)."""
    plan = (NightPlan.objects.select_related("night__resource__telescope", "night__night_type")
            .filter(pk=plan_id).first())
    if plan is None:
        raise Http404("no night plan %r" % plan_id)
    _night(plan.night_id)
    text = night_plans.export_target_list(plan, request.user, finders=request.GET.get("finders") == "1")
    response = HttpResponse(text, content_type="text/plain")
    filename = "%s_%s_plan-v%d.txt" % (plan.night.resource.telescope.name.replace(" ", "_"),
                                       night_planner.night_date(plan.night).isoformat(), plan.version)
    response["Content-Disposition"] = "attachment; filename=%s" % filename
    return response
