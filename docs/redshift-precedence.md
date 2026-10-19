# Redshift provenance and precedence

Issues [#436](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/436) (NED) and
[#437](https://github.com/Young-Supernova-Experiment/YSE_PZ/issues/437) (DESI); code in
`YSE_App/services/redshift_precedence.py`.

## Rules (Ryan, 2026-10-18)

One source vocabulary and one precedence list serve both `Host.redshift` and `Transient.redshift`.

1. **Override.** A redshift set by hand with `redshift_override` True wins over every catalogue. No later query or upload overwrites it.
2. **Source rank**, highest first:

| Rank | `redshift_source` | Where it comes from |
| --- | --- | --- |
| 60 | `NED` | NED::API (`YSE_App/services/ned.py`) |
| 50 | `DESI DR1` (any `DESI ...`) | Astro Data Lab TAP (`YSE_App/services/desi.py`) |
| 40 | `YSE spectrum`, `manual` | our own spectra; values entered by hand |
| 30 | any other label | Prost, Sherlock, Hermes, `ANTARES <catalogue>`, ... |
| 25 | blank | no recorded source (rows written before provenance existed) |
| 10 | `TNS` | TNS (crons and the #438 re-sync) |
| 0 | any label containing `photo` | photometric redshifts |

3. **Photo-z only when there is no spec-z.** A photometric source fills only an empty field.
4. **Every automatic write records its source.** NED, DESI, the TNS re-sync, the TNS crons, Prost, Sherlock, ANTARES and Hermes all set `redshift_source`.

An automatic writer replaces a value only when its rank is at least the current one. The same rank counts, so a catalogue can refresh its own value. A blank source ranks below our own and above TNS, so NED and DESI replace stale GHOST or legacy values, and TNS never replaces a value it did not write. An upload without a `redshift_source` counts as blank.

`best_redshift(transient)` gives the value to use under these rules: an override first, then the higher-ranked of the transient's and the host's spec-z, then the host photo-z. The tables and `Transient.z_or_hostz()` still prefer the transient's own value.

## Setting an override

- Command: `manage.py redshift_override --transient 2025abc --z 0.0312 --err 0.0005`, or `--host <id>`. `--clear` drops the flag and keeps the value.
- API: `PATCH /api/transients/<id>/` or `/api/hosts/<id>/` with `redshift`, `redshift_source` (for example `manual`) and `redshift_override: true`.

## Catalogue lookups

- **NED:** `ConeSearchByPosition` at the host position, within `NED_HOST_MATCH_ARCSEC` (3"). The match is the nearest object of type `G` with a spectroscopic `z flag` (`S...`, no `?`). Its uncertainty comes from `RedshiftsOfObject` (the fiducial row).
- **DESI:** an ADQL cone (`q3c_radial_query`) on `DESI_TABLE` (`desi_dr1.zpix`), within `DESI_HOST_MATCH_ARCSEC` (1.5"). Quality cuts: `zwarn = 0`, `spectype = 'GALAXY'`, `zcat_primary`, and `deltachi2 >= DESI_MIN_DELTACHI2` (25). For a later release, change `DESI_TABLE` and `DESI_SOURCE_LABEL` together.
- Only hosts are written. A transient's own redshift is never set from a nearby galaxy.
- Each check is an `ExternalServiceRun` of `ned-redshift` or `desi-redshift`, targeted at the host. The hourly sweep (`redshifts.sweep`) and the backfill (`manage.py host_redshifts`) both resume from these runs.

## Change log

Every write that changes a redshift is one `ValueChange` row (#737): the field, the old and new value and source, the writer (`ned-redshift`, `desi-redshift`, `tns`, `prost-host`, `Hermes`, `api`, `manual`, ...), its `ExternalServiceRun` when it has one, the time, and the actor (empty for the system). `apply_redshift`, `set_override` and `clear_override` record it themselves; a write that changes nothing records nothing. The same log holds TNS classification changes, Prost host links and photo-z updates. It is shown in the History tab of the transient page and at `GET /api/transients/<id>/history/`. Entries about catalogue values are shown to everyone who sees the object listed; overrides and values from our own spectra only to those who may see the object's data.
