"""SQL Explorer inside the YSE-PZ site shell (#450).

``templates/explorer/base.html`` overrides django-sql-explorer's base so its
pages extend ``YSE_App/base.html``. Two small hooks support it:

* ``explorer_shell`` (context processor): the schema pane is loaded into an
  iframe on the query and playground pages, so it must not carry the site
  navbar and sidebar. For those url names it points the override at the bare
  ``explorer/yse_bare.html`` parent instead.
* ``explorer_no_permission`` (``EXPLORER_NO_PERMISSION_VIEW``): a signed-in
  user without Explorer access gets 403 instead of Explorer's default admin
  login form; an anonymous user is sent to the site login.

Who may use Explorer: ``common.authz.explorer_access_allowed`` (full access),
plus every signed-in user with narrower rights when ``EXPLORER_OPEN_TO_ALL_USERS``
is on (``explorer_view_allowed`` / ``explorer_change_allowed``, #723), via
``EXPLORER_PERMISSION_VIEW`` / ``EXPLORER_PERMISSION_CHANGE``. What a query may
read is decided by ``services.explorer_scope`` (``YSE_App.explorer_scope_hooks``).
"""

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied

EXPLORER_BARE_URL_NAMES = frozenset({"explorer_schema"})
EXPLORER_BARE_TEMPLATE = "explorer/yse_bare.html"


def explorer_shell(request):
    match = getattr(request, "resolver_match", None)
    if match is not None and match.url_name in EXPLORER_BARE_URL_NAMES:
        return {"explorer_parent_template": EXPLORER_BARE_TEMPLATE}
    return {}


def explorer_no_permission(request):
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    raise PermissionDenied
