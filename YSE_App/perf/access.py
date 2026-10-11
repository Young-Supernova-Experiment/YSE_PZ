"""
Non-staff fixtures for the access-filtered dashboard and API paths (#451, #452).

Every other perf fixture logs in as staff (``create_test_user`` defaults to
``is_staff=True``), and staff bypass ``filter_transients_by_user_access``, so the
per-transient visibility cost never showed up in a query count. These helpers
seed a collaboration member who is not staff and transients whose photometry
or spectra are public, private to the member's group, or private to another
group, so a test can check both which rows a non-staff user gets and that the
number of queries stays flat as the number of transients grows.

Synthetic names and groups only; nothing here is copied from a real database.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from django.contrib.auth.models import Group
from django.utils import timezone

from YSE_App.models import Transient, TransientPhotData, TransientPhotometry, TransientSpectrum
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_test_user,
    ensure_transient_statuses,
)

NON_STAFF_USERNAME = "perf_non_staff_member"
MEMBER_GROUP_NAME = "perf-access-member"
OTHER_GROUP_NAME = "perf-access-other"


def create_non_staff_user(username: str = NON_STAFF_USERNAME, group_names: Sequence[str] = (MEMBER_GROUP_NAME,)):
    """A logged-in collaborator without staff or superuser rights, in ``group_names``."""
    user = create_test_user(username, is_staff=False, is_superuser=False)
    for name in group_names:
        group, _ = Group.objects.get_or_create(name=name)
        user.groups.add(group)
    return user


# (name suffix, photometry groups or None for no photometry, spectrum groups or None)
# An empty tuple is public data; a tuple of group names is private to those groups.
ACCESS_MIX = (
    ("pubphot", (), None),
    ("memberphot", (MEMBER_GROUP_NAME,), None),
    ("otherphot", (OTHER_GROUP_NAME,), None),
    ("bothphot", (MEMBER_GROUP_NAME, OTHER_GROUP_NAME), None),
    ("pubspec", None, ()),
    ("memberspec", None, (MEMBER_GROUP_NAME,)),
    ("otherspec", None, (OTHER_GROUP_NAME,)),
    ("otherphot-pubspec", (OTHER_GROUP_NAME,), ()),
    ("otherphot-memberspec", (OTHER_GROUP_NAME,), (MEMBER_GROUP_NAME,)),
    ("otherphot-otherspec", (OTHER_GROUP_NAME,), (OTHER_GROUP_NAME,)),
    ("nodata", None, None),
)


@dataclass
class AccessDataset:
    transients: List[Transient]
    by_suffix: Dict[str, Transient] = field(default_factory=dict)


def _groups(names: Sequence[str]) -> List[Group]:
    return [Group.objects.get_or_create(name=name)[0] for name in names]


def seed_access_transients(
    owner,
    *,
    prefix: str = "perfacc",
    status_name: str = "New",
    mix=ACCESS_MIX,
    copies: int = 1,
    n_points: int = 3,
) -> AccessDataset:
    """
    ``copies`` x ``mix`` transients named ``{prefix}{i:03d}-{suffix}``, each with
    the photometry and spectrum visibility its ``mix`` entry gives (one
    photometry set of ``n_points`` points, one spectrum). Bulk-created points;
    a few queries per transient for the M2M group links.
    """
    audit = audit_fields(owner)
    statuses = ensure_transient_statuses(owner)
    obs_group, instrument, band = create_instrument_stack(owner, obs_group_name=f"{prefix}-group")
    now = timezone.now()
    dataset = AccessDataset(transients=[])
    points = []
    for copy in range(copies):
        for j, (suffix, phot_groups, spec_groups) in enumerate(mix):
            index = copy * len(mix) + j
            transient = Transient.objects.create(
                name=f"{prefix}{index:03d}-{suffix}",
                ra=10.0 + 0.01 * index,
                dec=20.0,
                status=statuses[status_name],
                obs_group=obs_group,
                disc_date=now - datetime.timedelta(days=10 + index),
                **audit,
            )
            dataset.transients.append(transient)
            if copy == 0:
                dataset.by_suffix[suffix] = transient
            if phot_groups is not None:
                phot = TransientPhotometry.objects.create(
                    transient=transient, instrument=instrument, obs_group=obs_group, **audit
                )
                if phot_groups:
                    phot.groups.add(*_groups(phot_groups))
                for k in range(n_points):
                    points.append(TransientPhotData(
                        photometry=phot,
                        band=band,
                        obs_date=now - datetime.timedelta(days=n_points - k),
                        mag=18.0 + 0.1 * index + 0.01 * k,
                        mag_err=0.05,
                        discovery_point=(k == 0),
                        **audit,
                    ))
            if spec_groups is not None:
                spec = TransientSpectrum.objects.create(
                    transient=transient,
                    instrument=instrument,
                    obs_group=obs_group,
                    ra=transient.ra,
                    dec=transient.dec,
                    obs_date=now,
                    redshift=0.05,
                    **audit,
                )
                if spec_groups:
                    spec.groups.add(*_groups(spec_groups))
    TransientPhotData.objects.bulk_create(points)
    return dataset


def visible_suffixes_for(group_names: Optional[Sequence[str]]) -> set:
    """Which ``ACCESS_MIX`` suffixes a non-staff user in ``group_names`` may see (None = anonymous)."""
    if group_names is None:
        return set()
    mine = set(group_names)
    visible = set()
    for suffix, phot_groups, spec_groups in ACCESS_MIX:
        for groups in (phot_groups, spec_groups):
            if groups is not None and (not groups or mine.intersection(groups)):
                visible.add(suffix)
    return visible
