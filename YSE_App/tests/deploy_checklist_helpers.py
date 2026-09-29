"""
Shared helpers for the deploy-checklist CI tests (see docs/deploy-checklist-ci.md).

Everything here is deliberately small: seed rows the pre-release checklist
flows need (telescope/PI/night type, follow-up statuses, a spectrum with
points) and utilities to make the requests look like the real UI/API clients.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import re
from typing import Iterable, List

from django.contrib.auth.models import Group
from django.utils import timezone

from YSE_App.models import (
    ClassicalNightType,
    FollowupStatus,
    Instrument,
    ObservationGroup,
    Observatory,
    PhotometricBand,
    PrincipalInvestigator,
    TaskStatus,
    Telescope,
    TransientSpecData,
    TransientSpectrum,
)
from YSE_App.tests.fixtures_minimal import audit_fields

CHECKLIST_PASSWORD = "deploy-checklist-pass"
TESS_OBS_PATCH_TARGET = "YSE_App.models.transient_models.tess_obs"

_SLUG_LINK_RE = re.compile(r"/transient_detail/([^/\"'#?]+)/")


def basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def transient_slugs_in_order(html: str) -> List[str]:
    """Distinct transient_detail slugs in first-appearance order (table row order)."""
    seen = []
    for slug in _SLUG_LINK_RE.findall(html):
        if slug not in seen:
            seen.append(slug)
    return seen


def order_of(slugs_in_page: Iterable[str], wanted: Iterable[str]) -> List[str]:
    """Filter the page order down to the slugs a test seeded (ignores other rows)."""
    wanted = set(wanted)
    return [s for s in slugs_in_page if s in wanted]


@contextlib.contextmanager
def iers_offline():
    """
    astroplan sunrise/sunset needs astropy IERS tables. Never let a test hit the
    network for them; accept degraded UT1 accuracy for dates past the bundled table.
    """
    try:
        from astropy.utils import iers
    except Exception:  # pragma: no cover - astropy always present in the web image
        yield
        return
    old_download = iers.conf.auto_download
    old_degraded = getattr(iers.conf, "iers_degraded_accuracy", None)
    iers.conf.auto_download = False
    if old_degraded is not None:
        iers.conf.iers_degraded_accuracy = "ignore"
    try:
        yield
    finally:
        iers.conf.auto_download = old_download
        if old_degraded is not None:
            iers.conf.iers_degraded_accuracy = old_degraded


def ensure_followup_statuses(user):
    audit = audit_fields(user)
    statuses = {}
    for name in ("Requested", "Successful", "Failed"):
        statuses[name], _ = FollowupStatus.objects.get_or_create(name=name, defaults=audit)
    return statuses


def ensure_task_statuses(user):
    audit = audit_fields(user)
    statuses = {}
    for name in ("Requested", "Successful", "Failed"):
        statuses[name], _ = TaskStatus.objects.get_or_create(name=name, defaults=audit)
    return statuses


def ensure_yse_group(user):
    group, _ = Group.objects.get_or_create(name="YSE")
    user.groups.add(group)
    return group


def create_telescope(user, name, *, latitude=20.7, longitude=-156.25, elevation=3052.0):
    """Observatory + telescope. Defaults are Haleakala so sunset/sunrise are sane."""
    audit = audit_fields(user)
    observatory, _ = Observatory.objects.get_or_create(
        name=f"Obs-{name}",
        defaults={"utc_offset": -10, "tz_name": "US/Hawaii", **audit},
    )
    telescope, _ = Telescope.objects.get_or_create(
        name=name,
        defaults={
            "observatory": observatory,
            "latitude": latitude,
            "longitude": longitude,
            "elevation": elevation,
            **audit,
        },
    )
    return telescope


def create_instrument(user, telescope, name, *, band_names=()):
    audit = audit_fields(user)
    instrument, _ = Instrument.objects.get_or_create(
        name=name, defaults={"telescope": telescope, **audit}
    )
    bands = {}
    for band_name in band_names:
        bands[band_name], _ = PhotometricBand.objects.get_or_create(
            name=band_name,
            instrument=instrument,
            defaults={"disp_color": "#00aa00", "disp_symbol": "circle", **audit},
        )
    return instrument, bands


def create_principal_investigator(user, name="checklist-pi"):
    audit = audit_fields(user)
    pi, _ = PrincipalInvestigator.objects.get_or_create(
        name=name,
        defaults={
            "email": "checklist-pi@example.com",
            "phone": "000",
            "institution": "CI",
            **audit,
        },
    )
    return pi


def ensure_classical_night_type(user, name="Full"):
    night_type, _ = ClassicalNightType.objects.get_or_create(
        name=name, defaults=audit_fields(user)
    )
    return night_type


def ensure_observation_group(user, name):
    group, _ = ObservationGroup.objects.get_or_create(name=name, defaults=audit_fields(user))
    return group


def add_spectrum_points(user, spectrum: TransientSpectrum, n_points=5):
    audit = audit_fields(user)
    TransientSpecData.objects.bulk_create(
        [
            TransientSpecData(
                spectrum=spectrum,
                wavelength=4000.0 + 10.0 * i,
                flux=1.0 + 0.1 * i,
                flux_err=0.05,
                **audit,
            )
            for i in range(n_points)
        ]
    )


def utc_days_from_now(days: float, *, hour: int = 12) -> datetime.datetime:
    base = timezone.now() + datetime.timedelta(days=days)
    return base.replace(hour=hour, minute=0, second=0, microsecond=0)


def fmt_dt(value: datetime.datetime) -> str:
    """Format the way the UI's datetime pickers submit values."""
    return value.strftime("%Y-%m-%d %H:%M:%S")
