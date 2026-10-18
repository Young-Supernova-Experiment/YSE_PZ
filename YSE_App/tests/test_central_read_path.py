"""Central dataset read rule (#648, PR 0): one place decides photometry and spectrum visibility.

Equivalence guard for the refactor: for staff, a group member, a Public
member, an outsider and an anonymous user, every converted read path returns
exactly the rows the old inline group rule returned (and, where the path
builds a queryset, the same SQL). Then the hook shape: an allow widens, a
deny narrows, and a deny never hides public data.

Synthetic data only.
"""

import datetime
from unittest import mock

from django.contrib.auth.models import AnonymousUser, Group
from django.db.models import Exists, OuterRef, Q
from django.test import TestCase
from django.utils import timezone

from YSE_App.common.collaboration_groups import PUBLIC_COLLABORATION_GROUP_NAME
from YSE_App.data import PhotometryService, SpectraService
from YSE_App.filters.transient_search import _rows_visible_to
from YSE_App.models import (
    DataAccessRequest,
    Transient,
    TransientPhotData,
    TransientPhotometry,
    TransientSpectrum,
)
from YSE_App.services import data_access, visibility
from YSE_App.services.visibility import (
    AccessRules,
    filter_transients_by_user_access,
    hidden_photometry_exists,
    object_visible_to_user,
    photometry_visible_q,
    spectrum_visible_q,
    transient_access_q,
    transients_with_hidden_photometry,
    user_can_view_transient,
    visible_photometry_ids_q,
)
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

GROUP = "crp648-private"
OTHER = "crp648-other"


# ---------------------------------------------------- the old inline rules


def _old_names(user):
    return list(user.groups.values_list("name", flat=True)) if user.is_authenticated else []


def _old_q(user):
    names = _old_names(user)
    return Q(groups__isnull=True), Q(groups__name__in=names)


def _old_phot(user, transient_id=None):
    a, b = _old_q(user)
    if transient_id is None:
        return TransientPhotometry.objects.filter(a | b).distinct()
    return TransientPhotometry.objects.filter(a | b, transient_id=transient_id).distinct()


def _old_spec(user, transient_id=None):
    a, b = _old_q(user)
    if transient_id is None:
        return TransientSpectrum.objects.filter(Q(a | b)).distinct()
    return TransientSpectrum.objects.filter((a | b) & Q(transient_id=transient_id)).distinct()


def _old_visible_ids_q(user):
    if visibility.user_sees_everything(user):
        return None
    a, b = _old_q(user)
    return Q(photometry_id__in=TransientPhotometry.objects.filter(a | b).values("pk"))


def _old_hidden_exists(user):
    through = TransientPhotometry.groups.through
    restricted = TransientPhotometry.objects.filter(transient_id=OuterRef("pk")).filter(
        Exists(through.objects.filter(transientphotometry_id=OuterRef("pk"))))
    gids = list(user.groups.values_list("id", flat=True)) if user.is_authenticated else []
    if gids:
        restricted = restricted.filter(~Exists(
            through.objects.filter(transientphotometry_id=OuterRef("pk"), group_id__in=gids)))
    return Exists(restricted)


def _old_transient_access_q(user):
    a, b = _old_q(user)
    phot = TransientPhotometry.objects.filter(a | b, transient_id=OuterRef("pk"))
    spec = TransientSpectrum.objects.filter(a | b, transient_id=OuterRef("pk"))
    return Q(Exists(phot)) | Q(Exists(spec))


def _old_object_visible(user, obj):
    # The group rule; staff no longer bypass it for photometry and spectra (#706).
    if not user.is_authenticated:
        return False
    obj_names = set(obj.groups.values_list("name", flat=True))
    return not obj_names or bool(set(_old_names(user)) & obj_names)


def _old_hidden_datasets(user, transient_id, model):
    if not user.is_authenticated:
        return set()
    names = set(_old_names(user))
    return {row.pk for row in model.objects.filter(transient_id=transient_id)
            if row.groups.exists() and not {g.name for g in row.groups.all()} & names}


def _pks(qs):
    return set(qs.values_list("pk", flat=True))


class _Fixture(TestCase):
    """Datasets with no group, one group, the other group, both, and Public only, on several transients."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("crp648_staff")
        cls.member = create_test_user("crp648_member", is_staff=False)
        cls.public_member = create_test_user("crp648_public", is_staff=False)
        cls.outsider = create_test_user("crp648_outsider", is_staff=False)
        cls.group, _ = Group.objects.get_or_create(name=GROUP)
        cls.other, _ = Group.objects.get_or_create(name=OTHER)
        cls.public, _ = Group.objects.get_or_create(name=PUBLIC_COLLABORATION_GROUP_NAME)
        cls.member.groups.add(cls.group)
        cls.public_member.groups.add(cls.public)
        cls.obs_group, cls.instrument, cls.band = create_instrument_stack(cls.staff, obs_group_name="crp648")

        layouts = {
            "public": [()],
            "private": [(cls.group,)],
            "other": [(cls.other,)],
            "both": [(cls.group, cls.other)],
            "publicgrp": [(cls.public,)],
            "mixed": [(), (cls.other,)],
        }
        cls.transients = {}
        cls.phot = {}
        cls.spec = {}
        for i, (key, sets) in enumerate(layouts.items()):
            transient = create_minimal_transient(cls.staff, name=f"YSEcrp648{key}", ra=20.0 + i, dec=5.0)
            cls.transients[key] = transient
            for j, groups in enumerate(sets):
                phot = TransientPhotometry.objects.create(
                    transient=transient, instrument=cls.instrument, obs_group=cls.obs_group,
                    **audit_fields(cls.staff))
                spec = TransientSpectrum.objects.create(
                    transient=transient, instrument=cls.instrument, obs_group=cls.obs_group,
                    ra=transient.ra, dec=transient.dec, obs_date=timezone.now(), **audit_fields(cls.staff))
                if groups:
                    phot.groups.add(*groups)
                    spec.groups.add(*groups)
                TransientPhotData.objects.create(
                    photometry=phot, band=cls.band, mag=18.0 + j, mag_err=0.05,
                    obs_date=timezone.now() - datetime.timedelta(days=1 + j), **audit_fields(cls.staff))
                cls.phot[(key, j)] = phot
                cls.spec[(key, j)] = spec

    def users(self):
        return {
            "staff": self.staff,
            "member": self.member,
            "public_member": self.public_member,
            "outsider": self.outsider,
            "anonymous": AnonymousUser(),
        }


class EquivalenceTests(_Fixture):
    """With no rules, every converted path matches the old inline group rule."""

    def test_photometry_and_spectra_services(self):
        for label, user in self.users().items():
            with self.subTest(user=label):
                new = PhotometryService.GetAuthorizedTransientPhotometry_ByUser(user)
                self.assertEqual(_pks(new), _pks(_old_phot(user)))
                self.assertEqual(str(new.query), str(_old_phot(user).query))
                new = SpectraService.GetAuthorizedTransientSpectrum_ByUser(user)
                self.assertEqual(_pks(new), _pks(_old_spec(user)))
                self.assertEqual(str(new.query), str(_old_spec(user).query))
                self.assertEqual(
                    _pks(PhotometryService.GetAuthorizedTransientPhotData_ByUser(user)),
                    _pks(TransientPhotData.objects.filter(photometry__id__in=_old_phot(user).values("id"))))
                for key, transient in self.transients.items():
                    new = PhotometryService.GetAuthorizedTransientPhotometry_ByUser_ByTransient(user, transient.pk)
                    old = _old_phot(user, transient.pk)
                    self.assertEqual(_pks(new), _pks(old), key)
                    self.assertEqual(str(new.query), str(old.query), key)
                    new = SpectraService.GetAuthorizedTransientSpectrum_ByUser_ByTransient(user, transient.pk)
                    old = _old_spec(user, transient.pk)
                    self.assertEqual(_pks(new), _pks(old), key)
                    self.assertEqual(str(new.query), str(old.query), key)
                    self.assertEqual(
                        _pks(PhotometryService.GetAuthorizedTransientPhotData_ByUser_ByTransient(user, transient.pk)),
                        _pks(TransientPhotData.objects.filter(
                            photometry__id__in=_old_phot(user, transient.pk).values("id"))),
                        key)

    def test_point_condition_and_hidden_photometry(self):
        for label, user in self.users().items():
            with self.subTest(user=label):
                new, old = visible_photometry_ids_q(user), _old_visible_ids_q(user)
                self.assertEqual(new is None, old is None)
                if new is not None:
                    self.assertEqual(_pks(TransientPhotData.objects.filter(new)),
                                     _pks(TransientPhotData.objects.filter(old)))
                    self.assertEqual(str(TransientPhotData.objects.filter(new).query),
                                     str(TransientPhotData.objects.filter(old).query))
                if visibility.user_sees_everything(user):
                    continue
                new_qs = Transient.objects.filter(hidden_photometry_exists(user))
                old_qs = Transient.objects.filter(_old_hidden_exists(user))
                self.assertEqual(_pks(new_qs), _pks(old_qs))
                self.assertEqual(str(new_qs.query), str(old_qs.query))
                ids = [t.pk for t in self.transients.values()]
                self.assertEqual(transients_with_hidden_photometry(user, ids),
                                 _pks(old_qs.filter(pk__in=ids)))

    def test_transient_access(self):
        for label, user in self.users().items():
            if not user.is_authenticated or visibility.user_sees_everything(user):
                continue
            with self.subTest(user=label):
                new_qs = Transient.objects.filter(transient_access_q(user))
                old_qs = Transient.objects.filter(_old_transient_access_q(user))
                self.assertEqual(_pks(new_qs), _pks(old_qs))
                self.assertEqual(str(new_qs.query), str(old_qs.query))
        for label, user in self.users().items():
            with self.subTest(user=label):
                for key, transient in self.transients.items():
                    expected = (
                        user.is_authenticated and (
                            visibility.user_sees_everything(user)
                            or _old_phot(user, transient.pk).exists()
                            or _old_spec(user, transient.pk).exists()))
                    self.assertEqual(user_can_view_transient(user, transient.pk), bool(expected), key)
                    listed = filter_transients_by_user_access(user, Transient.objects.filter(pk=transient.pk))
                    self.assertEqual(listed.exists(), bool(expected), key)

    def test_per_object_checks_and_hidden_datasets(self):
        kinds = ((DataAccessRequest.KIND_PHOTOMETRY, TransientPhotometry, self.phot),
                 (DataAccessRequest.KIND_SPECTRUM, TransientSpectrum, self.spec))
        for label, user in self.users().items():
            with self.subTest(user=label):
                for kind, model, rows in kinds:
                    for key, obj in rows.items():
                        self.assertEqual(object_visible_to_user(user, obj), _old_object_visible(user, obj), key)
                    for key, transient in self.transients.items():
                        self.assertEqual(
                            {row.pk for row in data_access.hidden_datasets(user, transient.pk, kind)},
                            _old_hidden_datasets(user, transient.pk, model), (kind, key))

    def test_search_has_spectrum_filter(self):
        for label, user in self.users().items():
            with self.subTest(user=label):
                qs = TransientSpectrum.objects.all()
                new = _rows_visible_to(qs, user)
                if user.is_authenticated and visibility.user_sees_everything(user):
                    self.assertEqual(_pks(new), _pks(qs))
                    continue
                a, b = _old_q(user)
                old = qs.filter(a | b)
                self.assertEqual(_pks(new), _pks(old))
                self.assertEqual(str(new.query), str(old.query))


class HookShapeTests(_Fixture):
    """The #648 hooks: allow widens, deny narrows, public data is never denied."""

    def _patch(self, phot=None, spec=None):
        patches = []
        if phot is not None:
            patches.append(mock.patch.object(visibility, "_photometry_access_rules", return_value=phot))
        if spec is not None:
            patches.append(mock.patch.object(visibility, "_spectrum_access_rules", return_value=spec))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_no_rules_today(self):
        for user in self.users().values():
            self.assertTrue(visibility._photometry_access_rules(user).empty)
            self.assertTrue(visibility._spectrum_access_rules(user).empty)
            self.assertIsNone(visibility._extra_photometry_grants_q(user))

    def test_allow_widens_for_outsider(self):
        phot = self.phot[("private", 0)]
        spec = self.spec[("private", 0)]
        self._patch(AccessRules(allow=Q(pk__in=[phot.pk])), AccessRules(allow=Q(pk__in=[spec.pk])))
        self.assertIn(phot.pk, _pks(TransientPhotometry.objects.filter(photometry_visible_q(self.outsider))))
        self.assertIn(spec.pk, _pks(TransientSpectrum.objects.filter(spectrum_visible_q(self.outsider))))
        self.assertTrue(object_visible_to_user(self.outsider, phot))
        self.assertTrue(object_visible_to_user(self.outsider, spec))
        transient = self.transients["private"]
        self.assertTrue(user_can_view_transient(self.outsider, transient.pk))
        self.assertNotIn(transient.pk, transients_with_hidden_photometry(self.outsider, [transient.pk]))
        kind = DataAccessRequest.KIND_PHOTOMETRY
        self.assertEqual(data_access.hidden_datasets(self.outsider, transient.pk, kind), [])

    def test_deny_narrows_for_member(self):
        phot = self.phot[("private", 0)]
        spec = self.spec[("private", 0)]
        self._patch(AccessRules(deny=Q(pk__in=[phot.pk])), AccessRules(deny=Q(pk__in=[spec.pk])))
        self.assertNotIn(phot.pk, _pks(PhotometryService.GetAuthorizedTransientPhotometry_ByUser(self.member)))
        self.assertNotIn(spec.pk, _pks(SpectraService.GetAuthorizedTransientSpectrum_ByUser(self.member)))
        self.assertFalse(object_visible_to_user(self.member, phot))
        transient = self.transients["private"]
        self.assertIn(transient.pk, transients_with_hidden_photometry(self.member, [transient.pk]))
        self.assertFalse(user_can_view_transient(self.member, transient.pk))
        points = TransientPhotData.objects.filter(visible_photometry_ids_q(self.member))
        self.assertFalse(points.filter(photometry=phot).exists())
        # The other private-group sets stay visible.
        self.assertIn(self.phot[("both", 0)].pk,
                      _pks(PhotometryService.GetAuthorizedTransientPhotometry_ByUser(self.member)))

    def test_deny_never_hides_public_data(self):
        public_sets = [self.phot[("public", 0)], self.phot[("mixed", 0)]]
        public_spec = [self.spec[("public", 0)], self.spec[("mixed", 0)]]
        publicgrp_phot = self.phot[("publicgrp", 0)]
        publicgrp_spec = self.spec[("publicgrp", 0)]
        deny_phot = AccessRules(deny=Q(pk__in=[p.pk for p in public_sets] + [publicgrp_phot.pk]))
        deny_spec = AccessRules(deny=Q(pk__in=[s.pk for s in public_spec] + [publicgrp_spec.pk]))
        self._patch(deny_phot, deny_spec)
        for label, user in self.users().items():
            if user.is_authenticated and visibility.user_sees_everything(user):
                continue
            with self.subTest(user=label):
                visible = _pks(TransientPhotometry.objects.filter(photometry_visible_q(user)))
                self.assertTrue({p.pk for p in public_sets} <= visible)
                visible = _pks(TransientSpectrum.objects.filter(spectrum_visible_q(user)))
                self.assertTrue({s.pk for s in public_spec} <= visible)
                self.assertNotIn(self.transients["public"].pk,
                                 transients_with_hidden_photometry(user, [self.transients["public"].pk]))
        # Public-only data stays visible to a Public member (every account joins Public on creation).
        self.assertIn(publicgrp_phot.pk,
                      _pks(TransientPhotometry.objects.filter(photometry_visible_q(self.public_member))))
        self.assertIn(publicgrp_spec.pk,
                      _pks(TransientSpectrum.objects.filter(spectrum_visible_q(self.public_member))))
        self.assertTrue(object_visible_to_user(self.public_member, publicgrp_phot))
        # The public guard only limits a deny: it never shows more than the group rule.
        for label, user in self.users().items():
            with self.subTest(user=label, check="no widening"):
                self.assertTrue(_pks(TransientPhotometry.objects.filter(photometry_visible_q(user)))
                                <= _pks(_old_phot(user)))
                self.assertTrue(_pks(TransientSpectrum.objects.filter(spectrum_visible_q(user)))
                                <= _pks(_old_spec(user)))
        # Without Public membership, Public-only data is not protected (it was never visible).
        self.public_member.groups.remove(self.public)
        self.assertNotIn(publicgrp_phot.pk,
                         _pks(TransientPhotometry.objects.filter(photometry_visible_q(self.public_member))))
