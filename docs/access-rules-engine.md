# Access rules engine (developer notes)

How the per-point access rules of [#648](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/648) are stored, evaluated and written. User-facing behaviour is in [`data-access-rules.md`](data-access-rules.md). Everything here is inactive while `DATA_ACCESS_GRANTS_ENABLED` is off (the default): the read hooks return "no rules" and every read is the collaboration-group rule with the same SQL.

## Tables (`YSE_App/models/data_access_grant_models.py`)

| Model | Purpose |
|---|---|
| `DataAccessRule` | One allow / deny for one grantee (group or user) on one `TransientPhotData` point or one `TransientSpectrum`. Copies `photometry` and `transient` for set- and transient-level queries. Four unique pairs give one rule per (target, grantee). `granted_as`: `pi`, `program_deputy`, `group_deputy`, `staff`, or `kept` (written when a group is removed going forward). `updated_at` is when an allow was shared, which the former-member rule uses. |
| `StandingAccessGrant` | A program (`Allocation`) or program-night action applied to points that arrive later. `stopped_at` applies forward in time. A grant stopped at or before its `created_at` was stopped backwards and covers nothing. |
| `GroupMembershipPeriod` | Dated group membership (`joined_at` null: since before tracking), written by `m2m_changed` on `User.groups` (`services/access_history.py`). |
| `DatasetGroupShare` | When a group was added to a header's or spectrum's own `groups`. No row means before tracking. |
| `AccessRulesVersion` | One-row counter. Every rule, standing-grant or membership write bumps it to at least the current time in microseconds, so a rolled-back bump is never reused. |
| `AllocationDelegate` | Program deputies. |

Program links:
- `TransientPhotData.allocation` (per point, #714, with no DB constraint) and `TransientSpectrum.allocation`;
- `TransientPhotometry.allocation` is only the default for its points (`access_actions.point_program_q`).

Database checks say "at most one" grantee and target. On MySQL, Django nulls a nullable CASCADE FK just before deleting the row, so "exactly one" is enforced in `clean()` instead.

## Read path (`services/access_rules.py`, `services/visibility.py`)

Every photometry and spectrum read goes through:
- `photometry_visible_q` / `spectrum_visible_q` (headers and spectra);
- `visible_photometry_ids_q` / `photometry_points_visible_q` (points);
- or the services built on them (`data/PhotometryService.py`, `data/SpectraService.py`).

The hooks `_photometry_access_rules` / `_spectrum_access_rules` return `AccessRules(allow, deny)`.

**Per point:**

```
visible = PUBLIC | PA | (~PD & ~GD & (BASE | GA | XA | XB))
```

The terms:
- **PA / PD:** the user's own allow or deny.
- **GA / GD:** an allow or deny for one of the user's current groups.
- **XA:** an allow for a group the user left, made before they left.
- **XB:** the header's (or spectrum's) groups include a group the user left, and the point was shared with it before they left.
- **BASE:** the header's groups intersect the user's groups.
- **PUBLIC:** the header is public (never restricted).

Each rule term is a correlated `EXISTS` on the `(target, grantee)` unique index.

**Headers:**
- A header is listed when at least one of its points is visible, or by its groups when it has no points.
- Point reads always apply the point condition, so a viewer gets exactly their points.
- `hidden_photometry_exists` is true when any point (or a pointless header) is hidden. It drives the PhotStat fallback.

**Fast path and cache.** `summary_for(user)` returns a `RulesSummary`:
- which effects the user has personally;
- which of their groups have rules;
- the groups they left.

It is cached in the Django cache under the database `AccessRulesVersion` and memoised on the user object for the request. The version is read once per request, and only with the flag on. A user to whom nothing applies gets `NO_RULES`, which means today's SQL.

**Other cache keys.** `group_access_plot_cache_token` adds the version, plus a per-user digest for users with personal rules or left groups.

**Service readers.** `user_sees_everything(user)` is true only for service readers: usernames in `DATA_SERVICE_READER_USERNAMES` or `API_SERVICE_USERNAMES` (#706). Staff and superusers follow the rules like everyone. The collaboration surfaces (`user_can_view_transient`, `filter_transients_by_user_access`: comments, interests, favorites, annotations) still let staff through. `filter_transients_by_data_access` is the strict variant.

## PhotStat (`services/photstat.py`, `services/photstat_visibility.py`)

- **Stored scopes.** Group scopes include the group's point rules (`scope_point_sets(..., point_rules)`): a group's allows are added and its denies removed. Public points are in every scope.
- **Candidate scopes.**
  - On a transient where the user has a personal deny, only the public scope is a candidate.
  - Where a deny comes from one of the user's groups `h`, the user's other groups' scopes are dropped there.
- **Exact values.** Table cells and the detail page compute exact values from the user's visible points when rules apply (`_exact_viewer_stats`). List sorting and filters use the best stored scope.
- **Invalidation.** A group rule change deletes that group's scope rows for the affected transients and queues `photstat.sync_scopes` (`photstat.invalidate_scopes`).
- **When the flag is first enabled,** run `backfill_photstat_scopes --apply` once.

## Writes (`services/access_actions.py`)

- **Actions:**
  - `program_action` (program or night, and saves a standing grant);
  - `dataset_action`;
  - `object_night_action` (all of an object's points from one night, any filter, plus spectra);
  - `write_rules`, the bulk writer, which skips public data and replaces the grantee's rule for explicit actions.
- **Turning off:** `stop_standing_grant(backwards=)`, `remove_rules(effect=, backwards=)`, `remove_dataset_group(backwards=)`.
  - The forward default writes `kept` rules via `keep_after_group_removal`, which is called from the `access_history` m2m receiver, so every path to `groups.remove()` is covered.
- **Permissions:** `dataset_role` (the `data_sharing.share_role` PI or deputy, the program role on the point's or header's program, or staff) and `program_role` (allocation PI, program deputy, staff).
- **At ingest:**
  - `on_point_created` (`post_save` of `TransientPhotData`) and `apply_standing_grants_to_set` (after the bulk point writer in `data_utils`, and on linking) apply the standing grants of each point's program.
  - Night grants are applied before program-wide ones, and an existing rule for the grantee is never replaced.
  - Stopped grants still cover points whose `obs_date` is before the stop.
  - Spectra use `apply_standing_grants_to_spectrum`.
- **Program links** (`services/program_links.py`): `link_at_ingest` (follow-up or observed-block derivation), `link_dataset`, upload `allocation`, `backfill_data_programs`, and `migrate_point_programs` (copies header links onto points).
- **Pages and API:** `access_views.py` and `services/access_pages.py` hold the dashboard, program, object and turn-off handlers. The `api_views.Access*APIView` classes and `DatasetProgramLinkAPIView` are the API.

## Tests

- `test_access_rules_schema.py`: schema and history.
- `test_access_rules_engine.py`: precedence, cache, PhotStat, actions.
- `test_access_program_links.py`: program links.
- `test_access_ui.py`: pages and API.
- `test_access_dashboard.py`: forward and backwards turn-off, the dashboard.
- `test_per_point_access.py`: exact points, point programs.
- `test_staff_visibility.py`: staff equals outsider.
- `test_central_read_path.py`: flag-off equivalence and the hook shape.

Query budgets: users with no rules make the same queries as with the flag off, plus one version read per request.
