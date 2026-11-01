"""Viewer-scoped SQL Explorer queries (#723): the hooks into django-sql-explorer 2.3.

Every Explorer page that can run a query (query detail, playground, download,
stream, the CSV/JSON exports and the email export) goes through
``PermissionRequiredMixin.dispatch``, and every query Explorer runs goes through
``QueryResult.execute_query``. :func:`install` (called from
``YseAppConfig.ready``) wraps both:

* ``dispatch`` records the request's user as the viewer while the view runs;
* ``execute_query`` replaces the SQL with ``explorer_scope.scope_for_viewer``
  for that viewer (refused queries show their reason as the Explorer error),
  under the viewer time cap. A query run outside an Explorer request has no
  viewer and is refused, so nothing can run unscoped by accident.

It also limits the schema browser to the tables the viewer may read, shows a
saved query's author by username (not email address), and, for users who have
Explorer only because it is open to everyone, refuses a saved-query title that
another query already uses (``/query_api/`` and the pages find saved queries
by title).
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager

from django.db import DatabaseError

_UNSET = object()
_VIEWER = contextvars.ContextVar('yse_explorer_viewer', default=_UNSET)


class ExplorerScopeError(DatabaseError):
    """A query Explorer may not run for this viewer (shown as the query error)."""


@contextmanager
def explorer_viewer(user):
    token = _VIEWER.set(user)
    try:
        yield
    finally:
        _VIEWER.reset(token)


def current_viewer():
    """The viewer of the Explorer request being served, or ``_UNSET`` outside one."""
    return _VIEWER.get()


def _install_dispatch():
    from explorer.views import auth as explorer_auth

    original = explorer_auth.PermissionRequiredMixin.dispatch

    def dispatch(self, request, *args, **kwargs):
        with explorer_viewer(getattr(request, 'user', None)):
            return original(self, request, *args, **kwargs)

    explorer_auth.PermissionRequiredMixin.dispatch = dispatch


def _install_execute():
    from explorer import models as explorer_models

    from YSE_App.services.explorer_scope import ExplorerQueryRefused, scope_for_viewer, viewer_time_cap

    original = explorer_models.QueryResult.execute_query

    def execute_query(self):
        viewer = _VIEWER.get()
        if viewer is _UNSET:
            raise ExplorerScopeError('SQL Explorer queries run only from Explorer pages, for a signed-in viewer.')
        try:
            plan = scope_for_viewer(self.sql, viewer)
        except ExplorerQueryRefused as exc:
            raise ExplorerScopeError('Query refused: %s.' % exc.args[0]) from None
        self.sql = plan.sql
        if not plan.rewritten:
            return original(self)
        with viewer_time_cap(self.connection, plan.time_cap_ms):
            return original(self)

    explorer_models.QueryResult.execute_query = execute_query


def _install_schema():
    from explorer.views import schema as explorer_schema_view

    from YSE_App.services.explorer_scope import tables_for_viewer

    original = explorer_schema_view.schema_info

    def schema_info(connection_alias):
        info = original(connection_alias)
        viewer = _VIEWER.get()
        if not info:
            return info
        allowed = frozenset() if viewer is _UNSET else tables_for_viewer(viewer)
        if allowed is None:
            return info
        return [(table, columns) for table, columns in info if str(table).lower() in allowed]

    explorer_schema_view.schema_info = schema_info


def _install_form():
    from explorer import forms as explorer_forms
    from explorer.models import Query

    from YSE_App.common.authz import explorer_access_allowed

    def created_by_user_email(self):
        user = self.instance.created_by_user if self.instance else None
        return user.get_username() if user else '--'

    explorer_forms.QueryForm.created_by_user_email = property(created_by_user_email)

    original_clean = explorer_forms.QueryForm.clean

    def clean(self):
        data = original_clean(self)
        viewer = _VIEWER.get()
        if viewer is not _UNSET and not explorer_access_allowed(viewer):
            title = (self.cleaned_data.get('title') or '').strip()
            others = Query.objects.filter(title__iexact=title)
            if self.instance is not None and self.instance.pk is not None:
                others = others.exclude(pk=self.instance.pk)
            if title and others.exists():
                self.add_error('title', 'Another saved query already has this title; choose a different one.')
        return data

    explorer_forms.QueryForm.clean = clean


_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _install_dispatch()
    _install_execute()
    _install_schema()
    _install_form()
    _installed = True
