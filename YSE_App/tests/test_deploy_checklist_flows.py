"""
Deploy checklist, part 1: the data-entry and download flows the team checks by
hand before a release (docs/deploy-checklist-ci.md).

Each test drives the same endpoint the browser or the upload scripts use:
upload spectrum form, download spectra/photometry/all-data, the
add_transient_phot / add_transient_spec basic-auth APIs, follow-up requests
showing up on the observing calendar and observing-night page, classical and
ToO resource forms, status changes over the REST API, personal-dashboard query
add/remove, the summary view, forced photometry and the plot endpoints.
"""

from __future__ import annotations

import datetime
import json
import time
import zipfile
from io import BytesIO
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

import YSE_App.views as views_module
from YSE_App.models import (
    ClassicalObservingDate,
    ClassicalResource,
    Log,
    SurveyField,
    SurveyFieldMSB,
    SurveyObservation,
    ToOResource,
    Transient,
    TransientFollowup,
    TransientPhotData,
    TransientSpecData,
    TransientSpectrum,
    UserQuery,
    YSEOnCallDate,
)
from YSE_App.services.followup_requests import create_or_attach_request
from YSE_App.tests.deploy_checklist_helpers import (
    CHECKLIST_PASSWORD,
    TESS_OBS_PATCH_TARGET,
    add_spectrum_points,
    basic_auth_header,
    create_instrument,
    create_principal_investigator,
    create_telescope,
    ensure_classical_night_type,
    ensure_followup_statuses,
    ensure_observation_group,
    ensure_task_statuses,
    ensure_yse_group,
    fmt_dt,
    iers_offline,
    transient_slugs_in_order,
    utc_days_from_now,
)
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_minimal_transient,
    create_test_user,
    create_transient_with_synthetic_data,
    ensure_transient_statuses,
)


class _ExplorerToDefault:
    """personaldashboard/transient_summary read saved SQL via the 'explorer' alias;
    inside a TestCase transaction only 'default' can see the seeded rows."""

    def __getitem__(self, alias):
        if alias == "explorer":
            return connections["default"]
        return connections[alias]


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DeployChecklistFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("checklist_flow_user", is_staff=True)
        cls.user.set_password(CHECKLIST_PASSWORD)
        cls.user.save()
        cls.yse_group = ensure_yse_group(cls.user)
        cls.statuses = ensure_transient_statuses(cls.user)
        cls.followup_statuses = ensure_followup_statuses(cls.user)
        cls.task_statuses = ensure_task_statuses(cls.user)

        cls.transient = create_transient_with_synthetic_data(
            cls.user, name="chk-flow-sn", n_phot_points=6
        )
        cls.spectrum = TransientSpectrum.objects.filter(transient=cls.transient).first()
        add_spectrum_points(cls.user, cls.spectrum, n_points=5)
        cls.photometry = cls.transient.transientphotometry_set.select_related(
            "instrument", "obs_group"
        ).first()
        cls.band = TransientPhotData.objects.filter(photometry=cls.photometry).first().band

        cls.telescope = create_telescope(cls.user, "ChecklistTel")
        cls.pi = create_principal_investigator(cls.user)
        cls.night_type = ensure_classical_night_type(cls.user)

        # SpectrumUploadForm only accepts a fixed obs_group / instrument whitelist.
        cls.ucsc_group = ensure_observation_group(cls.user, "UCSC")
        cls.kast, _ = create_instrument(cls.user, cls.telescope, "KAST")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)
        self.auth = {"HTTP_AUTHORIZATION": basic_auth_header(self.user.username, CHECKLIST_PASSWORD)}

    # ------------------------------------------------------------------ spectra

    def test_upload_spectrum_form_renders(self):
        response = self.client.get(reverse("upload_spectrum"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "filename")

    def test_upload_spectrum_form_creates_spectrum_and_points(self):
        n_before = TransientSpectrum.objects.filter(transient=self.transient).count()
        payload = {
            "transient": self.transient.id,
            "ra": self.transient.ra,
            "dec": self.transient.dec,
            "spec_phase": "",
            "obs_date": "2026-09-01T03:00",
            "obs_group": self.ucsc_group.id,
            "instrument": self.kast.id,
            "data_quality": "",
            "filename": SimpleUploadedFile(
                "spec.txt",
                b"# wavelength flux fluxerr\n4000 1.0 0.1\n4010 1.1 0.1\n4020 1.2 0.1\n",
            ),
        }
        response = self.client.post(reverse("upload_spectrum"), payload)
        self.assertEqual(response.status_code, 302, getattr(response, "content", b""))
        self.assertIn(self.transient.slug, response.url)
        self.assertEqual(
            TransientSpectrum.objects.filter(transient=self.transient).count(), n_before + 1
        )
        new_spec = TransientSpectrum.objects.filter(
            transient=self.transient, instrument=self.kast
        ).get()
        self.assertEqual(TransientSpecData.objects.filter(spectrum=new_spec).count(), 3)

    def test_download_spectra_returns_zip_of_csv(self):
        response = self.client.get(reverse("download_spectra", kwargs={"slug": self.transient.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/x-zip-compressed")
        archive = zipfile.ZipFile(BytesIO(response.content))
        names = archive.namelist()
        self.assertGreaterEqual(len(names), 1, names)
        body = archive.read(names[0]).decode()
        self.assertIn("wavelength,flux,fluxerr", body)
        self.assertIn("4000.000", body)

    def test_add_transient_spec_api_basic_auth(self):
        n_before = TransientSpectrum.objects.filter(transient=self.transient).count()
        payload = {
            "header": {
                "instrument": self.photometry.instrument.name,
                "obs_group": self.photometry.obs_group.name,
                "groups": "",
                "data_quality": None,
                "obs_date": "2026-09-02T00:00:00",
                "ra": self.transient.ra,
                "dec": self.transient.dec,
                "rlap": None,
                "redshift": None,
                "redshift_err": None,
                "redshift_quality": None,
                "spectrum_notes": "ci upload",
                "spec_phase": None,
                "clobber": False,
            },
            "transient": {"name": self.transient.name},
            "0": {"wavelength": 5000.0, "flux": 1.0, "flux_err": 0.1},
            "1": {"wavelength": 5010.0, "flux": 1.1, "flux_err": 0.1},
        }
        response = self.client.post(
            "/add_transient_spec/",
            data=json.dumps(payload),
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["message"], "success")
        self.assertEqual(
            TransientSpectrum.objects.filter(transient=self.transient).count(), n_before + 1
        )
        spec = TransientSpectrum.objects.filter(
            transient=self.transient, spectrum_notes="ci upload"
        ).get()
        self.assertEqual(TransientSpecData.objects.filter(spectrum=spec).count(), 2)

    # --------------------------------------------------------------- photometry

    def test_add_transient_phot_api_basic_auth(self):
        n_before = TransientPhotData.objects.filter(photometry__transient=self.transient).count()
        payload = {
            "header": {"clobber": False, "mjdmatchmin": 0.01},
            "transient": {
                "name": self.transient.name,
                "ra": self.transient.ra,
                "dec": self.transient.dec,
                "status": "New",
                "obs_group": self.photometry.obs_group.name,
            },
            "photheader": {
                "instrument": self.photometry.instrument.name,
                "obs_group": self.photometry.obs_group.name,
                "groups": "",
            },
            "0": {
                "obs_date": "2026-09-10T00:00:00",
                "band": self.band.name,
                "mag": 19.1,
                "mag_err": 0.05,
                "flux": None,
                "flux_err": None,
                "forced": None,
                "diffim": None,
                "flux_zero_point": None,
                "data_quality": None,
                "discovery_point": False,
            },
        }
        with mock.patch(TESS_OBS_PATCH_TARGET, return_value=False):
            response = self.client.post(
                "/add_transient_phot/",
                data=json.dumps(payload),
                content_type="application/json",
                **self.auth,
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["message"], "success")
        self.assertEqual(
            TransientPhotData.objects.filter(photometry__transient=self.transient).count(),
            n_before + 1,
        )

    def test_download_photometry_snana_text(self):
        response = self.client.get(
            reverse("download_photometry", kwargs={"slug": self.transient.slug})
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/plain"))
        body = response.content.decode()
        self.assertIn("VARLIST:", body)
        n_obs = sum(1 for line in body.splitlines() if line.startswith("OBS:"))
        self.assertEqual(n_obs, 6)

    def test_download_data_json_bundle_within_ceiling(self):
        """'download all data' times out on prod for big objects; CI keeps a ceiling
        on a small object so a pathological regression still fails fast."""
        started = time.monotonic()
        response = self.client.get(reverse("download_data", kwargs={"slug": self.transient.slug}))
        elapsed = time.monotonic() - started
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        bundle = data[self.transient.name]
        self.assertEqual(set(bundle), {"transient", "host", "photometry", "spectra"})
        self.assertGreaterEqual(len(bundle["photometry"]), 1)
        self.assertIn("data", bundle["photometry"][0])
        self.assertGreaterEqual(len(bundle["spectra"]), 1)
        self.assertLess(elapsed, 20.0, f"download_data took {elapsed:.1f}s for a 6-point object")

    def test_forced_photometry_request_logs_and_rate_limits(self):
        """ztf_forced_phot view: first call submits (network mocked), second is refused."""
        transient = create_minimal_transient(self.user, name="chk-ztf-fp")
        fake = mock.MagicMock()
        fake.return_value.run_ztf_fp.return_value = "ztf_fp_ci.log"
        url = reverse("ztf_forced_phot", kwargs={"slug": transient.slug})
        with mock.patch("YSE_App.data_ingest.ZTF_Forced_Phot.ZTF_Forced_Phot", fake):
            first = self.client.get(url)
            second = self.client.get(url)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["msg"], "success")
        self.assertTrue(fake.return_value.run_ztf_fp.called)
        self.assertEqual(Log.objects.filter(transient=transient, comment__startswith="ZTF Forced Phot").count(), 1)
        self.assertTrue(second.json()["msg"].startswith("error"))

    def test_flux_plot_with_photometry_returns_html(self):
        response = self.client.get(f"/lightcurveplot_flux/{self.transient.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(response.content), 0)

    def test_salt2_plot_endpoints_without_fit_return_200(self):
        """salt2fit=0 is the un-fitted branch; the real SALT3 fit needs the
        sncosmo model download and multi-band data (manual, see docs)."""
        for name in ("salt2plot", "salt2fluxplot"):
            response = self.client.get(
                reverse(name, kwargs={"transient_id": self.transient.id, "salt2fit": "0"})
            )
            self.assertEqual(response.status_code, 200, name)

    # ------------------------------------------------------------- follow-ups

    def _classical_resource(self, *, begin_days, end_days, obs_days, name_suffix=""):
        audit = audit_fields(self.user)
        telescope = self.telescope
        if name_suffix:
            telescope = create_telescope(self.user, f"ChecklistTel{name_suffix}")
        resource = ClassicalResource.objects.create(
            telescope=telescope,
            principal_investigator=self.pi,
            begin_date_valid=utc_days_from_now(begin_days, hour=0),
            end_date_valid=utc_days_from_now(end_days, hour=0),
            **audit,
        )
        resource.groups.add(self.yse_group)
        night = ClassicalObservingDate.objects.create(
            resource=resource,
            night_type=self.night_type,
            obs_date=utc_days_from_now(obs_days, hour=12),
            **audit,
        )
        return resource, night

    def test_followup_request_appears_on_calendar_night_and_resources(self):
        resource, night = self._classical_resource(begin_days=1, end_days=6, obs_days=3)
        payload = {
            "status": self.followup_statuses["Requested"].id,
            "classical_resource": resource.id,
            "valid_start": fmt_dt(resource.begin_date_valid),
            "valid_stop": fmt_dt(resource.end_date_valid),
            "priority": 4.0,
            "comment": "checklist follow-up",
            "transient": self.transient.id,
            # New follow-ups need an audience: the collaboration group(s) that
            # can see the linked resource (the form rejects an empty selection).
            "audience_groups": [self.yse_group.id],
        }
        with mock.patch(TESS_OBS_PATCH_TARGET, return_value=False):
            response = self.client.post(
                reverse("add_transient_followup"),
                payload,
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertIn(self.telescope.name, data["classical_resource"])
        followup = TransientFollowup.objects.get(pk=data["id"])
        self.assertEqual(followup.classical_resource_id, resource.id)

        # Observing calendar lists the run night for that telescope.
        calendar = self.client.get(reverse("observing_calendar"))
        self.assertEqual(calendar.status_code, 200)
        self.assertContains(calendar, self.telescope.name)

        # Observing night page lists the request.
        night_url = reverse(
            "observing_night",
            kwargs={
                "telescope": self.telescope.name.replace(" ", "_"),
                "obs_date": night.obs_date.strftime("%Y-%m-%d"),
                "pi_name": "None",
            },
        )
        with iers_offline():
            night_page = self.client.get(night_url)
        self.assertEqual(night_page.status_code, 200)
        self.assertContains(night_page, self.transient.name)

        # Transient-detail "resources" tables show the upcoming classical night.
        fragment = self.client.get(
            reverse("transient_detail_resources_fragment", kwargs={"transient_id": self.transient.id})
        )
        self.assertEqual(fragment.status_code, 200)
        self.assertContains(fragment, self.telescope.name)

    def test_change_followup_status_via_api_patch(self):
        followup, _child, created = create_or_attach_request(
            self.user,
            self.transient,
            status=self.followup_statuses["Requested"],
            valid_start=timezone.now(),
            valid_stop=timezone.now() + datetime.timedelta(days=7),
            priority=4.0,
        )
        self.assertTrue(created)
        successful = self.followup_statuses["Successful"]
        response = self.client.patch(
            f"/api/transientfollowups/{followup.id}/",
            data=json.dumps({"status": f"http://testserver/api/followupstatuses/{successful.id}/"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        followup.refresh_from_db()
        self.assertEqual(followup.status_id, successful.id)

    def test_change_transient_status_via_api_patch(self):
        transient = create_minimal_transient(self.user, name="chk-status-sn")
        watch = self.statuses["Watch"]
        with mock.patch(TESS_OBS_PATCH_TARGET, return_value=False):
            response = self.client.patch(
                f"/api/transients/{transient.id}/",
                data=json.dumps({"status": f"http://testserver/api/transientstatuses/{watch.id}/"}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        transient.refresh_from_db()
        self.assertEqual(transient.status_id, watch.id)

    # --------------------------------------------------------------- resources

    def test_add_classical_resource_form_creates_resource_and_night(self):
        n_res = ClassicalResource.objects.count()
        n_nights = ClassicalObservingDate.objects.count()
        response = self.client.post(
            reverse("add_classical_resource"),
            {
                "telescope": self.telescope.id,
                "principal_investigator": self.pi.id,
                "observing_date": "2026-11-01 00:00:00",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["telescope"], self.telescope.name)
        self.assertEqual(body["obs_date"], "2026-11-01")
        self.assertEqual(ClassicalResource.objects.count(), n_res + 1)
        self.assertEqual(ClassicalObservingDate.objects.count(), n_nights + 1)
        resource = ClassicalResource.objects.latest("id")
        self.assertEqual(resource.created_by_id, self.user.id)
        self.assertEqual(
            list(resource.groups.values_list("name", flat=True)), ["YSE"]
        )

    def test_add_too_resource_form_and_resources_table(self):
        n_res = ToOResource.objects.count()
        response = self.client.post(
            reverse("add_too_resource"),
            {
                "telescope": self.telescope.id,
                "principal_investigator": self.pi.id,
                "begin_date_valid": fmt_dt(utc_days_from_now(-1, hour=0)),
                "end_date_valid": fmt_dt(utc_days_from_now(180, hour=0)),
                "awarded_too_hours": 10,
                "used_too_hours": 0,
                "awarded_too_triggers": 5,
                "used_too_triggers": 0,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(ToOResource.objects.count(), n_res + 1)
        too = ToOResource.objects.latest("id")
        self.assertEqual(too.awarded_too_hours, 10.0)
        too.groups.add(self.yse_group)

        fragment = self.client.get(
            reverse("transient_detail_resources_fragment", kwargs={"transient_id": self.transient.id})
        )
        self.assertEqual(fragment.status_code, 200)
        self.assertContains(fragment, self.telescope.name)

        too_calendar = self.client.get(reverse("too_calendar"))
        self.assertEqual(too_calendar.status_code, 200)

    # ------------------------------------------------------------- on-call

    def test_oncall_schedule_entry_shows_on_calendar(self):
        """OncallForm's user field is built at import time from the YSE group, so the
        form POST cannot be exercised on a fresh DB; the model + page are."""
        entry = YSEOnCallDate.objects.create(
            on_call_date=utc_days_from_now(2, hour=0), **audit_fields(self.user)
        )
        entry.user.add(self.user)
        response = self.client.get(reverse("yse_oncall_calendar"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.user.username)

    # ---------------------------------------------------------- survey fields

    def _survey_stack(self):
        yse_obs_group = ensure_observation_group(self.user, "YSE")
        ps1 = create_telescope(self.user, "Pan-STARRS1")
        gpc1, bands = create_instrument(self.user, ps1, "GPC1", band_names=("g", "r", "i", "z"))
        return yse_obs_group, ps1, gpc1, bands

    def test_add_survey_field_api_creates_field_and_msb(self):
        self._survey_stack()
        payload = {
            "0": {
                "field_id": "998.B",
                "ra_cen": 40.0,
                "dec_cen": 10.0,
                "width_deg": 3.3,
                "height_deg": 3.3,
                "instrument": "GPC1",
                "ztf_field_id": "998",
                "cadence": 3,
                "active": 1,
            }
        }
        with iers_offline():
            response = self.client.post(
                "/add_yse_survey_fields/",
                data=json.dumps(payload),
                content_type="application/json",
                **self.auth,
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["message"], "success")
        field = SurveyField.objects.get(field_id="998.B")
        self.assertEqual(field.instrument.name, "GPC1")
        msb = SurveyFieldMSB.objects.get(name="998")
        self.assertIn(field, msb.survey_fields.all())

    def test_survey_obs_schedule_and_ingest_observation_record(self):
        yse_obs_group, _ps1, gpc1, bands = self._survey_stack()
        audit = audit_fields(self.user)
        field = SurveyField.objects.create(
            obs_group=yse_obs_group,
            field_id="999.A",
            cadence=3,
            instrument=gpc1,
            ztf_field_id="999",
            active=True,
            ra_cen=50.0,
            dec_cen=-5.0,
            width_deg=3.3,
            height_deg=3.3,
            **audit,
        )
        from YSE_App.common.utilities import date_to_mjd

        tonight = timezone.now().replace(hour=8, minute=0, second=0, microsecond=0)
        mjd_requested = date_to_mjd(tonight)
        scheduled = SurveyObservation.objects.create(
            mjd_requested=mjd_requested,
            survey_field=field,
            status=self.task_statuses["Requested"],
            exposure_time=27,
            photometric_band=bands["g"],
            **audit,
        )

        # Schedule pages render with a pending observation.
        with iers_offline():
            calendar = self.client.get(reverse("yse_observing_calendar"))
            night = self.client.get(
                reverse("yse_observing_night", kwargs={"obs_date": tonight.strftime("%Y-%m-%d")})
            )
        self.assertEqual(calendar.status_code, 200)
        self.assertEqual(night.status_code, 200)

        # Ingest the observation record the way the yse_obs cron does.
        payload = {
            "0": {
                "survey_field": "999.A",
                "ra_cen": 50.0,
                "dec_cen": -5.0,
                "obs_mjd": mjd_requested + 0.02,
                "status": "Successful",
                "exposure_time": 27,
                "photometric_band": "GPC1-g",
                "image_id": "ci-image-1",
                "mag_lim": 21.5,
            }
        }
        with iers_offline():
            response = self.client.post(
                "/add_yse_survey_obs/",
                data=json.dumps(payload),
                content_type="application/json",
                **self.auth,
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["message"], "success")
        records = SurveyObservation.objects.filter(survey_field=field, obs_mjd__isnull=False)
        self.assertEqual(records.count(), 1)
        record = records.get()
        self.assertEqual(record.image_id, "ci-image-1")
        self.assertEqual(record.status.name, "Successful")
        self.assertEqual(record.pk, scheduled.pk, "ingest should update the scheduled row")

    # ------------------------------------------------- personal dashboard

    def test_add_and_remove_personal_dashboard_query(self):
        from explorer.models import Query

        query = Query.objects.create(
            title="Checklist saved query",
            sql=f"SELECT name FROM YSE_App_transient WHERE name = '{self.transient.name}'",
            description="deploy checklist",
            snapshot=False,
            created_by_user=self.user,
        )
        response = self.client.post(
            reverse("add_dashboard_query"),
            {"query": query.id, "python_query": ""},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200, response.content)
        user_query = UserQuery.objects.get(user=self.user, query=query)

        with mock.patch.object(views_module, "connections", _ExplorerToDefault()):
            shell = self.client.get(reverse("personaldashboard"))
            section = self.client.get(
                reverse("personaldashboard_section", kwargs={"user_query_id": user_query.id})
            )
            summary = self.client.get(
                reverse("transient_summary", kwargs={"status_or_query_name": query.title})
            )
        self.assertEqual(shell.status_code, 200)
        self.assertEqual(section.status_code, 200)
        # The shell renders each saved query's box (title); the AJAX section
        # fragment is only the table body for that query.
        self.assertContains(shell, query.title)
        self.assertIn(self.transient.slug, transient_slugs_in_order(section.content.decode()))
        self.assertEqual(summary.status_code, 200)
        self.assertContains(summary, self.transient.name)

        removal = self.client.post(reverse("remove_dashboard_query", kwargs={"pk": user_query.id}))
        self.assertEqual(removal.status_code, 302)
        self.assertEqual(removal.url, reverse("personaldashboard"))
        self.assertFalse(UserQuery.objects.filter(pk=user_query.id).exists())

    def test_transient_summary_for_status(self):
        response = self.client.get(
            reverse("transient_summary", kwargs={"status_or_query_name": "New"})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.transient.name)
        self.assertTrue(Transient.objects.filter(name=self.transient.name, status__name="New").exists())
