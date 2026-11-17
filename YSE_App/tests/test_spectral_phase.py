"""Computed spectral phase (#848): phase math, the validity rule, hooks, backfill, the search filter,
Selections, Download -> Spectra / Photometry, access filtering, the preview gate, ON DELETE.

Synthetic data only: made-up users, groups, transients, spectra and fit results.
"""

from __future__ import annotations

import csv
import datetime
import importlib
import io
import types
import zipfile
from unittest import mock

from django.apps import apps
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import IntegrityError, connection, models
from django.http import QueryDict
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from YSE_App import views as views_module
from YSE_App.common import spectrum_arrays
from YSE_App.filters.transient_search import TransientSearchFilterSet
from YSE_App.models import (
    ExternalServiceRun,
    SpectrumPhase,
    Transient,
    TransientPhotData,
    TransientPhotometry,
    TransientSpectrum,
)
from YSE_App.services import analysis_services as svc
from YSE_App.services import selections as selection_svc
from YSE_App.services import spectral_phase as sp
from YSE_App.services.download_options import PhotometryOptions, SpectraOptions, median_snr
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)
from YSE_App.tests.test_fit_tables import bazin_result, salt_result

phase_migration = importlib.import_module("YSE_App.migrations.0070_spectrum_phase_fk_on_delete")
PREVIEW_COOKIE = "yse-ui-preview"
MJD0 = datetime.datetime(1858, 11, 17, tzinfo=datetime.timezone.utc)


def mjd_date(mjd):
    return MJD0 + datetime.timedelta(days=mjd)


# --------------------------------------------------------------------------- pure math


class PhaseMathTests(SimpleTestCase):
    def rows(self, spectrum_mjd, **kwargs):
        return {r["reference"]: r for r in sp.compute_phases(spectrum_mjd, sp.Epochs(**kwargs))}

    def test_bazin_rest_frame_is_the_default_with_a_redshift(self):
        rows = self.rows(60020.0, bazin_peak=60010.0, bazin_valid=True, redshift=0.25)
        self.assertAlmostEqual(rows["bazin_rest"]["phase"], 8.0)
        self.assertEqual(rows["bazin_rest"]["frame"], "rest")
        self.assertEqual(rows["bazin_rest"]["redshift"], 0.25)
        self.assertTrue(rows["bazin_rest"]["is_default"])
        self.assertAlmostEqual(rows["bazin_obs"]["phase"], 10.0)
        self.assertEqual(rows["bazin_obs"]["frame"], "observer")
        self.assertIsNone(rows["bazin_obs"]["redshift"])
        self.assertFalse(rows["bazin_obs"]["is_default"])

    def test_observer_frame_bazin_is_the_default_without_a_redshift(self):
        rows = self.rows(60005.0, bazin_peak=60010.0, bazin_valid=True)
        self.assertNotIn("bazin_rest", rows)
        self.assertAlmostEqual(rows["bazin_obs"]["phase"], -5.0)
        self.assertTrue(rows["bazin_obs"]["is_default"])

    def test_salt_t0_uses_the_fit_redshift_then_the_transient_one(self):
        rows = self.rows(60021.0, salt_t0=60010.0, salt_z=0.1, redshift=0.5)
        self.assertAlmostEqual(rows["salt_t0"]["phase"], 10.0)
        self.assertEqual(rows["salt_t0"]["frame"], "rest")
        rows = self.rows(60022.0, salt_t0=60010.0, redshift=0.2)
        self.assertAlmostEqual(rows["salt_t0"]["phase"], 10.0)
        rows = self.rows(60022.0, salt_t0=60010.0)
        self.assertAlmostEqual(rows["salt_t0"]["phase"], 12.0)
        self.assertEqual(rows["salt_t0"]["frame"], "observer")
        self.assertFalse(rows["salt_t0"]["is_default"])

    def test_discovery_is_observer_frame_days(self):
        rows = self.rows(60020.5, discovery=60000.0, redshift=0.3)
        self.assertAlmostEqual(rows["discovery"]["phase"], 20.5)
        self.assertEqual(rows["discovery"]["frame"], "observer")
        self.assertEqual(set(rows), {"discovery"})

    def test_validity_rule(self):
        self.assertTrue(sp.bazin_peak_valid(60010.0, True))
        self.assertTrue(sp.bazin_peak_valid(60010.0, None))  # no non-detection known
        self.assertFalse(sp.bazin_peak_valid(60010.0, False))
        self.assertFalse(sp.bazin_peak_valid(None, True))
        rows = self.rows(60020.0, bazin_peak=60010.0, bazin_valid=False, redshift=0.1, discovery=60000.0)
        self.assertEqual(set(rows), {"discovery"})
        self.assertFalse(any(r["is_default"] for r in rows.values()))

    def test_unusable_redshift_means_observer_frame(self):
        self.assertIsNone(sp.usable_redshift(-1.0))
        self.assertIsNone(sp.usable_redshift(float("nan")))
        rows = self.rows(60020.0, bazin_peak=60010.0, bazin_valid=True, redshift=float("nan"))
        self.assertTrue(rows["bazin_obs"]["is_default"])

    def test_median_snr(self):
        self.assertAlmostEqual(median_snr(([1, 2, 3], [10.0, 20.0, 30.0], [1.0, 2.0, 0.0])), 10.0)
        self.assertIsNone(median_snr(([1], [1.0], None)))

    def test_photometry_options(self):
        opts = PhotometryOptions.from_params(QueryDict("mjd_min=60000&mjd_max=60010&band=g,R&exclude_flagged=1"))
        self.assertTrue(opts.keeps(60005.0, "r", ""))
        self.assertFalse(opts.keeps(60005.0, "i", ""))
        self.assertFalse(opts.keeps(60011.0, "g", ""))
        self.assertFalse(opts.keeps(60005.0, "g", "bad-flag"))
        opts = PhotometryOptions.from_params(QueryDict("phase_min=-5&phase_max=5"))
        self.assertTrue(opts.keeps(60005.0, "g", "", 0.0))
        self.assertFalse(opts.keeps(60005.0, "g", "", 6.0))
        self.assertFalse(opts.keeps(60005.0, "g", "", None))
        self.assertFalse(PhotometryOptions.from_params(QueryDict("")).active)

    def test_bad_options_are_refused(self):
        from YSE_App.services.download_options import OptionError

        for query in ("phase_min=x", "phase_min=5&phase_max=1", "phase_ref=nope", "snr_min=inf"):
            with self.subTest(query=query), self.assertRaises(OptionError):
                SpectraOptions.from_params(QueryDict(query))


# --------------------------------------------------------------------------- fixtures


class PhaseFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = create_test_user("sp_staff", is_superuser=True)
        cls.member = create_test_user("sp_member", is_staff=False)
        cls.outsider = create_test_user("sp_outsider", is_staff=False)
        cls.group = Group.objects.create(name="sp-private")
        cls.member.groups.add(cls.group)
        cls.obs_group, cls.instrument, cls.band = create_instrument_stack(cls.staff, obs_group_name="sp-a")
        _g, cls.other_instrument, _b = create_instrument_stack(cls.staff, obs_group_name="sp-b")
        cls.bazin = svc.register_service("bazin_fit", runner="bazin_fit", user=cls.staff, timeout_seconds=0)
        cls.salt = svc.register_service("sncosmo_fit", runner="sncosmo_fit", user=cls.staff, timeout_seconds=0)

    def make_transient(self, name, *, redshift=0.25, disc_mjd=60000.0):
        transient = create_minimal_transient(self.staff, name=name, obs_group_name="sp-a")
        Transient.objects.filter(pk=transient.pk).update(redshift=redshift, disc_date=mjd_date(disc_mjd))
        transient.refresh_from_db()
        return transient

    def make_spectrum(self, transient, mjd, *, instrument=None, groups=(), pixels=True, err=0.1):
        spectrum = TransientSpectrum.objects.create(
            transient=transient, instrument=instrument or self.instrument, obs_group=self.obs_group,
            ra=transient.ra, dec=transient.dec, obs_date=mjd_date(mjd), **audit_fields(self.staff))
        for group in groups:
            spectrum.groups.add(group)
        if pixels:
            spectrum_arrays.save(spectrum, [4000.0, 5000.0, 6000.0], [1.0, 2.0, 3.0],
                                 [err, err, err] if err else None)
        return spectrum

    def fit(self, profile, transient, result):
        run = ExternalServiceRun.objects.create(
            service=profile.service, transient=transient, created_by=self.staff, modified_by=self.staff,
            request_payload={"analysis": True, "params": {}})
        run.mark_succeeded(result=result)
        return run

    def phases(self, spectrum):
        return {row.reference: row for row in SpectrumPhase.objects.filter(spectrum=spectrum)}


# --------------------------------------------------------------------------- writing


class RecomputeTests(PhaseFixture):
    def test_new_spectrum_gets_discovery_phase_and_fit_writes_the_rest(self):
        transient = self.make_transient("2099spa")
        spectrum = self.make_spectrum(transient, 60021.0)
        self.assertEqual(set(self.phases(spectrum)), {"discovery"})
        self.assertAlmostEqual(self.phases(spectrum)["discovery"].phase, 21.0)

        self.fit(self.bazin, transient, bazin_result(peak_r=60011.0, last_nd=60000.0))
        rows = self.phases(spectrum)
        self.assertAlmostEqual(rows["bazin_rest"].phase, 8.0)
        self.assertTrue(rows["bazin_rest"].is_default)
        self.assertAlmostEqual(rows["bazin_obs"].phase, 10.0)
        self.assertEqual(rows["bazin_obs"].reference_mjd, 60011.0)

        self.fit(self.salt, transient, salt_result(t0=60012.0))  # z = 0.03 in the fit
        self.assertAlmostEqual(self.phases(spectrum)["salt_t0"].phase, 9.0 / 1.03)

    def test_spectrum_added_after_the_fit(self):
        transient = self.make_transient("2099spb", redshift=None)
        self.fit(self.bazin, transient, bazin_result(peak_r=60011.0, last_nd=60000.0))
        spectrum = self.make_spectrum(transient, 60001.0)
        rows = self.phases(spectrum)
        self.assertNotIn("bazin_rest", rows)
        self.assertTrue(rows["bazin_obs"].is_default)
        self.assertAlmostEqual(rows["bazin_obs"].phase, -10.0)

    def test_peak_before_last_non_detection_gives_no_bazin_phase(self):
        transient = self.make_transient("2099spc")
        spectrum = self.make_spectrum(transient, 60021.0)
        self.fit(self.bazin, transient, bazin_result(peak_r=60011.0, last_nd=60015.0))
        self.assertEqual(set(self.phases(spectrum)), {"discovery"})

    def test_newest_fit_wins_and_rows_are_replaced(self):
        transient = self.make_transient("2099spd")
        spectrum = self.make_spectrum(transient, 60021.0)
        self.fit(self.bazin, transient, bazin_result(peak_r=60011.0, last_nd=60000.0))
        self.fit(self.bazin, transient, bazin_result(peak_r=60016.0, last_nd=60000.0))
        self.assertAlmostEqual(self.phases(spectrum)["bazin_obs"].phase, 5.0)
        self.assertEqual(SpectrumPhase.objects.filter(spectrum=spectrum, reference="bazin_obs").count(), 1)

    def test_hook_errors_never_break_the_save(self):
        transient = self.make_transient("2099spe")
        with mock.patch.object(sp, "recompute_transient", side_effect=RuntimeError("synthetic")):
            spectrum = self.make_spectrum(transient, 60021.0)
            run = self.fit(self.bazin, transient, bazin_result())
        self.assertTrue(TransientSpectrum.objects.filter(pk=spectrum.pk).exists())
        self.assertEqual(run.status, ExternalServiceRun.STATUS_SUCCEEDED)

    def test_backfill_dry_run_then_apply(self):
        transient = self.make_transient("2099spf")
        spectrum = self.make_spectrum(transient, 60021.0)
        self.fit(self.bazin, transient, bazin_result(peak_r=60011.0, last_nd=60000.0))
        SpectrumPhase.objects.all().delete()
        out = io.StringIO()
        call_command("backfill_spectral_phases", stdout=out)
        self.assertIn("dry run", out.getvalue())
        self.assertFalse(SpectrumPhase.objects.exists())
        out = io.StringIO()
        call_command("backfill_spectral_phases", "--apply", stdout=out)
        self.assertEqual(set(self.phases(spectrum)), {"bazin_rest", "bazin_obs", "discovery"})
        call_command("backfill_spectral_phases", "--apply", stdout=io.StringIO())
        self.assertEqual(SpectrumPhase.objects.filter(spectrum=spectrum).count(), 3)


# --------------------------------------------------------------------------- search filter and Selections


class FilterTests(PhaseFixture):
    def setUp(self):
        self.young = self.make_transient("2099spg")
        self.old = self.make_transient("2099sph")
        self.private = self.make_transient("2099spi")
        for transient in (self.young, self.old, self.private):
            self.fit(self.bazin, transient, bazin_result(peak_r=60011.0, last_nd=60000.0))
        self.make_spectrum(self.young, 60013.5)  # rest +2.0, obs +2.5, discovery +13.5
        self.make_spectrum(self.old, 60061.0)  # rest +40, obs +50, discovery +61
        self.make_spectrum(self.private, 60013.5, groups=[self.group])

    def names(self, query, user):
        data = QueryDict(query)
        request = types.SimpleNamespace(user=user, GET=data)
        filterset = TransientSearchFilterSet(data, queryset=Transient.objects.filter(name__startswith="2099sp"),
                                             request=request)
        self.assertTrue(filterset.is_valid(), filterset.errors)
        return set(filterset.qs.values_list("name", flat=True))

    def test_default_reference(self):
        self.assertEqual(self.names("spec_phase_min=-5&spec_phase_max=5", self.member), {"2099spg", "2099spi"})
        self.assertEqual(self.names("spec_phase_min=30", self.member), {"2099sph"})

    def test_each_reference(self):
        q = "spec_phase_min=2.2&spec_phase_max=3&spec_phase_ref=%s"
        self.assertEqual(self.names(q % "bazin_obs", self.member), {"2099spg", "2099spi"})
        self.assertEqual(self.names(q % "bazin_rest", self.member), set())
        self.assertEqual(self.names("spec_phase_min=60&spec_phase_ref=discovery", self.member), {"2099sph"})
        self.assertEqual(self.names("spec_phase_min=0&spec_phase_ref=salt_t0", self.member), set())

    def test_reference_alone_filters_nothing(self):
        self.assertEqual(len(self.names("spec_phase_ref=salt_t0", self.member)), 3)

    def test_spectra_the_user_may_not_see_do_not_match(self):
        self.assertEqual(self.names("spec_phase_max=5", self.outsider), {"2099spg"})

    def test_selection_uses_the_filter_for_each_viewer(self):
        selection = selection_svc.create_selection(
            self.member, "Early spectra", visibility="all", search_params="spec_phase_max=5&name_contains=2099sp")
        member_rows = set(selection_svc.selection_queryset(selection, self.member).values_list("name", flat=True))
        outsider_rows = set(selection_svc.selection_queryset(selection, self.outsider).values_list("name", flat=True))
        self.assertEqual(member_rows, {"2099spg", "2099spi"})
        self.assertEqual(outsider_rows, {"2099spg"})


# --------------------------------------------------------------------------- downloads and pages


def preview_client(user, preview=True):
    client = Client()
    client.force_login(user)
    if preview:
        client.cookies[PREVIEW_COOKIE] = "1"
    return client


def read_zip(response):
    body = b"".join(response.streaming_content) if response.streaming else response.content
    return zipfile.ZipFile(io.BytesIO(body))


def manifest(archive):
    return list(csv.DictReader(io.StringIO(archive.read("manifest.csv").decode())))


class DownloadTests(PhaseFixture):
    def setUp(self):
        self.t = self.make_transient("2099spj")
        self.fit(self.bazin, self.t, bazin_result(peak_r=60011.0, last_nd=60000.0))
        self.early = self.make_spectrum(self.t, 60013.5)
        self.late = self.make_spectrum(self.t, 60061.0, instrument=self.other_instrument, err=None)
        self.hidden = self.make_spectrum(self.t, 60015.0, groups=[self.group])
        self.url = reverse("search_download_spectra")
        self.params = "name=2099spj"

    def get(self, user, query="", preview=True):
        data = QueryDict(query, mutable=True)
        data["params"] = self.params
        return preview_client(user, preview).get(self.url, data)

    def test_preview_off_is_404(self):
        self.assertEqual(self.get(self.outsider, preview=False).status_code, 404)
        self.assertEqual(preview_client(self.outsider, False).get(
            reverse("search_download_photometry"), {"params": self.params}).status_code, 404)

    def test_manifest_and_access(self):
        response = self.get(self.outsider)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.streaming)
        archive = read_zip(response)
        rows = manifest(archive)
        self.assertEqual(len(rows), 2)  # the group-only spectrum is not the outsider's
        early = next(r for r in rows if r["instrument"] == self.instrument.name)
        self.assertEqual(early["phase_reference"], "bazin_rest")
        self.assertEqual(early["phase_frame"], "rest")
        self.assertEqual(early["phase_days"], "2.000")
        self.assertEqual(early["redshift_used"], "0.25000")
        self.assertTrue(early["file"] in archive.namelist())
        self.assertIn("wavelength,flux,fluxerr", archive.read(early["file"]).decode())
        self.assertIn("README.txt", archive.namelist())
        self.assertEqual(len(manifest(read_zip(self.get(self.member)))), 3)

    def test_options_phase_instrument_snr(self):
        rows = manifest(read_zip(self.get(self.member, "phase_ref=bazin_obs&phase_min=-1&phase_max=3")))
        self.assertEqual([r["phase_days"] for r in rows], ["2.500"])
        rows = manifest(read_zip(self.get(self.member, "phase_ref=discovery&phase_min=60")))
        self.assertEqual([r["phase_reference"] for r in rows], ["discovery"])
        rows = manifest(read_zip(self.get(self.member, "instrument=%s" % self.other_instrument.name)))
        self.assertEqual([r["instrument"] for r in rows], [self.other_instrument.name])
        rows = manifest(read_zip(self.get(self.member, "snr_min=5")))  # S/N 20 ... 30; no errors: left out
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["median_snr"] for r in rows))

    def test_fit_phases_hidden_from_viewers_missing_photometry(self):
        photometry = TransientPhotometry.objects.create(
            transient=self.t, instrument=self.instrument, obs_group=self.obs_group, **audit_fields(self.staff))
        photometry.groups.add(self.group)
        TransientPhotData.objects.create(photometry=photometry, band=self.band, obs_date=mjd_date(60012.0),
                                         mag=18.0, mag_err=0.1, **audit_fields(self.staff))
        from YSE_App.download_views import HIDDEN_NOTE

        rows = manifest(read_zip(self.get(self.outsider)))
        self.assertTrue(all(r["phase_days"] == "" and r["note"] == HIDDEN_NOTE for r in rows))
        rows = manifest(read_zip(self.get(self.outsider, "phase_ref=discovery")))
        self.assertEqual(sorted(r["phase_days"] for r in rows), ["13.500", "61.000"])
        rows = manifest(read_zip(self.get(self.member)))
        self.assertTrue(all(r["phase_days"] for r in rows))

    def test_object_cap(self):
        with mock.patch.object(views_module, "SEARCH_BULK_PHOTOMETRY_MAX_ROWS", 0):
            response = self.get(self.member)
        self.assertEqual(response.status_code, 302)
        self.assertIn("bulk_result=Too+many", response["Location"])

    def test_bad_option_goes_back_with_a_message(self):
        response = self.get(self.member, "phase_min=abc")
        self.assertEqual(response.status_code, 302)
        self.assertIn("bulk_result=", response["Location"])

    def test_selection_download(self):
        selection = selection_svc.create_selection(self.member, "Mine", search_params=self.params)
        url = reverse("selection_download_spectra", kwargs={"slug": selection.slug})
        rows = manifest(read_zip(preview_client(self.member).get(url, {"phase_max": "20"})))
        self.assertEqual(len(rows), 2)
        self.assertEqual(preview_client(self.outsider).get(url).status_code, 404)  # not shared
        url = reverse("selection_download_photometry", kwargs={"slug": selection.slug})
        response = preview_client(self.member).get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("2099spj.csv", read_zip(response).namelist())

    def test_photometry_options(self):
        photometry = TransientPhotometry.objects.create(
            transient=self.t, instrument=self.instrument, obs_group=self.obs_group, **audit_fields(self.staff))
        for mjd in (60005.0, 60012.0, 60030.0):
            TransientPhotData.objects.create(photometry=photometry, band=self.band, obs_date=mjd_date(mjd),
                                             mag=18.0, mag_err=0.1, **audit_fields(self.staff))
        plain = views_module.bulk_photometry_zip_response(self.member, [self.t])
        archive = zipfile.ZipFile(io.BytesIO(plain.content))
        self.assertEqual(archive.read("2099spj.csv").decode().count("\n6"), 3)
        self.assertNotIn("Download options", archive.read("README.txt").decode())

        options = PhotometryOptions.from_params(QueryDict("mjd_min=60010&mjd_max=60020"))
        archive = zipfile.ZipFile(io.BytesIO(views_module.bulk_photometry_zip_response(
            self.member, [self.t], options).content))
        self.assertEqual(archive.read("2099spj.csv").decode().count("\n6"), 1)
        self.assertIn("left out: 2", archive.read("README.txt").decode())

        options = PhotometryOptions.from_params(QueryDict("phase_ref=bazin_obs&phase_min=-10&phase_max=0"))
        archive = zipfile.ZipFile(io.BytesIO(views_module.bulk_photometry_zip_response(
            self.member, [self.t], options).content))
        self.assertEqual(archive.read("2099spj.csv").decode().count("\n6"), 1)  # MJD 60005

        options = PhotometryOptions.from_params(QueryDict("band=nope"))
        archive = zipfile.ZipFile(io.BytesIO(views_module.bulk_photometry_zip_response(
            self.member, [self.t], options).content))
        self.assertEqual(archive.read("2099spj.csv").decode().count("\n6"), 0)


class PageTests(PhaseFixture):
    def test_search_page_preview_off_unchanged(self):
        html = preview_client(self.member, preview=False).get(reverse("search")).content.decode()
        self.assertNotIn('id="search-group-spectra"', html)
        self.assertNotIn('id="download-menu"', html)
        self.assertIn('id="search-group-relations"', html)

    def test_search_page_preview_on(self):
        html = preview_client(self.member).get(reverse("search"), {"name_contains": "2099"}).content.decode()
        self.assertIn('id="search-group-spectra"', html)
        self.assertIn('name="spec_phase_ref"', html)
        self.assertIn('id="download-menu"', html)
        self.assertIn('id="download-spectra-form"', html)
        self.assertIn(reverse("search_download_spectra"), html)

    def test_selection_page_has_the_menu(self):
        selection = selection_svc.create_selection(self.member, "Menu", search_params="name_contains=2099")
        html = preview_client(self.member).get(
            reverse("selection_detail", kwargs={"slug": selection.slug})).content.decode()
        self.assertIn(reverse("selection_download_spectra", kwargs={"slug": selection.slug}), html)


# --------------------------------------------------------------------------- ON DELETE


class ForeignKeyListTests(SimpleTestCase):
    def test_every_foreign_key_is_listed(self):
        model = apps.get_model("YSE_App", "SpectrumPhase")
        expected = set()
        for field in model._meta.local_concrete_fields:
            if isinstance(field, models.ForeignKey):
                self.assertIs(field.remote_field.on_delete, models.CASCADE)
                expected.add((model._meta.db_table, field.column, field.related_model._meta.db_table,
                              phase_migration.CASCADE))
        self.assertEqual(set(phase_migration.FOREIGN_KEYS), expected)


def _sql(statement, params):
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


class RawDeleteTests(PhaseFixture):
    """Deletes as code that does not know the phase table issues them (develop on the shared DB)."""

    def setUp(self):
        if connection.vendor != "mysql":
            self.skipTest("ON DELETE rules are MySQL constraints")

    def test_constraints_carry_on_delete_cascade(self):
        from YSE_App.common.fk_on_delete import current_constraints

        for table, column, _, rule in phase_migration.FOREIGN_KEYS:
            self.assertEqual([row[3] for row in current_constraints(connection, table, column)], [rule])

    def test_spectrum_delete_cascades(self):
        transient = self.make_transient("2099spk")
        spectrum = self.make_spectrum(transient, 60021.0, pixels=False)
        self.assertTrue(SpectrumPhase.objects.filter(spectrum=spectrum).exists())
        _sql("DELETE FROM YSE_App_transientspectrum_groups WHERE transientspectrum_id = %s", [spectrum.id])
        _sql("DELETE FROM YSE_App_transientspectrum WHERE id = %s", [spectrum.id])
        self.assertFalse(SpectrumPhase.objects.exists())

    def test_transient_delete_cascades(self):
        owner = self.make_transient("2099spl")
        spectrum = self.make_spectrum(owner, 60021.0, pixels=False)
        other = self.make_transient("2099spm")
        # A row that points at ``other`` only through transient_id: deleting ``other`` must remove it.
        SpectrumPhase.objects.filter(spectrum=spectrum).update(transient=other)
        with connection.cursor() as cursor:
            cursor.execute("SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                           "WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME = 'YSE_App_transient'")
            children = [(t, c) for t, c in cursor.fetchall() if t != "YSE_App_spectrumphase"]
            for _attempt in range(3):  # a child row may itself have children: retry in a later pass
                for table, column in children:
                    try:
                        cursor.execute(f"DELETE FROM `{table}` WHERE `{column}` = %s", [other.id])
                    except IntegrityError:
                        pass
        _sql("DELETE FROM YSE_App_transient WHERE id = %s", [other.id])
        self.assertFalse(SpectrumPhase.objects.exists())
        self.assertTrue(TransientSpectrum.objects.filter(pk=spectrum.pk).exists())
