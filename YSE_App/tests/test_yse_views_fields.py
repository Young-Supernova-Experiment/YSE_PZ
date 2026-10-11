"""Nearest-field columns (batched per page) and the yse_fields page (#468, #469, #470)."""

import datetime

from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django_tables2 import RequestConfig

from YSE_App.models import (
    ObservationGroup,
    SurveyField,
    Transient,
    TransientTag,
)
from YSE_App.models.transient_models import batch_nearest_survey_fields
from YSE_App.table_utils import (
    AdjustFieldTransientTable,
    FieldTransientTable,
    PageFirstQuerySet,
    annotate_dashboard_transient_fields,
)
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_photometry,
    audit_fields,
    create_instrument_stack,
    create_minimal_transient,
    create_test_user,
)


def _survey_field(user, group, instrument, field_id, ra, dec, ztf_id=None):
    return SurveyField.objects.create(
        obs_group=group,
        field_id=field_id,
        instrument=instrument,
        ztf_field_id=ztf_id,
        ra_cen=ra,
        dec_cen=dec,
        width_deg=3.3,
        height_deg=3.3,
        **audit_fields(user),
    )


class NearestFieldBatchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("yse_views_nearest_user")
        audit = audit_fields(cls.user)
        cls.ztf, cls.instrument, _ = create_instrument_stack(
            cls.user, obs_group_name="ZTF"
        )
        cls.yse = ObservationGroup.objects.get_or_create(name="YSE", defaults=audit)[0]
        _survey_field(cls.user, cls.ztf, cls.instrument, "z100", 10.0, 20.0)
        _survey_field(cls.user, cls.ztf, cls.instrument, "z200", 11.0, 20.0)  # also in box
        _survey_field(cls.user, cls.yse, cls.instrument, "y1.1", 12.0, 21.0, "y1")
        cls.transients = [
            create_minimal_transient(
                cls.user, name=f"nf{i}", ra=10.0 + 0.1 * i, dec=20.0 + 0.1 * i
            )
            for i in range(12)
        ]
        # far from every field
        cls.far = create_minimal_transient(cls.user, name="nffar", ra=200.0, dec=-60.0)

    def test_matches_old_rule_first_field_and_separation(self):
        t = self.transients[0]
        (zid, zsep), = batch_nearest_survey_fields([t], "ztf")
        self.assertEqual(zid, "z100")
        self.assertEqual(zsep, "0.0")
        (yid, ysep), = batch_nearest_survey_fields([t], "yse")
        self.assertEqual(yid, "y1.1")
        from astropy.coordinates import SkyCoord

        expect = SkyCoord(t.ra, t.dec, unit="deg").separation(
            SkyCoord(12.0, 21.0, unit="deg")
        ).degree
        self.assertEqual(ysep, "%.1f" % expect)

    def test_no_match_returns_none_instead_of_raising(self):
        self.assertEqual(batch_nearest_survey_fields([self.far], "ztf"), [(None, None)])
        self.assertEqual(batch_nearest_survey_fields([self.far], "yse"), [(None, None)])
        self.assertIsNone(self.far.nearest_ztf_field())
        self.assertIsNone(self.far.nearest_ztf_field_sep())
        self.assertIsNone(self.far.nearest_yse_field())
        self.assertIsNone(self.far.nearest_yse_field_sep())

    def test_model_methods_agree_with_batch(self):
        t = self.transients[3]
        self.assertEqual(t.nearest_ztf_field(), "z100")
        self.assertEqual(t.nearest_yse_field(), "y1.1")

    def _table_surveyfield_queries(self, table_cls, n):
        qs = annotate_dashboard_transient_fields(
            Transient.objects.filter(name__startswith="nf").order_by("name")
        )
        qs = PageFirstQuerySet.wrap(qs.filter(pk__in=[t.pk for t in self.transients[:n]]))
        request = RequestFactory().get("/")
        table = table_cls(qs, prefix="t")
        with CaptureQueriesContext(connection) as ctx:
            RequestConfig(request, paginate={"per_page": 20}).configure(table)
            cols = ("ztf_field", "ztf_sep") if table_cls is FieldTransientTable else (
                "yse_field", "yse_sep")
            cells = [[row.get_cell(c) for c in cols] for row in table.paginated_rows]
        self.assertEqual(len(cells), n)
        return sum("yse_app_surveyfield" in q["sql"].lower() for q in ctx.captured_queries)

    def test_surveyfield_queries_do_not_grow_with_rows(self):
        for table_cls in (FieldTransientTable, AdjustFieldTransientTable):
            few = self._table_surveyfield_queries(table_cls, 2)
            many = self._table_surveyfield_queries(table_cls, 12)
            self.assertEqual(few, 1)
            self.assertEqual(many, 1)


class YseFieldsPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("yse_views_fields_user")
        audit = audit_fields(cls.user)
        ztf, instrument, _ = create_instrument_stack(cls.user, obs_group_name="ZTF")
        yse = ObservationGroup.objects.get_or_create(name="YSE", defaults=audit)[0]
        _survey_field(cls.user, yse, instrument, "400.1", 30.0, 10.0, "400")
        _survey_field(cls.user, yse, instrument, "400.2", 33.0, 10.0, "400")
        tag = TransientTag.objects.create(name="YSE", **audit)
        cls.bright = create_minimal_transient(
            cls.user, name="2026bright", ra=30.5, dec=10.5
        )
        attach_synthetic_photometry(cls.user, cls.bright, n_points=3)  # mags 18.0-18.1
        cls.faint = create_minimal_transient(
            cls.user, name="2026faint", ra=30.6, dec=10.4
        )
        for t in (cls.bright, cls.faint):
            t.tags.add(tag)
            Transient.objects.filter(pk=t.pk).update(
                disc_date=timezone.now() - datetime.timedelta(days=3)
            )

    def setUp(self):
        self.client.force_login(self.user)

    def test_page_renders_and_uses_url_magnitude_limit(self):
        # tile 400 spans ra 30-33 deg = hours 2.0-2.2
        url = reverse("yse_fields", args=["1", "3", "19"])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        tables = resp.context["survey_tables"]
        self.assertEqual([t[1] for t in tables], ["400"])
        names = [r.record.name for r in tables[0][0].paginated_rows]
        self.assertEqual(names, ["2026bright"])  # point brighter than 19; faint has none

    def test_tighter_limit_excludes_everything(self):
        resp = self.client.get(reverse("yse_fields", args=["1", "3", "10"]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(list(resp.context["survey_tables"][0][0].paginated_rows), [])

    def test_bad_magnitude_is_404(self):
        self.assertEqual(self.client.get(reverse("yse_fields", args=["1", "3", "abc"])).status_code, 404)
