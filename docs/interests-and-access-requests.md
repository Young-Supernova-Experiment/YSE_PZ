# Transient interests and data access requests

Two SkyPortal-parity umbrellas: **Transient interests** (#288: #289 model, service, auto-comment and
API; #290 detail-page box and personal list) and **Data access requests** (#291: #292 model, workflow,
API and notifications; #293 detail-page hints and owner inbox). Both sit on the comment model
(`Log`, `services/comments.py`), the group visibility helpers (`services/visibility.py`,
`data/PhotometryService.py`, `data/SpectraService.py`) and `notify()` (#347, #266). SkyPortal's
`source_interest.py` and `data_access_request.py` (BSD-3-Clause) were read for the behaviour; no code
was copied, so there is no third-party notice to add.

## Interests: "I am working on a paper about this transient"

| piece | where |
|---|---|
| `TransientInterest` (alias `SourceInterest`) | `YSE_App/models/interest_models.py`: `transient`, `user`, optional `group` (collaboration the paper is under), `title` (the planned paper, unique per transient + user), `description`, `role` (lead / co-author / observer), `status` (planned, in_progress, submitted, published, withdrawn), `doi`, audit fields; `auditlog` registered |
| service | `YSE_App/services/interests.py`: `register_interest`, `update_interest_status`, `interest_queryset`, `interests_for_user`, `open_interest_counts` |
| pages | "Working on this" box on the summary tab (`templates/YSE_App/transient_detail/interests_panel.html`, next to the comments panel) with the **Register interest** form and per-row status links; `/my/interests/` (user menu) listing your interests with status / DOI editing |
| API | `/api/transientinterests/` (list, create, retrieve, update; `?transient=`, `?user=`, `?mine=1`, `?status=`, `?include_withdrawn=1`) |
| admin | `TransientInterestAdmin` |

Registering an interest:

1. validates the title (non-empty, unique for you on that transient; a withdrawn row with the same
   title is revived instead), the group (one of yours) and that you may see the transient;
2. creates the row;
3. posts the automatic comment **"<you> registered an interest: <title>"** through
   `create_transient_comment`. With a group chosen the comment is private to that group; without one
   it takes the comment helper's default audience (the groups you share with the transient's
   restricted data, or public when there are none). Because it is an ordinary `Log` row it shows in
   the comments panel, goes to the Slack mirror and through the `@mention` flow;
4. notifies the transient's other open interest holders (kind `interest`, preference group
   *Interests and data access*).

Status changes are the owner's (or staff's). *Withdrawn* and *published* post a comment
("<you> withdrew the interest: <title>", "<you> published: <title> (<doi>)"); the other transitions
are silent. Withdrawn rows are hidden on the detail page unless `?interests=all` is added, and on
`/my/interests/` unless *Show withdrawn* is used.

## Data access requests: ask the owners of embargoed photometry / spectra

A `TransientPhotometry` or `TransientSpectrum` row with a non-empty `groups` M2M is visible only to
members of those groups. The detail page used to hide such rows silently; now the Spectra tab, the
Detailed Photometry tab and the Photometry box on the summary tab show

> **2 spectra** on this transient are restricted to **DEBASS**. [Request access]

for every (kind, owner group) with hidden data - counts and owner-group names only, never the rows,
their instruments or dates. The request form asks which of *your* collaboration groups should receive
access (never `Public`) and takes an optional message.

| piece | where |
|---|---|
| `DataAccessRequest` | `YSE_App/models/data_access_models.py`: `requester`, `transient`, `dataset_kind` (photometry / spectrum), optional `dataset_id` (one dataset; empty = every restricted dataset of that kind the owner group holds on the transient), `owner_group`, `target_group`, `message`, `status` (pending / accepted / declined), `decided_by` / `decided_at` / `note`, `granted_dataset_ids`; `auditlog` registered |
| service | `YSE_App/services/data_access.py`: `count_hidden`, `hidden_summary`, `requestable_groups`, `request_access`, `decide`, `requests_to_decide`, `requests_by`, `pending_count_for` |
| pages | hint partial `templates/YSE_App/transient_detail/data_access_hint.html` (included by the spectra fragment, the photometry fragment and the summary tab); `/data_access_requests/` (user menu, with a pending badge) with the tabs **To decide**, **Decided**, **Mine** and Accept / Decline buttons with a note |
| API | `/api/dataaccessrequests/` (list with `?box=mine|inbox`, `?status=`, `?transient=`, `?kind=`; create; `POST .../<id>/accept/` and `.../decline/` with optional `note`) |
| admin | `DataAccessRequestAdmin` |

### Workflow

1. **Request**: `request_access(user, transient, kind, owner_group, target_group=, message=)`
   checks the owner group really holds hidden data of that kind for the user, that the target group
   is one of the user's non-Public groups (chosen automatically when there is exactly one) and that no
   pending duplicate exists. Every active member of the owner group gets a notification (kind
   `data_access`) with the request text and a link to the inbox; email / Slack follow their
   preferences (the *Interests and data access* group defaults to in-app + email + Slack).
2. **Decide**: members of the owner group (and staff) see the request under *To decide*.
   **Accept** adds `target_group` to the `groups` of the matching datasets and records their ids in
   `granted_dataset_ids`; **Decline** stores the note. The requester is notified either way (accept
   links to the transient, decline to the inbox).
3. After acceptance the data appears wherever the requester's group membership already applies:
   detail-page fragments, plots, downloads and the REST endpoints, with no other change.

### Why the grant is a group on the dataset, not a per-request table

Adding the requester's group to the dataset's `groups` reuses the one mechanism every access check in
the code base already enforces (`GetUserGroupQuery` in the data services, the visibility helpers,
the DRF `get_queryset` overrides, the plot cache token keyed by group names, the export code). A
per-request grant table would have made all of those consult a second source of truth; missing one
would either leak data or hide it inconsistently. The group add is narrowed and audited instead:

- the target is always a collaboration group the requester belongs to and never `Public`, so an
  acceptance cannot make data world-readable by accident;
- the owner sees exactly which group will gain access before accepting;
- `granted_dataset_ids` on the request records what was shared, so a grant can be reversed by
  removing the group again (admin, or a future *Revoke* action);
- the owner group keeps its access (the M2M only grows).

Staff are not special in the hidden counts: the data services filter by group name, so a staff user
outside the owning group is hidden from too and sees the same hint (they can add themselves to the
group in the admin or request like anyone else if they have a collaboration group).

## Notifications

Both features send through `notify()` with kinds `interest` and `data_access`, which moved from the
*System and jobs* preference group to a new **Interests and data access** group
(`KIND_GROUPS` in `models/notification_models.py`) whose defaults are in-app, email and Slack on.
Users tune it on `/notifications/preferences/`. No settings were added.

## Tests

`YSE_App/tests/test_interests_data_access.py`: model defaults and the `SourceInterest` alias,
uniqueness and revival, the automatic comment and its audience, notifications to other holders,
status comments, permissions, the detail-page box (zero, several, withdrawn hidden), the register /
status forms, `/my/interests/`, the REST endpoint; hidden counts and summary, request validation
(kind, owner group, target group, Public, duplicates, staff without a group), owner notifications and
email jobs, decision permissions, acceptance verified through `SpectraService` /
`PhotometryService` / `user_can_view_transient` and the spectra fragment, single-dataset grants,
decline, preference handling, the hint on the fragments and summary tab, the inbox page, the admin
pages and the REST actions.

## Deploy

- Migration `0020_interests_data_access` (two tables, six indexes; no data migration).
- No new settings or packages.
