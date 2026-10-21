# Favorite transients, activity notifications and the notification-center leftovers

Part of the SkyPortal-parity notification center (#320). This page covers the pieces that landed after
the inbox, the preference matrix and the mentions (#347): **favorite transients with activity
notifications** (#323), the **Slack direct-message channel and the REST notifications API** (#321),
and the **paper-interest column** on the transient tables (#290). SkyPortal's `Favorites` handler
(BSD-3-Clause) was read for the behaviour; no code was copied, so there is no third-party notice to add.

## Favorites: star a transient, hear about what happens to it

| piece | where |
|---|---|
| `UserFavoriteTransient` | `YSE_App/models/favorite_models.py`: `user`, `transient`, `created`; one row per star (unique together) |
| service | `YSE_App/services/favorites.py`: `add`, `remove`, `toggle`, `favorite_state` (mine + count, one query), `favorite_ids`, `favorites_for_user`, `favoriters`, `record_activity` and the `on_*` hooks |
| star | the transient page header (next to the name, with the number of users who starred it); the **Fav.** column of every `TransientTable` (dashboard, personal dashboard, search) and of `/my/favorites/` |
| pages | `/my/favorites/` (user menu) with the shared table columns plus *Starred*; a **My favorites** box at the top of the personal dashboard, loaded after the page like the saved-query sections (`/my/favorites/section/`) |
| search | `favorites=true|false` (*My favorites only*) and `has_interest=true|false` (*Has open paper interest*) in the Relations group of `/search/` and `/api/transients/` |
| endpoints | `POST /favorites/toggle/<transient_id>/` (`state=on|off` forces one; JSON for XHR, redirect otherwise), `GET /my/favorites/ids.json` |
| API | `/api/favorites/` (list mine, `POST {"transient": id}`, `DELETE /api/favorites/<id>/`, `?transient=<id|name>`) and `POST /api/favorites/toggle/ {"transient": id}` -> `{favorite, count}` |
| admin | `UserFavoriteTransientAdmin` |

Starring is personal (nobody else sees who starred what) and needs read access to the transient, like a
comment. Table renders stay query-free: the stars are rendered *state-unknown* and painted by one fetch
of `/my/favorites/ids.json` per page (`base.html`; a `MutationObserver` covers sections loaded later);
the header star on the transient page is rendered with its state (one aggregate query, budget +1 in
`test_performance`).

### Activity events

Each of these on a starred transient becomes one *event* for its favoriters, minus the person who
caused it (`YSE_App/signals.py` and `services/photstat.py` call the hooks; a failing hook is logged
and never breaks the save):

| event | source | text |
|---|---|---|
| comment | `Log` saved with a transient (`create_transient_comment` records it once the audience groups are set; a `Log` saved any other way at `post_save`) | `new comment: "<first 140 chars>"`; a comment the favoriter may not read is announced as *new comment (restricted to collaboration groups)* |
| status / class / redshift | the auditlog entry `Transient.save()` writes (`status`, `best_spec_class`, `photo_class`, `redshift`, `TNS_spec_class`) | `status changed New -> Following; redshift set to 0.031` |
| spectrum | `TransientSpectrum` created | `new spectrum from Binospec (observed 2026-09-29)` |
| photometry | the stat recompute (`photstat.recompute`) finding more detections than the stored row (`num_det_global` grew; bulk rebuilds do not notify) | `12 new photometry points, latest 17.40 r`; consecutive photometry events merge into one line |
| follow-up | `TransientFollowup` created, or its status changed (a pre-save hook keeps the old status name) | `follow-up requested on MMT (Requested)`, `follow-up on MMT: status Requested -> Successful` |

Events are delivered through `notify()` with kind `favorite_activity`, preference group **Favorite
transients** (`/notifications/preferences/`; in-app, email and Slack on by default). **Batching**: an
unread `favorite_activity` notification for the same recipient and transient younger than
`FAVORITE_ACTIVITY_BATCH_MINUTES` (default 60, `[site_settings]` in `settings.ini`; 0 = one row per
event) collects the new event (`payload["events"]`, text rebuilt as *N updates on <name>*), and the
email / Slack delivery job of a fresh row is created with `run_after` = now + the window, so the
message that goes out carries every event of the hour. A row the user already read is not extended.

Acceptance (#323): star a transient, add a comment as another user -> the starrer has exactly one
unread in-app notification; a second comment within the hour extends it instead of adding a row
(`YSE_App/tests/test_favorites.py`).

## Notification center leftovers (#321)

* **Slack direct messages.** `NotificationPreference.slack_user_id` (a Slack member id, `U…`) is a
  new field on `/notifications/preferences/`, next to the webhook URL. **Look up from my email** calls
  `users.lookupByEmail` through the site's Slack app (`integrations/slack/client.py`, form-encoded)
  and stores the id; it needs `SLACK_BOT_TOKEN` with the `users:read.email` scope. Delivery is the
  `slack_dm` channel in `services/notify.py` (`chat.postMessage` to the member id, scope `chat:write`
  / `im:write`); it follows the **Slack** column of the preference matrix like the webhook, and is
  recorded in `Notification.delivered["slack_dm"]` with the usual retry on failure. The id lives on
  the preference row rather than `Profile` because a `Profile` row requires the phone fields and most
  accounts have none.
* **REST.** `/api/notifications/` lists the requesting user's notifications (`?unread=1`, `?kind=`,
  `?transient=`), `GET /api/notifications/unread_count/`, `POST /api/notifications/<id>/read/`,
  `POST /api/notifications/read_all/`. Other users' rows answer 404. The navbar badge keeps using the
  lighter `/notifications/unread_count.json`.

## Paper-interest column (#290)

`TransientTable` (and therefore the dashboard, personal dashboard and search tables) has a **Papers**
column: the number of open interests (planned / in progress / submitted) on the row, as a badge, sortable.
It comes from a correlated subquery added in `TransientTable.__init__` (`annotate_open_interest_count`,
next to `annotate_peak_mag`), so it costs no extra query. Both new columns sit last so the DataTables
column indexes the templates rely on are unchanged.

## Operator steps

* `manage.py migrate` (migration `0025_favorites_slack_dm`: the `YSE_App_userfavoritetransient` table and
  `notificationpreference.slack_user_id`) runs in the deploy workflow; nothing to do by hand.
* Optional: `FAVORITE_ACTIVITY_BATCH_MINUTES = 60` under `[site_settings]` in `settings.ini` to change
  the batching window.
* Optional, for Slack DMs: give the Slack app the `users:read.email`, `chat:write` and `im:write`
  scopes and set `SLACK_BOT_TOKEN` in the web environment (already needed for the comment mirror).
  Without the token the field is shown but the lookup button is disabled and no DM is attempted.
* The delivery jobs run with the existing `run_jobs` cron / loop (see `background-jobs.md`); a batched
  favorite notification's job simply has a `run_after` an hour ahead.
