"""Selections: a saved set of criteria for transients, shared by owner, team or all YSE (#841).

A Selection stores *criteria*, never results and never per-object decisions (no "seen",
"skipped" or "reviewed" state). Its rows are compiled on every use, for the viewing user, by
``YSE_App.services.selections``; data access stays per point, so a Selection never lets
anyone see a transient, photometry point or spectrum they could not see anyway.

The criteria come from one of these ``kind`` values:

* ``search``: ``search_params``, the transient search page's query string
  (``YSE_App.filters.transient_search``);
* ``rules``: ``rules``, a JSON rule tree (built by #843; no compiler yet);
* ``sql``: ``query``, a saved Explorer query (run for the viewer, scoped like the dashboards);
* ``function``: ``python_query``, a registered Python query (``queries/yse_python_queries.py``).

``user_query`` optionally links the personal-dashboard panel the Selection was made from.
Existing ``UserQuery`` rows and panels are never changed.

Every foreign key to a table develop knows carries the matching MySQL ``ON DELETE``
(migration 0066, ``YSE_App.common.fk_on_delete``), so develop's deletes on the shared
database keep working.
"""

from django.contrib.auth.models import Group, User
from django.db import models

from YSE_App.models.fields import JSONTextField


class Selection(models.Model):
    VISIBILITY_ME = "me"
    VISIBILITY_TEAM = "team"
    VISIBILITY_ALL = "all"
    VISIBILITY_CHOICES = (
        (VISIBILITY_ME, "Only me"),
        (VISIBILITY_TEAM, "My team"),
        (VISIBILITY_ALL, "All YSE"),
    )

    KIND_SEARCH = "search"
    KIND_RULES = "rules"
    KIND_SQL = "sql"
    KIND_FUNCTION = "function"
    KIND_CHOICES = (
        (KIND_SEARCH, "Search filters"),
        (KIND_RULES, "Rules"),
        (KIND_SQL, "SQL query"),
        (KIND_FUNCTION, "Built-in list"),
    )

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=80, unique=True)
    description = models.TextField(blank=True, default="")
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="selections")
    visibility = models.CharField(max_length=8, choices=VISIBILITY_CHOICES, default=VISIBILITY_ME)
    team = models.ForeignKey(Group, null=True, blank=True, on_delete=models.SET_NULL, related_name="selections",
                             help_text="The team that sees and edits this Selection (visibility 'My team').")

    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_SEARCH)
    search_params = models.TextField(blank=True, default="",
                                     help_text="Transient search query string (kind 'search').")
    rules = JSONTextField(null=True, blank=True, help_text="Rule tree as JSON (kind 'rules').")
    query = models.ForeignKey("explorer.Query", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="selections", help_text="Saved Explorer query (kind 'sql').")
    python_query = models.CharField(max_length=64, blank=True, default="",
                                    help_text="Registered Python query name (kind 'function').")
    user_query = models.ForeignKey("YSE_App.UserQuery", null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="selections",
                                   help_text="The dashboard panel this Selection was made from, if any.")

    created_date = models.DateTimeField(auto_now_add=True)
    modified_date = models.DateTimeField(auto_now=True)
    modified_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ("name", "pk")

    def __str__(self):
        return "Selection: %s" % self.name

    # Convenience wrappers; the rules live in YSE_App.services.selections.
    def queryset(self, user):
        from YSE_App.services.selections import selection_queryset

        return selection_queryset(self, user)

    def can_view(self, user):
        from YSE_App.services.selections import can_view

        return can_view(user, self)

    def can_edit(self, user):
        from YSE_App.services.selections import can_edit

        return can_edit(user, self)

    def can_delete(self, user):
        from YSE_App.services.selections import can_delete

        return can_delete(user, self)
