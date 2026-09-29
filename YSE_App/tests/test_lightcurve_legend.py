"""Light-curve legend layout, ordering and colours (#226, #91, #249, #250, #251).

The legend below each Bokeh light curve is laid out in columns whose count
depends on the longest label, series are ordered by telescope family and
filter wavelength regardless of which bands have data, and clicking a legend
entry hides the whole series.  These tests build a transient with many
instrument+band series of varying label length and inspect the Bokeh
document the views embed.
"""

import datetime
import json
import os
import re
from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from YSE_App.common.filter_display import (
    FILTER_COLORS,
    band_display_color,
    legend_sort_key,
    telescope_display_name,
)
from YSE_App.common.legend_layout import (
    MAX_COLUMNS,
    ROW_HEIGHT_PX,
    legend_column_count,
    legend_height_px,
    legend_label_width_px,
    legend_row_count,
    legend_rows,
    requested_plot_width,
)
from YSE_App.models import (
    Instrument,
    ObservationGroup,
    Observatory,
    PhotometricBand,
    Telescope,
    TransientPhotData,
    TransientPhotometry,
)
from YSE_App.tests.fixtures_minimal import (
    audit_fields,
    create_minimal_transient,
    create_test_user,
)

# telescope row name, instrument name, band names.  The ZTF telescope row is
# deliberately named like the physical telescope so the label must come from
# the instrument (#250); Thacher matches no family and keeps its raw name.
SERIES = (
    ("Palomar 48", "ZTF-Cam", ["g-ZTF", "r-ZTF", "i-ZTF"]),
    ("Pan-STARRS1", "GPC1", ["g", "r", "i", "z", "y", "w"]),
    ("Swope", "Direct/4Kx4K", ["u", "B", "V"]),
    ("ATLAS", "ACAM1", ["cyan-ATLAS", "orange-ATLAS"]),
    ("Thacher", "Thacher-Cam", ["H", "J"]),
)

_FILE_HTML_JSON_RE = re.compile(r'<script type="application/json" id="[^"]+">(.*?)</script>', re.S)
_COMPONENTS_JSON_RE = re.compile(r"const docs_json = '(.*?)';\n", re.S)


def seed_many_band_transient(user, name="lc-legend-sn", series=SERIES, *, points=4):
    """A transient with one photometry row per instrument and ``points`` mags per band."""
    audit = audit_fields(user)
    transient = create_minimal_transient(user, name=name, obs_group_name="lc-legend-group")
    obs_group = ObservationGroup.objects.get(name="lc-legend-group")
    observatory, _ = Observatory.objects.get_or_create(
        name="LegendObs", defaults={"utc_offset": 0, "tz_name": "UTC", **audit}
    )
    base = timezone.now() - datetime.timedelta(days=40)
    for tel_name, inst_name, bands in series:
        telescope, _ = Telescope.objects.get_or_create(
            name=tel_name,
            defaults={"observatory": observatory, "latitude": 0.0, "longitude": 0.0,
                      "elevation": 0.0, **audit},
        )
        instrument, _ = Instrument.objects.get_or_create(
            name=inst_name, defaults={"telescope": telescope, **audit}
        )
        photometry = TransientPhotometry.objects.create(
            transient=transient, instrument=instrument, obs_group=obs_group, **audit
        )
        rows = []
        for k, band_name in enumerate(bands):
            band, _ = PhotometricBand.objects.get_or_create(
                name=band_name, instrument=instrument,
                defaults={"disp_color": None, "disp_symbol": "circle", **audit},
            )
            for i in range(points):
                rows.append(TransientPhotData(
                    photometry=photometry, band=band,
                    obs_date=base + datetime.timedelta(days=5 * i + 0.1 * k),
                    mag=18.0 + 0.2 * k + 0.05 * i, mag_err=0.05,
                    discovery_point=(i == 0 and k == 0 and inst_name == "GPC1"),
                    **audit,
                ))
            # one upper limit (flux / flux_err < 3, no mag)
            rows.append(TransientPhotData(
                photometry=photometry, band=band,
                obs_date=base - datetime.timedelta(days=3 + k),
                mag=None, mag_err=None, flux=10.0, flux_err=20.0, flux_zero_point=27.5,
                **audit,
            ))
        TransientPhotData.objects.bulk_create(rows)
    return transient


def bokeh_doc_from_html(html):
    """The Bokeh document JSON embedded by file_html() (detail, flux) or components() (summary)."""
    match = _FILE_HTML_JSON_RE.search(html)
    if match:
        raw = match.group(1)
    else:
        match = _COMPONENTS_JSON_RE.search(html)
        assert match, "no Bokeh docs_json in response"
        raw = match.group(1).replace("\\'", "'").replace("\\\\", "\\")
    docs = json.loads(raw)
    return next(iter(docs.values()))


def legends_in_doc(doc):
    return [ref for ref in doc["roots"]["references"] if ref["type"] == "Legend"]


def legend_grid(doc):
    """[[label, ...], ...]: one list per legend row, in document order."""
    refs = {ref["id"]: ref for ref in doc["roots"]["references"]}
    plot = refs[doc["roots"]["root_ids"][0]]
    rows = []
    for below in plot["attributes"].get("below", []):
        ref = refs[below["id"]]
        if ref["type"] != "Legend":
            continue
        rows.append([refs[item["id"]]["attributes"]["label"]["value"]
                     for item in ref["attributes"]["items"]])
    return rows


class LegendLayoutRuleTests(TestCase):
    """Pure arithmetic of YSE_App/common/legend_layout.py."""

    def test_short_labels_pack_into_many_columns(self):
        # 'PS1 g' (5 chars): entry = 20 + 5 + 35 + 6 = 66 px; usable 400 - 30 - 8 = 362 -> 5
        self.assertEqual(legend_column_count(["PS1 g"] * 19, 400), 5)

    def test_medium_labels_fewer_columns(self):
        # 12 chars: entry = 20 + 5 + 84 + 6 = 115 px -> 362 // 115 = 3 columns at 400 px,
        # 4 at 500 px and 6 (the cap) at 800 px
        self.assertEqual(legend_column_count(["ATLAS orange"] * 19, 400), 3)
        self.assertEqual(legend_column_count(["ATLAS orange"] * 19, 500), 4)
        self.assertEqual(legend_column_count(["ATLAS orange"] * 19, 800), 6)
        # 13 chars ('today (61312)'): entry 122 px -> 2 columns at 400 px
        self.assertEqual(legend_column_count(["today (61312)"] * 19, 400), 2)

    def test_long_label_forces_single_column(self):
        self.assertEqual(
            legend_column_count(["Very Long Telescope Name Halpha-narrow", "PS1 g"], 400), 1
        )

    def test_column_count_never_exceeds_items_or_cap(self):
        self.assertEqual(legend_column_count(["g", "r", "i"], 400), 3)
        self.assertEqual(legend_column_count(["g"] * 40, 4000), MAX_COLUMNS)
        self.assertEqual(legend_column_count([], 400), 1)
        self.assertEqual(legend_column_count(["PS1 g"], 10), 1)

    def test_wider_plot_gets_more_columns(self):
        self.assertGreater(
            legend_column_count(["ATLAS orange"] * 19, 800),
            legend_column_count(["ATLAS orange"] * 19, 400),
        )

    def test_rows_are_row_major_and_last_row_may_be_short(self):
        self.assertEqual(legend_rows(list(range(7)), 3), [[0, 1, 2], [3, 4, 5], [6]])
        self.assertEqual(legend_row_count(7, 3), 3)
        self.assertEqual(legend_row_count(0, 3), 0)
        self.assertEqual(legend_height_px(7, 3), 3 * ROW_HEIGHT_PX)

    def test_label_width_is_longest_label(self):
        self.assertEqual(legend_label_width_px(["ab", "abcd"]), 4 * 7)

    def test_requested_width_is_clamped_and_bucketed(self):
        self.assertEqual(requested_plot_width(None, 400), 400)
        self.assertEqual(requested_plot_width("abc", 400), 400)
        self.assertEqual(requested_plot_width("", 500), 500)
        self.assertEqual(requested_plot_width("657", 400), 640)
        self.assertEqual(requested_plot_width("657.4", 400), 640)
        self.assertEqual(requested_plot_width("12", 400), 320)
        self.assertEqual(requested_plot_width("99999", 400), 1600)


class LegendOrderingAndColourTests(TestCase):
    """#91: the same band always gets the same label, position and colour."""

    def test_instrument_family_wins_over_unmatched_telescope_name(self):
        self.assertEqual(telescope_display_name("ZTF-Cam", "Palomar 48"), "ZTF")
        self.assertEqual(telescope_display_name("GPC1", "Pan-STARRS1"), "PS1")
        self.assertEqual(telescope_display_name("Thacher-Cam", "Thacher"), "Thacher")
        self.assertEqual(telescope_display_name("UVOT", "Swift"), "Swift")
        self.assertEqual(telescope_display_name("LSSTCam", "Simonyi Survey Telescope"), "LSST")
        self.assertEqual(telescope_display_name("LSSTCam", None), "LSST")

    def test_sort_key_orders_by_telescope_then_wavelength(self):
        labels = [
            ("r-ZTF", "ZTF-Cam", "Palomar 48"),
            ("g", "GPC1", "Pan-STARRS1"),
            ("orange-ATLAS", "ACAM1", "ATLAS"),
            ("w", "GPC1", "Pan-STARRS1"),
            ("g-ZTF", "ZTF-Cam", "Palomar 48"),
            ("cyan-ATLAS", "ACAM1", "ATLAS"),
            ("z", "GPC1", "Pan-STARRS1"),
        ]
        ordered = sorted(
            labels,
            key=lambda item: legend_sort_key(item[0], instrument_name=item[1], telescope_name=item[2]),
        )
        self.assertEqual(
            [band for band, _i, _t in ordered],
            ["cyan-ATLAS", "orange-ATLAS", "g", "z", "w", "g-ZTF", "r-ZTF"],
        )

    def test_lsst_bands_share_the_family_colours(self):
        for filt in "ugrizy":
            self.assertEqual(band_display_color(f"{filt}-LSST"), FILTER_COLORS[filt], filt)
            self.assertEqual(band_display_color(filt, None), FILTER_COLORS[filt], filt)
            self.assertEqual(band_display_color(f"lsst{filt}"), FILTER_COLORS[filt], filt)

    def test_lsstcam_series_sort_by_wavelength(self):
        ordered = sorted(
            "yzirgu",
            key=lambda band: legend_sort_key(band, instrument_name="LSSTCam", telescope_name="Simonyi Survey Telescope"),
        )
        self.assertEqual("".join(ordered), "ugrizy")

    def test_unknown_filter_colour_does_not_depend_on_plot_order(self):
        self.assertEqual(band_display_color("J", fallback_index=0), band_display_color("J", fallback_index=5))
        self.assertNotEqual(band_display_color("J"), band_display_color("Halpha-narrow"))


class LightcurveLegendRenderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user("lc_legend_user", is_staff=True)
        cls.transient = seed_many_band_transient(cls.user)
        # Same bands, only a subset with data, different creation order.
        cls.subset = seed_many_band_transient(
            cls.user, name="lc-legend-subset",
            series=(
                ("ATLAS", "ACAM1", ["orange-ATLAS"]),
                ("Palomar 48", "ZTF-Cam", ["r-ZTF", "g-ZTF"]),
                ("Pan-STARRS1", "GPC1", ["z", "g"]),
            ),
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _doc(self, view_name, transient=None, width=None):
        transient = transient or self.transient
        url = reverse(view_name, args=[transient.id])
        if width is not None:
            url += f"?w={width}"
        with mock.patch.dict(os.environ, {"YSE_PLOT_HTML_CACHE": "0"}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return bokeh_doc_from_html(response.content.decode())

    def _plot_attrs(self, doc):
        refs = {ref["id"]: ref for ref in doc["roots"]["references"]}
        return refs[doc["roots"]["root_ids"][0]]["attributes"]

    def test_detail_legend_is_a_grid_of_horizontal_rows(self):
        grid = legend_grid(self._doc("lightcurveplot_detail"))
        labels = [label for row in grid for label in row]
        # 16 series + today
        self.assertEqual(len(labels), 17)
        # longest label 'today (NNNNN)' = 13 chars -> the 400 px default fits 2 columns
        expected_cols = legend_column_count(labels, 400)
        self.assertEqual(expected_cols, 2)
        self.assertEqual(len(grid[0]), expected_cols)
        self.assertEqual(len(grid), legend_row_count(len(labels), expected_cols))
        self.assertTrue(labels[-1].startswith("today ("))
        doc = self._doc("lightcurveplot_detail")
        for legend in legends_in_doc(doc):
            attrs = legend["attributes"]
            self.assertEqual(attrs["orientation"], "horizontal")
            self.assertEqual(attrs["click_policy"], "hide")
            self.assertEqual(attrs["label_width"], legend_label_width_px(labels))

    def test_page_width_query_sets_columns_and_plot_width(self):
        for width, expected_cols, expected_width in ((657, 4, 640), (1000, 6, 1000), ("junk", 2, 400)):
            with self.subTest(width=width):
                doc = self._doc("lightcurveplot_detail", width=width)
                grid = legend_grid(doc)
                self.assertEqual(len(grid[0]), expected_cols)
                self.assertEqual(self._plot_attrs(doc)["width"], expected_width)
                self.assertEqual(len(grid), legend_row_count(17, expected_cols))

    def test_width_query_is_part_of_the_plot_cache_key(self):
        url = reverse("lightcurveplot_detail", args=[self.transient.id])
        with mock.patch.dict(os.environ, {"YSE_PLOT_HTML_CACHE": "1"}):
            narrow = self.client.get(url + "?w=400").content
            wide = self.client.get(url + "?w=1000").content
            narrow_again = self.client.get(url + "?w=410").content
        self.assertNotEqual(narrow, wide)
        self.assertEqual(narrow, narrow_again)

    def test_detail_legend_order_is_telescope_then_wavelength(self):
        labels = [label for row in legend_grid(self._doc("lightcurveplot_detail")) for label in row]
        self.assertEqual(labels[:-1], [
            "ATLAS g", "ATLAS r",
            "PS1 g", "PS1 r", "PS1 i", "PS1 z", "PS1 y", "PS1 w",
            "Swope u", "Swope B", "Swope V",
            "Thacher H", "Thacher J",
            "ZTF g", "ZTF r", "ZTF i",
        ])

    def test_subset_transient_keeps_relative_order_and_colours(self):
        full = self._doc("lightcurveplot_detail")
        subset = self._doc("lightcurveplot_detail", self.subset)
        full_labels = [l for row in legend_grid(full) for l in row][:-1]
        subset_labels = [l for row in legend_grid(subset) for l in row][:-1]
        self.assertEqual(subset_labels, ["ATLAS r", "PS1 g", "PS1 z", "ZTF g", "ZTF r"])
        self.assertEqual(
            subset_labels, [label for label in full_labels if label in subset_labels]
        )
        self.assertEqual(self._series_colours(full)["PS1 g"], FILTER_COLORS["g"])
        for label, colour in self._series_colours(subset).items():
            self.assertEqual(colour, self._series_colours(full)[label], label)

    def _series_colours(self, doc):
        refs = {ref["id"]: ref for ref in doc["roots"]["references"]}
        colours = {}
        for legend in legends_in_doc(doc):
            for item in legend["attributes"]["items"]:
                item_ref = refs[item["id"]]
                if item_ref["attributes"]["label"]["value"].startswith("today"):
                    continue  # default (black) line colour is not serialised
                renderer = refs[item_ref["attributes"]["renderers"][0]["id"]]
                glyph = refs[renderer["attributes"]["glyph"]["id"]]["attributes"]
                colour = next(
                    spec["value"] for spec in (glyph.get("fill_color"), glyph.get("line_color"))
                    if isinstance(spec, dict) and spec.get("value")
                )
                colours[item_ref["attributes"]["label"]["value"]] = colour
        return colours

    def test_detail_plot_height_grows_per_legend_row_not_per_band(self):
        doc = self._doc("lightcurveplot_detail")
        plot = self._plot_attrs(doc)
        rows = len(legend_grid(doc))
        # bokeh 2.4 serialises plot_height as "height"
        self.assertEqual(plot["height"], 400 + ROW_HEIGHT_PX * rows)
        self.assertLess(plot["height"], 400 + 20 * 17)

    def test_legend_click_also_hides_error_bars_and_upper_limits(self):
        doc = self._doc("lightcurveplot_detail")
        refs = {ref["id"]: ref for ref in doc["roots"]["references"]}
        callbacks = [ref for ref in refs.values() if ref["type"] == "CustomJS"]
        self.assertGreaterEqual(len(callbacks), 16)
        followers = sum(len(cb["attributes"]["args"]["followers"]) for cb in callbacks)
        # every series links its error bars; each also has one upper limit
        self.assertEqual(followers, 16 * 2)
        for cb in callbacks:
            self.assertIn("r.visible = cb_obj.visible", cb["attributes"]["code"])
        # markers are Scatter glyphs in bokeh 2.4 (one renderer, several glyph
        # copies); upper limits are inverted triangles drawn at the detection
        # size (7 px) instead of 5 px
        ulims = []
        for ref in refs.values():
            if ref["type"] != "GlyphRenderer":
                continue
            glyph = refs[ref["attributes"]["glyph"]["id"]]
            if glyph["type"] == "Scatter" and glyph["attributes"].get("marker", {}).get("value") == "inverted_triangle":
                ulims.append(glyph)
        self.assertEqual(len(ulims), 16)
        self.assertTrue(all(glyph["attributes"]["size"]["value"] == 7 for glyph in ulims))

    def test_flux_and_summary_plots_use_the_grid_too(self):
        for view in ("lightcurveplot_flux", "lightcurveplot_summary"):
            with self.subTest(view=view):
                grid = legend_grid(self._doc(view))
                self.assertGreater(len(grid), 1, view)
                labels = [label for row in grid for label in row]
                self.assertEqual(labels[:2], ["ATLAS g", "ATLAS r"])
                self.assertEqual(len(grid[0]), legend_column_count(labels, 500 if "summary" in view else 400))
                self.assertEqual(len(legend_grid(self._doc(view, width=900))[0]), MAX_COLUMNS)

    def test_detail_page_renders_with_plot_in_both_defer_modes(self):
        for defer in ("1", "0"):
            with self.subTest(defer=defer), mock.patch.dict(os.environ, {"YSE_TRANSIENT_DETAIL_DEFER": defer}):
                response = self.client.get(reverse("transient_detail", kwargs={"slug": self.transient.slug}))
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                self.assertIn('id="lcplot"', html)
                self.assertIn(reverse("lightcurveplot_detail", args=[self.transient.id]), html)
                # the page sends its container width so the legend columns fit it
                self.assertIn('ysePlotWidthQuery("#lcplot")', html)
                self.assertIn('ysePlotWidthQuery("#lcfluxplot")', html)
                self.assertIn("window.ysePlotWidthQuery = function", html)
