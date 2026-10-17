"""Serializer related fields respect dataset visibility: best_spectrum output and write references.

Synthetic data only.
"""

from django.contrib.auth.models import AnonymousUser, Group
from django.test import RequestFactory, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from YSE_App.models import TransientPhotometry, TransientSpectrum
from YSE_App.serializers.transient_serializers import TransientSerializer
from YSE_App.serializers.log_serializers import LogSerializer
from YSE_App.serializers.spectra_serializers import TransientSpecDataSerializer
from YSE_App.serializers.phot_serializers import TransientPhotDataSerializer
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)

GROUP = "sv693-private"


def _ctx(user):
    request = RequestFactory().get("/api/")
    request.user = user
    return {"request": request}


class SerializerVisibilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("sv693_staff")
        cls.member = create_test_user("sv693_member", is_staff=False)
        cls.outsider = create_test_user("sv693_outsider", is_staff=False)
        cls.group, _ = Group.objects.get_or_create(name=GROUP)
        cls.member.groups.add(cls.group)
        cls.obs_group, cls.instrument, cls.band = create_instrument_stack(cls.staff, obs_group_name="sv693")
        cls.transient = create_minimal_transient(cls.staff, name="YSEsv693")
        audit = audit_fields(cls.staff)

        def spec(grouped):
            s = TransientSpectrum.objects.create(
                transient=cls.transient, instrument=cls.instrument, obs_group=cls.obs_group,
                ra=1.0, dec=1.0, obs_date=timezone.now(), **audit)
            if grouped:
                s.groups.add(cls.group)
            return s

        def phot(grouped):
            p = TransientPhotometry.objects.create(
                transient=cls.transient, instrument=cls.instrument, obs_group=cls.obs_group, **audit)
            if grouped:
                p.groups.add(cls.group)
            return p

        cls.hidden_spec, cls.public_spec = spec(True), spec(False)
        cls.hidden_phot, cls.public_phot = phot(True), phot(False)

    def _best(self, spectrum, context):
        self.transient.best_spectrum = spectrum
        self.transient.save()
        self.transient.refresh_from_db()
        return TransientSerializer(self.transient, context=context).data["best_spectrum"]

    def test_best_spectrum_hidden_from_outsider_and_anonymous(self):
        for user in (self.outsider, AnonymousUser()):
            self.assertIsNone(self._best(self.hidden_spec, _ctx(user)))
        # No request in context: behaves as anonymous (public only).
        self.assertFalse(self._field_ok(TransientSpecDataSerializer, "spectrum", self.hidden_spec.pk, None))

    def test_best_spectrum_visible_to_member_staff_and_when_public(self):
        for user in (self.member, self.staff):
            self.assertIsNotNone(self._best(self.hidden_spec, _ctx(user)))
        self.assertIsNotNone(self._best(self.public_spec, _ctx(self.outsider)))
        self.assertTrue(self._field_ok(TransientSpecDataSerializer, "spectrum", self.public_spec.pk, None))

    def _field_ok(self, serializer_cls, name, pk, user):
        context = _ctx(user) if user is not None else {}
        field = serializer_cls(context=context).fields[name]
        return field.get_queryset().filter(pk=pk).exists()

    def test_write_reference_spectrum(self):
        for cls, name in ((TransientSpecDataSerializer, "spectrum"), (LogSerializer, "transient_spectrum")):
            self.assertFalse(self._field_ok(cls, name, self.hidden_spec.pk, self.outsider), cls)
            self.assertTrue(self._field_ok(cls, name, self.public_spec.pk, self.outsider), cls)
            self.assertTrue(self._field_ok(cls, name, self.hidden_spec.pk, self.member), cls)
            self.assertTrue(self._field_ok(cls, name, self.hidden_spec.pk, self.staff), cls)

    def test_write_reference_photometry(self):
        for cls, name in ((TransientPhotDataSerializer, "photometry"), (LogSerializer, "transient_photometry")):
            self.assertFalse(self._field_ok(cls, name, self.hidden_phot.pk, self.outsider), cls)
            self.assertTrue(self._field_ok(cls, name, self.public_phot.pk, self.outsider), cls)
            self.assertTrue(self._field_ok(cls, name, self.hidden_phot.pk, self.staff), cls)

    def test_api_writers_group_service_account_may_reference_any(self):
        from django.conf import settings

        service = create_test_user("sv693_service", is_staff=False)
        service.groups.add(Group.objects.get(name=settings.API_WRITERS_GROUP))
        self.assertTrue(self._field_ok(TransientSpecDataSerializer, "spectrum", self.hidden_spec.pk, service))

    def test_api_best_spectrum_for_outsider(self):
        self.transient.best_spectrum = self.hidden_spec
        self.transient.save()
        client = APIClient()
        client.force_authenticate(user=self.outsider)
        resp = client.get(f"/api/transients/{self.transient.pk}/")
        if resp.status_code == 200:
            self.assertIsNone(resp.json()["best_spectrum"])
