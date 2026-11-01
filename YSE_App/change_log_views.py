"""The History tab of the transient detail page and its read-only API (#737).

* ``GET transient_detail/<id>/history_fragment/``  the tab body, loaded when the tab opens
* ``GET api/transients/<id>/history/``  the same entries as JSON (``?limit=``, at most 1000)

Both show :func:`services.change_log.visible_changes`: nothing for a transient the viewer
may not list (404), and entries that may derive from restricted data only to viewers who
may see the transient's data. The actor is shown as a username only.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from YSE_App.models import Transient
from YSE_App.services import change_log
from YSE_App.services.visibility import user_can_list_transient


def _listed_transient(user, transient_id):
    transient = get_object_or_404(Transient.objects.only("id", "name", "host_id"), pk=transient_id)
    if not user_can_list_transient(user, transient.pk):
        raise Http404("No such transient")
    return transient


@login_required
@require_GET
def transient_history_fragment(request, transient_id):
    transient = _listed_transient(request.user, transient_id)
    limit = change_log.clamp_limit(request.GET.get("limit"))
    rows = list(change_log.visible_changes(request.user, transient)[: limit + 1])
    return render(request, "YSE_App/transient_detail_history_tab.html", {
        "transient": transient, "changes": rows[:limit], "truncated": len(rows) > limit, "limit": limit,
    })


class TransientHistoryAPIView(APIView):
    """Read-only change log of one transient and its host (newest first)."""

    permission_classes = (permissions.IsAuthenticated,)
    http_method_names = ["get", "head", "options"]

    def get(self, request, transient_id):
        transient = _listed_transient(request.user, transient_id)
        limit = change_log.clamp_limit(request.query_params.get("limit"))
        rows = list(change_log.visible_changes(request.user, transient)[: limit + 1])
        return Response({
            "transient_id": transient.pk,
            "transient": transient.name,
            "count": min(len(rows), limit),
            "truncated": len(rows) > limit,
            "results": [change_log.as_dict(c) for c in rows[:limit]],
        })
