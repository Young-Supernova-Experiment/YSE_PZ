# Who can see which data: access rules, programs and the data access dashboard

This guide is for PIs, their deputies, program deputies and administrators ([#648](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/648)). The access rules and the pages below are switched on by `DATA_ACCESS_GRANTS_ENABLED`, which is **off by default**. Until it is on, only the collaboration groups described in the first section apply. The developer view is in [`access-rules-engine.md`](access-rules-engine.md); the server steps are in the runbook ([enable access rules](ziggy-runbook.md#enable-access-rules)).

## 1. The basics: access is per point

Access is decided **for each photometry point and each spectrum**, never for a "photometry set". A photometry header (`TransientPhotometry`, one per transient, instrument and observation group) only groups points for storage.

- **Collaboration groups.** A header's `groups` (and a spectrum's own `groups`) are the **default** for its points:
  - a point with no collaboration group is public;
  - otherwise it is visible to the members of its groups.
  - Access rules (below) override the default point by point.
- **Public data can never be restricted.** Public data means points and spectra with no collaboration group (or only `Public`).
  - Everything reported to TNS is public and stays public, but only the points that were actually reported ([#708](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/708) / #711). An accepted report makes exactly those points and that spectrum public. Other points from the same header keep their access.
- **Everyone sees exactly the points they may see.** This covers tables, plots, Bazin and SALT values, exports, the REST API, reports, saved queries and PhotStat values (Last Det., Peak Mag).
  - A header with some visible points is shown with those points only.
  - A transient is listed when it has a TNS name or when you may see at least one of its points or spectra ([#607](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/607)).
- **Staff and superusers** see only the data they have access to, like everyone else ([#706](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/706)).
  - They keep their administrative actions: deciding access requests, the Django admin, acting on access rules as administrators, and moderating comments and follow-ups.
  - The only accounts that read all data are the configured **service readers**, which are cron and script accounts (`DATA_SERVICE_READER_USERNAMES`; see the runbook).
- **Saved Explorer queries behind pages** may read only transient metadata tables for viewers who do not see all data ([#715](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/715) / #716). Derived values may select objects but are not displayed. See [`transient-search.md`](transient-search.md#selection-rule-for-saved-explorer-queries-715).

## 2. Access rules

A rule **allows** or **restricts** one grantee on one point or one spectrum. The grantee is a collaboration group or one person (never `Public`).

**Precedence**, for one person and one point:

1. Public data are always visible.
2. A rule for **that person** decides.
3. Otherwise the rules for **their groups** decide; a restriction beats an allow.
4. Otherwise the point's **default groups** decide.
5. **Leaving a group:** someone who leaves a group keeps the points that were shared with it while they were a member. They do not get points shared with it after they left. Membership is recorded with dates, and memberships that existed before tracking count as "since before tracking".

**Who may set rules:**
- on one dataset or one object's night: the data's PIs and their designated deputies, the program's PI and program deputies (when the data are linked to the program), or an administrator;
- on a whole program or one of its nights: the program's PI, its program deputies, or an administrator.

Every change is logged (django-auditlog).

## 3. Actions: what a click changes

An action writes one rule per matching point and spectrum. Its scope is only a convenient default:

| Action | Covers | Where |
|---|---|---|
| **Program** | every point and spectrum taken under the program (an `Allocation`, one telescope and semester), also those taken later | program page |
| **Night of a program** | the program's data from one night (local noon to noon at the telescope), also those uploaded later | program page |
| **Object night** | all of one object's points from one night, in any filter, and that night's spectra | object page |
| **Dataset** | every point of one header, or one spectrum | object page, dashboard |

- **Standing grants.** A program or night action is also saved as a *standing grant*, so points that arrive later get the rule when they are uploaded. Later uploads never replace a rule set explicitly on a point.
- **Restricting shared data.** You may restrict any non-public data from anyone, including data they can already see. The result says how many items were newly restricted ("shared items now restricted").

## 4. Programs and the program link

- **Program link.** Each photometry point and spectrum can name the program it was taken under. The header's link is the default for its points ([#714](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/714)), so forced photometry from two programs in one header follows each program's rules.
- **Where the link comes from:**
  - the follow-up's allocation, or a matching AstroScheduler observation, at ingest;
  - facility results, which use the request's program;
  - an upload's `allocation` field (an id, a proposal id or a name), when you may link the data;
  - the dataset program endpoint;
  - the optional backfill commands (runbook).
- **Program deputies.** The program's PI (or an administrator) names program deputies on the program page. They may set access for the program's data like the PI. They are separate from a PI's data-sharing deputies.

## 5. Turning sharing off: going forward or backwards

Turning sharing off means one of three things:
- stopping a standing grant;
- turning off an allow;
- removing a group from a data set's own groups.

The default is **going forward in time**: data taken before the change stay visible to that grantee. The **backwards** choice also removes the earlier data, and the page asks you to confirm it. Remember that they may already have looked at or downloaded those data.

| Turning off | Going forward (default) | Backwards |
|---|---|---|
| stop a standing grant | data taken after now are not covered; earlier data keep it, also when uploaded or linked later | every rule it wrote is removed; it covers nothing |
| turn off an allow | data already taken stay visible | the allow rules are removed |
| remove a group from a dataset | the points shared with it before now stay visible to it (and to people who were members then), through *kept* rules | the group loses the earlier points too |

**Lifting a restriction** shares data again, so it has no forward or backward choice.

## 6. The pages

All of these are visible only with `DATA_ACCESS_GRANTS_ENABLED` on.

- **Data access dashboard** (`/data_access/`). For PIs, deputies and program deputies:
  - your programs and their standing access, with a forward or backwards Stop;
  - your restricted data, with their groups (Remove), the rules per grantee (Turn off, Lift restriction) and the automatic-publishing toggle.
- **Program pages** (`/data_access/programs/` and one page per program). Standing access, *Add access* (program, classical night, or a night by date) and program deputies.
- **Object page** (`/transient_detail/<id>/data_access/rules/`, linked from the photometry tab):
  - the object's nights, built from the data you manage, each with one action for all of that night's points and spectra;
  - an action per header or spectrum;
  - the rules on those data.
- **API.** Everything is under `/api/access/...`:
  - program grants: list, create, stop with `backwards`;
  - program deputies;
  - dataset rules: list, create, remove with `effect` and `backwards`;
  - dataset groups: remove;
  - autopublish;
  - an object's nights.

  `/api/datasets/<kind>/<id>/program/` links or unlinks a dataset. These endpoints accept token and Basic auth like the rest of the API.

## 7. Automatic publishing (opt-in)

Restricted data are sent to TNS or Hermes automatically only when their PI or a deputy has opted the dataset in ([#691](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/691)). Use the dashboard toggle or `/api/access/datasets/<kind>/<id>/autopublish/`.

- **Administrators cannot opt in** on a PI's behalf ([#708](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/708)), but they may opt a dataset out.
- **Public data** need no opt-in.
- **Manual reports** send only the data the sender may see and may share.
