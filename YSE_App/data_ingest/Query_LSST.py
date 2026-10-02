"""
Rubin / LSST photometry from the ANTARES broker (issue #224).

``AntaresLSST`` is the Rubin analogue of ``Query_ZTF.AntaresZTF``. Where the ZTF
cron discovers new objects in YSE survey fields, this one refreshes the light
curves of transients YSE already cares about: for each recently active
transient it runs an ANTARES cone search around the YSE position, keeps the
LSST alerts on the matched loci and writes them as ordinary
``TransientPhotometry`` / ``TransientPhotData`` rows on the ``LSSTCam``
instrument, so the detail-page light curve, ``recent_mag`` and the scheduling
tables pick them up with no further changes.

Field mapping (verified against the ``antares-client`` repository's own LSST
API fixtures, ``test/data/api_responses/lsst-loci-*.json``; the ANTARES web
docs are not reachable from the build environment):

* ``locus.lightcurve`` is a pandas DataFrame with the columns ``time``,
  ``alert_id``, ``ant_mjd``, ``ant_survey``, ``ant_ra``, ``ant_dec``,
  ``ant_passband``, ``ant_mag``, ``ant_magerr``, ``ant_maglim``. ``ant_survey``
  is ``1`` for a ZTF candidate, ``2`` for a ZTF upper limit and ``4`` for an
  LSST alert (``alert_id`` prefix ``lsst:``). Only rows whose ``ant_survey``
  equals ``[antares] lsst_survey_id`` (default 4) and whose ``ant_mag`` is
  finite are ingested; upper limits (NaN ``ant_mag``) are skipped exactly as
  the ZTF path does.
* ``locus.properties['survey']['lsst']['dia_object_id']`` marks an LSST locus.
* Per-alert quality flags live in ``alert.properties`` under the LSST alert
  schema names prefixed ``lsst_diaSource_``: ``pixelFlags`` (any bad pixel
  flag), ``isNegative`` and ``reliability``. Points that fail them are stored
  with the ``Bad`` ``DataQuality`` flag (which ``recent_mag`` already excludes)
  instead of being dropped.

Everything ANTARES-specific is a module constant or a ``[antares]``
``settings.ini`` key with a safe default (see ``LSSTIngestConfig``), so a field
rename on the broker side is a config change, not a code change.

The ``antares_client`` import is guarded in the broker provider
(``YSE_App.brokers.antares``, issue #273; like ``TNS_uploads``, PR #194): the
web venv can import this module and ``manage.py runcrons`` without the package;
the cron then logs that it is disabled and exits cleanly.
"""

from __future__ import annotations

import configparser
import datetime
import math
import smtplib
import sys
import time
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, Iterable, List, Optional

import numpy as np
from astropy.time import Time
from django.conf import settings as djangoSettings
from django.contrib.auth.models import User
from django.db.models import Q
from django.utils import timezone
from django_cron import CronJobBase, Schedule

from YSE_App.common.collaboration_groups import (
    PUBLIC_COLLABORATION_GROUP_NAME,
    apply_collaboration_groups_to_photometry,
    get_or_create_public_group,
)
from YSE_App.common.filter_display import FILTER_COLORS
from YSE_App.models import (
    DataQuality,
    Instrument,
    ObservationGroup,
    Observatory,
    PhotometricBand,
    Telescope,
    Transient,
    TransientFollowup,
    TransientPhotData,
    TransientPhotometry,
)

# The ANTARES client lives in the broker provider (YSE_App.brokers.antares,
# issue #273); this module keeps the Rubin-specific parsing and storage and the
# cron. ``antares_available()`` reads the provider's guarded import at call time.
from YSE_App.brokers import antares as _antares_provider


def antares_available() -> bool:
    return bool(_antares_provider.HAS_ANTARES)


# --- Rubin instrument stack ----------------------------------------------------

OBSERVATORY_NAME = "Cerro Pachón"
OBSERVATORY_DEFAULTS = {"utc_offset": -4, "tz_name": "America/Santiago"}
TELESCOPE_NAME = "Rubin Observatory / Simonyi Survey Telescope"
TELESCOPE_DEFAULTS = {"latitude": -30.2446, "longitude": -70.7494, "elevation": 2663.0}
INSTRUMENT_NAME = "LSSTCam"
INSTRUMENT_DESCRIPTION = "Rubin Observatory LSST Camera (alerts via the ANTARES broker)"
OBS_GROUP_NAME = "LSST"
LSST_BANDS = ("u", "g", "r", "i", "z", "y")
PLOT_SYMBOL = "plus"
BAD_DATA_QUALITY_NAME = "Bad"

# Flux is stored on the same AB zero point the ZTF ingest uses.
FLUX_ZERO_POINT = 27.5

# --- ANTARES field names (see module docstring) ---------------------------------

LC_COL_ALERT_ID = "alert_id"
LC_COL_SURVEY = "ant_survey"
LC_COL_MJD = "ant_mjd"
LC_COL_PASSBAND = "ant_passband"
LC_COL_MAG = "ant_mag"
LC_COL_MAGERR = "ant_magerr"
LOCUS_SURVEY_KEY = "lsst"  # locus.properties['survey'][LOCUS_SURVEY_KEY]
ALERT_ID_PREFIX = "lsst:"
ALERT_PIXELFLAGS_KEY = "lsst_diaSource_pixelFlags"
ALERT_IS_NEGATIVE_KEY = "lsst_diaSource_isNegative"
ALERT_RELIABILITY_KEY = "lsst_diaSource_reliability"


@dataclass
class LSSTIngestConfig:
    """``[antares]`` keys read from ``settings.ini``; every one has a default."""

    survey_id: int = 4
    cone_radius_arcsec: float = 2.0
    max_days: float = 30.0
    statuses: List[str] = field(
        default_factory=lambda: ["New", "Following", "Watch", "FollowupRequested", "Interesting"]
    )
    max_transients: int = 500
    max_dec: float = 32.0
    mjd_match_min: float = 0.0005
    min_reliability: float = 0.0
    use_alert_flags: bool = True

    @classmethod
    def from_config(cls, config: Optional[configparser.RawConfigParser]) -> "LSSTIngestConfig":
        cfg = cls()
        if config is None or not config.has_section("antares"):
            return cfg
        get = config.get
        try:
            cfg.survey_id = config.getint("antares", "lsst_survey_id", fallback=cfg.survey_id)
            cfg.cone_radius_arcsec = config.getfloat(
                "antares", "lsst_cone_radius_arcsec", fallback=cfg.cone_radius_arcsec
            )
            cfg.max_days = config.getfloat("antares", "lsst_max_days", fallback=cfg.max_days)
            statuses = get("antares", "lsst_statuses", fallback=",".join(cfg.statuses))
            cfg.statuses = [s.strip() for s in statuses.split(",") if s.strip()] or cfg.statuses
            cfg.max_transients = config.getint(
                "antares", "lsst_max_transients", fallback=cfg.max_transients
            )
            cfg.max_dec = config.getfloat("antares", "lsst_max_dec", fallback=cfg.max_dec)
            cfg.mjd_match_min = config.getfloat(
                "antares", "lsst_mjd_match_min", fallback=cfg.mjd_match_min
            )
            cfg.min_reliability = config.getfloat(
                "antares", "lsst_min_reliability", fallback=cfg.min_reliability
            )
            cfg.use_alert_flags = config.getboolean(
                "antares", "lsst_use_alert_flags", fallback=cfg.use_alert_flags
            )
        except (ValueError, configparser.Error) as e:
            print(f"[AntaresLSST] bad [antares] lsst_* value in settings.ini ({e}); using defaults")
            return cls()
        return cfg


def read_settings_ini() -> configparser.RawConfigParser:
    config = configparser.RawConfigParser()
    config.read("%s/settings.ini" % djangoSettings.PROJECT_DIR)
    return config


# --- helpers --------------------------------------------------------------------


def _truthy(value) -> bool:
    """ANTARES serialises some booleans as the strings ``'true'``/``'false'``."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "t", "yes")
    if value is None:
        return False
    try:
        if isinstance(value, float) and math.isnan(value):
            return False
    except TypeError:
        pass
    return bool(value)


def _finite(value) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _survey_matches(value, survey_id: int) -> bool:
    try:
        return int(float(value)) == int(survey_id)
    except (TypeError, ValueError):
        return False


def mjd_to_datetime(mjd: float) -> datetime.datetime:
    dt = Time(float(mjd), format="mjd", scale="utc").to_datetime()
    return timezone.make_aware(dt, datetime.timezone.utc) if timezone.is_naive(dt) else dt


def datetime_to_mjd(dt: datetime.datetime) -> float:
    if timezone.is_aware(dt):
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return float(Time(dt, scale="utc").mjd)


def get_audit_user() -> Optional[User]:
    """The user rows are stamped with: ``admin`` like the other ingest crons, else a superuser."""
    user = User.objects.filter(username="admin").first()
    if user is None:
        user = User.objects.filter(is_superuser=True).order_by("pk").first()
    return user


def is_lsst_locus(locus, survey_id: int) -> bool:
    """A locus carries LSST data if its survey block or any light-curve row says so."""
    props = getattr(locus, "properties", None) or {}
    survey = props.get("survey") or {}
    if isinstance(survey, dict) and survey.get(LOCUS_SURVEY_KEY):
        return True
    lc = getattr(locus, "lightcurve", None)
    if lc is not None and len(lc) and LC_COL_SURVEY in lc.columns:
        return bool(any(_survey_matches(v, survey_id) for v in lc[LC_COL_SURVEY].tolist()))
    return False


def alert_flags_by_id(locus, cfg: LSSTIngestConfig) -> Dict[str, dict]:
    """``alert_id -> alert.properties`` for the locus (one extra API call per locus)."""
    if not cfg.use_alert_flags:
        return {}
    try:
        alerts = locus.alerts or []
    except Exception as e:  # network / API error: quality flags are optional
        print(f"[AntaresLSST] could not fetch alerts for {getattr(locus, 'locus_id', '?')}: {e}")
        return {}
    out = {}
    for alert in alerts:
        alert_id = getattr(alert, "alert_id", None)
        if alert_id:
            out[str(alert_id)] = getattr(alert, "properties", None) or {}
    return out


def point_is_bad(alert_props: Optional[dict], cfg: LSSTIngestConfig) -> bool:
    if not alert_props:
        return False
    if _truthy(alert_props.get(ALERT_PIXELFLAGS_KEY)):
        return True
    if _truthy(alert_props.get(ALERT_IS_NEGATIVE_KEY)):
        return True
    reliability = alert_props.get(ALERT_RELIABILITY_KEY)
    if cfg.min_reliability > 0 and _finite(reliability) and float(reliability) < cfg.min_reliability:
        return True
    return False


def parse_locus_lightcurve(locus, cfg: LSSTIngestConfig) -> List[dict]:
    """
    Turn one ANTARES locus into a list of LSST photometry points::

        {'mjd', 'obs_date', 'band', 'mag', 'mag_err', 'flux', 'flux_err',
         'flux_zero_point', 'forced', 'diffim', 'bad', 'alert_id'}

    Rows from other surveys, upper limits (NaN mag) and unknown passbands are
    skipped; the return value is sorted by MJD.
    """
    lc = getattr(locus, "lightcurve", None)
    if lc is None or not len(lc):
        return []
    required = {LC_COL_MJD, LC_COL_PASSBAND, LC_COL_MAG}
    missing = required - set(lc.columns)
    if missing:
        print(f"[AntaresLSST] locus {getattr(locus, 'locus_id', '?')} lightcurve lacks {sorted(missing)}")
        return []
    has_survey = LC_COL_SURVEY in lc.columns
    has_alert_id = LC_COL_ALERT_ID in lc.columns
    has_magerr = LC_COL_MAGERR in lc.columns
    flags = alert_flags_by_id(locus, cfg)

    points = []
    for row in lc.to_dict("records"):
        alert_id = str(row.get(LC_COL_ALERT_ID)) if has_alert_id and row.get(LC_COL_ALERT_ID) is not None else None
        if has_survey:
            if not _survey_matches(row.get(LC_COL_SURVEY), cfg.survey_id):
                continue
        elif not (alert_id and alert_id.startswith(ALERT_ID_PREFIX)):
            continue
        mag = row.get(LC_COL_MAG)
        mjd = row.get(LC_COL_MJD)
        if not (_finite(mag) and _finite(mjd)):
            continue
        band = str(row.get(LC_COL_PASSBAND) or "").strip().lower()
        if band not in LSST_BANDS:
            continue
        mag = float(mag)
        mag_err = row.get(LC_COL_MAGERR) if has_magerr else None
        mag_err = float(mag_err) if _finite(mag_err) else None
        flux = 10 ** (-0.4 * (mag - FLUX_ZERO_POINT))
        flux_err = 0.4 * np.log(10) * flux * mag_err if mag_err is not None else None
        points.append(
            {
                "mjd": float(mjd),
                "obs_date": mjd_to_datetime(mjd),
                "band": band,
                "mag": mag,
                "mag_err": mag_err,
                "flux": flux,
                "flux_err": flux_err,
                "flux_zero_point": FLUX_ZERO_POINT,
                # ANTARES LSST alerts are DIA (difference-image) detections, never forced photometry.
                "forced": False,
                "diffim": True,
                "bad": point_is_bad(flags.get(alert_id) if alert_id else None, cfg),
                "alert_id": alert_id,
            }
        )
    points.sort(key=lambda p: p["mjd"])
    return points


def ensure_lsst_instrument_stack(user):
    """get_or_create Observatory / Telescope / Instrument / bands / ObservationGroup for Rubin."""
    audit = {"created_by_id": user.id, "modified_by_id": user.id}
    obs_group, _ = ObservationGroup.objects.get_or_create(name=OBS_GROUP_NAME, defaults=audit)
    observatory, _ = Observatory.objects.get_or_create(
        name=OBSERVATORY_NAME, defaults={**OBSERVATORY_DEFAULTS, **audit}
    )
    telescope, _ = Telescope.objects.get_or_create(
        name=TELESCOPE_NAME, defaults={"observatory": observatory, **TELESCOPE_DEFAULTS, **audit}
    )
    instrument, _ = Instrument.objects.get_or_create(
        name=INSTRUMENT_NAME,
        defaults={"telescope": telescope, "description": INSTRUMENT_DESCRIPTION, **audit},
    )
    bands = {}
    for name in LSST_BANDS:
        bands[name], _ = PhotometricBand.objects.get_or_create(
            name=name,
            instrument=instrument,
            defaults={"disp_color": FILTER_COLORS.get(name, "#888888"), "disp_symbol": PLOT_SYMBOL, **audit},
        )
    return obs_group, instrument, bands


def select_transients(cfg: LSSTIngestConfig, now: Optional[datetime.datetime] = None):
    """
    Transients worth polling: in an active status and created / discovered /
    modified within ``max_days``, or with an open ``TransientFollowup`` window,
    south of ``max_dec`` (Rubin cannot see the far north). Most recently
    modified first, capped at ``max_transients``.
    """
    now = now or timezone.now()
    cutoff = now - datetime.timedelta(days=cfg.max_days)
    recent = Q(disc_date__gte=cutoff) | Q(modified_date__gte=cutoff) | Q(created_date__gte=cutoff)
    active = Transient.objects.filter(status__name__in=cfg.statuses).filter(recent)
    followup_ids = TransientFollowup.objects.filter(valid_stop__gte=now).values("transient_id")
    qs = (
        Transient.objects.filter(Q(pk__in=active.values("pk")) | Q(pk__in=followup_ids))
        .filter(dec__lte=cfg.max_dec)
        .order_by("-modified_date", "-pk")
    )
    return list(qs[: cfg.max_transients])


def existing_mjds_by_band(photometry) -> Dict[int, List[float]]:
    out: Dict[int, List[float]] = {}
    for band_id, obs_date in TransientPhotData.objects.filter(photometry=photometry).values_list(
        "band_id", "obs_date"
    ):
        out.setdefault(band_id, []).append(datetime_to_mjd(obs_date))
    return out


def store_points(transient, points: Iterable[dict], *, user, obs_group, instrument, bands, cfg, reference=None) -> int:
    """Write new points for one transient; returns how many rows were created."""
    points = list(points)
    if not points:
        return 0
    photometry = (
        TransientPhotometry.objects.filter(transient=transient, instrument=instrument, obs_group=obs_group)
        .order_by("pk")
        .first()
    )
    if photometry is None:
        photometry = TransientPhotometry.objects.create(
            transient=transient,
            instrument=instrument,
            obs_group=obs_group,
            reference=reference,
            created_by_id=user.id,
            modified_by_id=user.id,
        )
    elif reference and not photometry.reference:
        photometry.reference = reference
        photometry.save(update_fields=["reference"])
    # Broker alerts are public data, like the TNS-imported photometry.
    get_or_create_public_group()
    apply_collaboration_groups_to_photometry(photometry, [PUBLIC_COLLABORATION_GROUP_NAME])

    seen = existing_mjds_by_band(photometry)
    bad_dq = None
    created = 0
    from YSE_App.services import photstat
    with photstat.deferred_updates():  # one TransientPhotStat recompute per transient (#268)
        created = _store_point_rows(points, photometry, bands, cfg, seen, bad_dq, user)
    return created


def _store_point_rows(points, photometry, bands, cfg, seen, bad_dq, user):
    created = 0
    for p in points:
        band = bands[p["band"]]
        mjds = seen.setdefault(band.id, [])
        if any(abs(m - p["mjd"]) < cfg.mjd_match_min for m in mjds):
            continue
        row = TransientPhotData.objects.create(
            photometry=photometry,
            band=band,
            obs_date=p["obs_date"],
            mag=p["mag"],
            mag_err=p["mag_err"],
            flux=p["flux"],
            flux_err=p["flux_err"],
            flux_zero_point=p["flux_zero_point"],
            forced=p["forced"],
            diffim=p["diffim"],
            discovery_point=False,
            created_by_id=user.id,
            modified_by_id=user.id,
        )
        if p["bad"]:
            if bad_dq is None:
                bad_dq, _ = DataQuality.objects.get_or_create(
                    name=BAD_DATA_QUALITY_NAME,
                    defaults={"created_by_id": user.id, "modified_by_id": user.id},
                )
            row.data_quality.add(bad_dq)
        mjds.append(p["mjd"])
        created += 1
    return created


def lsst_loci_near(ra: float, dec: float, cfg: LSSTIngestConfig):
    """ANTARES cone search around a position, LSST loci only (via the provider)."""
    return _antares_provider.lsst_loci_near(ra, dec, cfg)


def getLSSTPhotometry_ANTARES(ra: float, dec: float, cfg: Optional[LSSTIngestConfig] = None) -> Optional[dict]:
    """
    ``/add_transient``-shaped photometry block for one position, the Rubin twin of
    ``QUB_data.getZTFPhotometry_ANTARES`` (for callers that upload via the API).
    """
    cfg = cfg or LSSTIngestConfig()
    for locus in lsst_loci_near(ra, dec, cfg):
        points = parse_locus_lightcurve(locus, cfg)
        if not points:
            continue
        photdata = {}
        for i, p in enumerate(points):
            photdata["%s_%i" % (p["obs_date"].strftime("%Y-%m-%dT%H:%M:%S"), i)] = {
                "obs_date": p["obs_date"].strftime("%Y-%m-%dT%H:%M:%S.%f"),
                "band": p["band"],
                "groups": [],
                "mag": p["mag"],
                "mag_err": p["mag_err"],
                "flux": p["flux"],
                "flux_err": p["flux_err"],
                "data_quality": BAD_DATA_QUALITY_NAME if p["bad"] else 0,
                "forced": 0,
                "flux_zero_point": p["flux_zero_point"],
                "discovery_point": 0,
                "diffim": 1,
            }
        return {"instrument": INSTRUMENT_NAME, "obs_group": OBS_GROUP_NAME, "photdata": photdata}
    return None


def sendemail(from_addr, to_addr, subject, message, login, password, smtpserver):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(message, "html"))
    with smtplib.SMTP(smtpserver) as server:
        try:
            server.starttls()
            server.login(login, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())
            print("Send success")
        except Exception:
            print("Send fail")


class AntaresLSST(CronJobBase):
    """Hourly: pull LSST alert photometry from ANTARES for active YSE transients."""

    RUN_EVERY_MINS = 60

    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = "YSE_App.data_ingest.Query_LSST.AntaresLSST"

    def __init__(self, config: Optional[configparser.RawConfigParser] = None):
        super().__init__()
        self._config = config
        self.cfg = LSSTIngestConfig()

    def do(self):
        print("running ANTARES LSST query at {}".format(datetime.datetime.now().isoformat()))
        tstart = time.time()
        nrows, ntransients = 0, 0
        config = self._config if self._config is not None else read_settings_ini()
        self.cfg = LSSTIngestConfig.from_config(config)
        if not antares_available():
            print("[AntaresLSST] antares_client is not installed in this environment; nothing to do")
            return
        try:
            nrows, ntransients = self.main()
        except Exception as e:
            exc_tb = sys.exc_info()[2]
            lineno = exc_tb.tb_lineno if exc_tb else "?"
            print("ANTARES LSST cron failed with error %s at line number %s" % (e, lineno))
            self._email_failure(config, "ANTARES LSST cron failed with error %s at line number %s" % (e, lineno))
        print(
            "ANTARES LSST -> YSE_PZ took %.1f seconds: %i new points for %i transients"
            % (time.time() - tstart, nrows, ntransients)
        )

    def main(self):
        cfg = self.cfg
        user = get_audit_user()
        if user is None:
            print("[AntaresLSST] no admin/superuser to stamp rows with; skipping")
            return 0, 0
        obs_group, instrument, bands = ensure_lsst_instrument_stack(user)
        transients = select_transients(cfg)
        print("[AntaresLSST] polling %i transients (radius %.1f arcsec)" % (len(transients), cfg.cone_radius_arcsec))
        nrows, ntransients = 0, 0
        for t in transients:
            try:
                n = self.ingest_transient(t, user=user, obs_group=obs_group, instrument=instrument, bands=bands)
            except Exception as e:  # one bad object must not stop the run
                print("[AntaresLSST] %s failed: %s: %s" % (t.name, type(e).__name__, e))
                continue
            if n:
                nrows += n
                ntransients += 1
        return nrows, ntransients

    def ingest_transient(self, transient, *, user, obs_group, instrument, bands) -> int:
        cfg = self.cfg
        created = 0
        for locus in lsst_loci_near(transient.ra, transient.dec, cfg):
            points = parse_locus_lightcurve(locus, cfg)
            if not points:
                continue
            created += store_points(
                transient,
                points,
                user=user,
                obs_group=obs_group,
                instrument=instrument,
                bands=bands,
                cfg=cfg,
                reference="ANTARES %s" % getattr(locus, "locus_id", ""),
            )
        if created:
            print("[AntaresLSST] %s: %i new LSST points" % (transient.name, created))
        return created

    @staticmethod
    def _email_failure(config, message):
        try:
            smtpserver = "%s:%s" % (config.get("SMTP_provider", "SMTP_HOST"), config.get("SMTP_provider", "SMTP_PORT"))
            login = config.get("SMTP_provider", "SMTP_LOGIN")
            sendemail(
                "%s@gmail.com" % login,
                config.get("main", "dbemail"),
                "YSE_PZ/ANTARES LSST Transient Upload Failure",
                "Alert : YSE_PZ failed to ingest LSST photometry in Query_LSST.py<br>" + message,
                login,
                config.get("main", "dbemailpassword"),
                smtpserver,
            )
        except Exception as e:
            print("[AntaresLSST] could not send failure email: %s" % e)
