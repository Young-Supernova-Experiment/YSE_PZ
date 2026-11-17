# Combined candidate list (#849)

One list built from several sources, for UI preview users (`combined-list/`, sidebar
Observe → "Combined candidate list"). Without the preview the page is a 404.

## Sources (the user picks them)

| Source | Rows | "Why it's here" |
|---|---|---|
| Selections (#841) | `selection_queryset(selection, viewer)`; only Selections the viewer may see | `Selection · <name>` |
| My dashboard queries | the viewer's own `UserQuery` panels, under the dashboards' SQL rules for the viewer | `Dashboard · <title>` |
| Tags | the tag page's list for each tag | `Tag · <name>` |
| Recent follow-up requests | follow-ups the viewer may see, created or re-requested in the last N nights (N x 24 h; default 3, 0 = off, at most 30) | `Requested in the last N nights` |
| TNS | TNS-named objects whose last detection (as the viewer may see it) is brighter than 16 mag and within the last 30 days | named only when no other source brought the object in |

Defaults (no choice yet): the viewer's own Selections, all their dashboard queries, 3 nights of
requests and TNS; no tags.

## Rules

- **Stateless.** Nothing is stored on the server: no skipped, reviewed or watched state, no score.
  The choice is in the query string (`src=1&sel=<slug>&uq=<id>&tag=<id>&nights=N&tns=1`); the
  browser keeps the last choice in localStorage (`yse-combined-list:<user id>`), and
  `?src=default` forgets it.
- **Each object once**, with every source that brought it in. The union is one queryset
  (`pk IN (source subquery) OR ...`) over the search page's base rows, so the listing rule
  (`filter_listed_transients`) always applies.
- **Columns:** last detection, trend (rising when the newest detection is the brightest, else the
  decay rate since peak), last observation, class, redshift, host, coordinates, discovery date.
- **Evaluation:** links into the transient page carry the list context (#850, `ls=comb`), so J/K
  step through this list; the follow-up form's default comment is
  `In list: Combined candidate list (why: <sources>)`.
- **Observable tonight** (#854) works on the list when it has at most 500 objects.

Code: `YSE_App/services/combined_list.py`, `YSE_App/combined_list_views.py`,
`YSE_App/templates/YSE_App/combined_list.html`, `YSE_App/static/YSE_App/combined-list.js`.

Migrations: none. Server steps: none.
