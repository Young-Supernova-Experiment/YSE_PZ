"""Light-curve legend layout, ordering and colours (#226, #91, #249, #250, #251, #372).

The legend below each Bokeh light curve has one column per instrument that
reads down (PS1/2, DECam, Swope, LSST, ZTF, ATLAS, Swift, then the rest),
bands bluest to reddest within a column, columns packed side by side by
their own width and wrapping to a new block only when the next one would
not fit the plot, and clicking a legend entry hides the whole series.  These tests build a transient with many instrument+band series of
varying label length and inspect the Bokeh document the views embed.
"""

import datetime
import json
import os
import re
from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from YSE_App.common.band_order import legend_sort_key
from YSE_App.common.filter_display import (
    FILTER_COLORS,
    band_display_color,
    telescope_display_name,
)
from YSE_App.common.legend_layout import (
    BLOCK_GAP,
    COLUMN_GAP,
    GLYPH_HEIGHT,
    legend_block_height_px,
    legend_blocks_height_px,
    legend_column_blocks,
    legend_column_height_px,
    legend_column_margin,
    legend_column_width_px,
    legend_label_width_px,
    legend_usable_width_px,
    label_width_px,
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
# Every instrument group the legend orders explicitly, plus one "other".
SERIES_ALL_GROUPS = (
    ("Swift", "UVOT", ["V", "B", "U", "UVW1", "UVM2", "UVW2"]),
    ("Thacher", "Thacher-Cam", ["H", "J"]),
    ("Blanco", "DECam", ["z-DECam", "g-DECam", "r-DECam", "i-DECam"]),
    ("Simonyi Survey Telescope", "LSSTCam", ["y-LSST", "u-LSST", "g-LSST"]),
) + SERIES

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


def legend_blocks(doc):
    """Blocks of column legends below the plot, in document order.

    ``[{"columns": [{"labels": [...], "x": px, "attrs": {...}}, ...], "height": px}, ...]``:
    a block is the vertical column legends up to the empty spacer legend
    whose panel (``2 * margin``) reserves the block's height.
    """
    refs = {ref["id"]: ref for ref in doc["roots"]["references"]}
    plot = refs[doc["roots"]["root_ids"][0]]
    blocks = []
    columns = []
    for below in plot["attributes"].get("below", []):
        ref = refs[below["id"]]
        if ref["type"] != "Legend":
            continue
        attrs = ref["attributes"]
        items = attrs.get("items", [])
        if not items:
            blocks.append({"columns": columns, "height": 2 * attrs["margin"]})
            columns = []
            continue
        columns.append({
            "labels": [refs[item["id"]]["attributes"]["label"]["value"] for item in items],
            "x": attrs["location"][0],
            "attrs": attrs,
        })
    assert not columns, "column legends after the last block spacer"
    return blocks


def legend_columns(blocks):
    """Label lists of every column, blocks in order, left to right within a block."""
    return [column["labels"] for block in blocks for column in block["columns"]]


def legend_series(blocks):
    """Series in reading order: down each column, columns left to right, block by block."""
    return [label for column in legend_columns(blocks) for label in column]


def first_row(blocks):
    """Top label of every column in the first block."""
    return [column["labels"][0] for column in blocks[0]["columns"]]


# The 2022abom-like legend (#372): every instrument group plus Thacher and the
# today marker, longest label per column as the render tests produce them.
ABOM_COLUMNS = [
    ["PS1 g", "PS1 w", "PS1 r", "PS1 i", "PS1 z", "PS1 y"],
    ["DECam g", "DECam r", "DECam i", "DECam z"],
    ["Swope u", "Swope B", "Swope V"],
    ["LSST u", "LSST g", "LSST y"],
    ["ZTF g", "ZTF r", "ZTF i"],
    ["ATLAS g", "ATLAS r"],
    ["Swift UVW2", "Swift UVM2", "Swift UVW1", "Swift u", "Swift B", "Swift V"],
    ["Thacher J", "Thacher H"],
    ["today (61312)"],
]


class LegendLayoutRuleTests(TestCase):
    """Pure arithmetic of YSE_App/common/legend_layout.py."""

    def test_column_width_is_marker_standoff_and_its_own_widest_label(self):
        # 20 px marker + 5 px standoff + the widest label, summed from the 13 px
        # Helvetica glyph widths (capitals ~9 px, lowercase ~7, i/l ~3) and rounded up
        self.assertEqual(label_width_px("PS1 w"), 39)
        self.assertEqual(label_width_px("DECam g"), 57)
        self.assertEqual(label_width_px("Swift UVW2"), 71)
        self.assertEqual(legend_column_width_px(["PS1 g", "PS1 w"]), 64)
        self.assertEqual(legend_column_width_px(["ATLAS cyan", "ATLAS orange"]), 112)
        self.assertEqual(legend_column_width_px(["today (61312)"]), 107)
        self.assertEqual(legend_label_width_px(["ab", "abcd"]), 29)
        self.assertEqual(legend_label_width_px([]), 0)

    def test_usable_width_excludes_axis_and_toolbar(self):
        self.assertEqual(legend_usable_width_px(880), 796)
        self.assertEqual(legend_usable_width_px(400), 316)
        self.assertEqual(legend_usable_width_px(10), 1)

    def test_abom_like_legend_fits_one_row_at_900_px(self):
        # #372: 9 columns of their own widths (64 + 82 + 78 + 69 + 60 + 78 + 96 + 87 + 107
        # plus 8 gaps of 8 = 785 px) fit the 796 px usable at ?w=900 (bucketed to 880),
        # so Swift sits right of ATLAS instead of under PS1
        self.assertEqual([legend_column_width_px(column) for column in ABOM_COLUMNS], [64, 82, 78, 69, 60, 78, 96, 87, 107])
        blocks = legend_column_blocks(ABOM_COLUMNS, requested_plot_width("900", 400))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(
            blocks[0],
            [(0, 0), (1, 72), (2, 162), (3, 248), (4, 325), (5, 393), (6, 479), (7, 583), (8, 678)],
        )
        last_index, last_x = blocks[0][-1]
        self.assertLessEqual(last_x + legend_column_width_px(ABOM_COLUMNS[last_index]), 796)

    def test_columns_wrap_only_when_the_next_one_would_not_fit(self):
        # 400 px (316 usable): PS1, DECam, Swope end at 240, LSST would end at 317 -> new
        # block; LSST, ZTF, ATLAS end at 223, Swift would end at 319 -> new block
        blocks = legend_column_blocks(ABOM_COLUMNS, 400)
        self.assertEqual(
            blocks,
            [[(0, 0), (1, 72), (2, 162)], [(3, 0), (4, 77), (5, 145)], [(6, 0), (7, 104), (8, 199)]],
        )
        for block in blocks:
            for (index, x), (_next_index, next_x) in zip(block, block[1:]):
                self.assertEqual(next_x, x + legend_column_width_px(ABOM_COLUMNS[index]) + COLUMN_GAP)

    def test_wider_plot_never_needs_more_blocks(self):
        widths = (320, 400, 480, 640, 880, 1000, 1600)
        counts = [len(legend_column_blocks(ABOM_COLUMNS, width)) for width in widths]
        self.assertEqual(counts, sorted(counts, reverse=True))
        self.assertEqual(counts[0], 4)
        self.assertEqual(counts[-1], 1)

    def test_long_column_gets_a_block_of_its_own(self):
        columns = [["Very Long Telescope Name Halpha-narrow"], ["PS1 g"], ["today (61312)"]]
        self.assertEqual(legend_column_blocks(columns, 320), [[(0, 0)], [(1, 0), (2, 69)]])

    def test_empty_columns_are_skipped(self):
        self.assertEqual(legend_column_blocks([[], ["PS1 g"], [], ["ZTF g"]], 400), [[(1, 0), (3, 69)]])
        self.assertEqual(legend_column_blocks([], 400), [])
        self.assertEqual(legend_column_blocks([[]], 400), [])

    def test_column_and_block_heights_follow_bokeh_row_height(self):
        self.assertEqual(legend_column_height_px(3), 3 * GLYPH_HEIGHT)
        self.assertEqual(legend_column_margin(3), -(3 * GLYPH_HEIGHT) // 2)
        self.assertEqual(legend_column_margin(0), 0)
        self.assertEqual(legend_block_height_px(6), 6 * GLYPH_HEIGHT + BLOCK_GAP)
        self.assertEqual(legend_block_height_px(6) % 2, 0)
        self.assertEqual(legend_blocks_height_px([6, 2]), legend_block_height_px(6) + legend_block_height_px(2))
        self.assertEqual(legend_blocks_height_px([]), 0)

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

    def test_sort_key_orders_by_instrument_group_then_wavelength(self):
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
        # PS1/2 before ZTF before ATLAS; w (6080 A) sits between g and z
        self.assertEqual(
            [band for band, _i, _t in ordered],
            ["g", "w", "z", "g-ZTF", "r-ZTF", "cyan-ATLAS", "orange-ATLAS"],
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
        cls.all_groups = seed_many_band_transient(cls.user, name="lc-legend-groups", series=SERIES_ALL_GROUPS)
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

    def test_detail_legend_reads_down_one_column_per_instrument(self):
        doc = self._doc("lightcurveplot_detail")
        blocks = legend_blocks(doc)
        labels = legend_series(blocks)
        # 16 series + today
        self.assertEqual(len(labels), 17)
        # 400 px (316 usable): PS1 (64), Swope (78), ZTF (60) and ATLAS (78) end at 304 px;
        # Thacher (87) would not fit and starts the second block with today (107)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(
            [[column["labels"] for column in block["columns"]] for block in blocks],
            [
                [
                    ["PS1 g", "PS1 w", "PS1 r", "PS1 i", "PS1 z", "PS1 y"],
                    ["Swope u", "Swope B", "Swope V"],
                    ["ZTF g", "ZTF r", "ZTF i"],
                    ["ATLAS g", "ATLAS r"],
                ],
                [["Thacher J", "Thacher H"], [labels[-1]]],
            ],
        )
        self.assertTrue(labels[-1].startswith("today ("))
        self.assertEqual([column["x"] for column in blocks[0]["columns"]], [0, 72, 158, 226])
        self.assertEqual([column["x"] for column in blocks[1]["columns"]], [0, 95])
        self.assertEqual([block["height"] for block in blocks], [6 * GLYPH_HEIGHT + BLOCK_GAP, 2 * GLYPH_HEIGHT + BLOCK_GAP])
        refs = {ref["id"]: ref for ref in doc["roots"]["references"]}
        for block in blocks:
            for column in block["columns"]:
                attrs = column["attrs"]
                n = len(column["labels"])
                # bokeh serialises only non-default attributes: vertical is the default
                self.assertEqual(attrs.get("orientation", "vertical"), "vertical")
                self.assertEqual(attrs["click_policy"], "hide")
                self.assertEqual(attrs["label_width"], legend_label_width_px(column["labels"]))
                # the panel collapses to zero height and the legend hangs from its top edge
                self.assertEqual(attrs["margin"], -(n * GLYPH_HEIGHT) // 2)
                self.assertEqual(attrs["location"], [column["x"], 2 * attrs["margin"]])
                self.assertIsNone(attrs["border_line_color"])
                for item in attrs["items"]:
                    self.assertEqual(len(refs[item["id"]]["attributes"]["renderers"]), 1)

    def test_page_width_query_sets_columns_and_plot_width(self):
        # 520 px fits 5 of the 6 columns (today wraps); 657 (bucketed to 640) and
        # 1000 px fit all 6 side by side; a bad ?w= falls back to 400 (4 + 2).
        for width, expected_blocks, expected_width in (
            (520, [5, 1], 520), (657, [6], 640), (1000, [6], 1000), ("junk", [4, 2], 400),
        ):
            with self.subTest(width=width):
                doc = self._doc("lightcurveplot_detail", width=width)
                blocks = legend_blocks(doc)
                self.assertEqual([len(block["columns"]) for block in blocks], expected_blocks)
                self.assertEqual(self._plot_attrs(doc)["width"], expected_width)
                labels = [column["labels"] for column in blocks[0]["columns"]]
                self.assertEqual(
                    [column["x"] for column in blocks[0]["columns"]],
                    [x for _index, x in legend_column_blocks(labels, expected_width)[0]],
                )

    def test_width_query_is_part_of_the_plot_cache_key(self):
        url = reverse("lightcurveplot_detail", args=[self.transient.id])
        with mock.patch.dict(os.environ, {"YSE_PLOT_HTML_CACHE": "1"}):
            narrow = self.client.get(url + "?w=400").content
            wide = self.client.get(url + "?w=1000").content
            narrow_again = self.client.get(url + "?w=410").content
        self.assertNotEqual(narrow, wide)
        self.assertEqual(narrow, narrow_again)

    def test_detail_legend_order_is_instrument_group_then_wavelength(self):
        columns = legend_columns(legend_blocks(self._doc("lightcurveplot_detail", width=1000)))
        self.assertEqual(columns[:-1], [
            ["PS1 g", "PS1 w", "PS1 r", "PS1 i", "PS1 z", "PS1 y"],
            ["Swope u", "Swope B", "Swope V"],
            ["ZTF g", "ZTF r", "ZTF i"],
            ["ATLAS g", "ATLAS r"],
            ["Thacher J", "Thacher H"],
        ])
        self.assertEqual(len(columns[-1]), 1)
        self.assertTrue(columns[-1][0].startswith("today ("))

    def test_all_instrument_groups_follow_the_requested_order(self):
        # 1600 px fits every column: PS1/2, DECam, Swope, LSST, ZTF, ATLAS, Swift, other, markers
        blocks = legend_blocks(self._doc("lightcurveplot_detail", self.all_groups, width=1600))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(
            first_row(blocks)[:-1],
            ["PS1 g", "DECam g", "Swope u", "LSST u", "ZTF g", "ATLAS g", "Swift UVW2", "Thacher J"],
        )
        columns = legend_columns(blocks)
        self.assertEqual(columns[1], ["DECam g", "DECam r", "DECam i", "DECam z"])
        self.assertEqual(columns[3], ["LSST u", "LSST g", "LSST y"])
        self.assertEqual(columns[6], ["Swift UVW2", "Swift UVM2", "Swift UVW1", "Swift u", "Swift B", "Swift V"])
        # the same order in the flux and summary plots
        for view in ("lightcurveplot_flux", "lightcurveplot_summary"):
            with self.subTest(view=view):
                series = legend_series(legend_blocks(self._doc(view, self.all_groups, width=1600)))
                self.assertEqual(series[:8], ["PS1 g", "PS1 w", "PS1 r", "PS1 i", "PS1 z", "PS1 y", "DECam g", "DECam r"])
                self.assertEqual(series[13:16], ["LSST u", "LSST g", "LSST y"])

    def test_swift_stays_on_the_first_row_while_there_is_room(self):
        # #372: on the 900 px plot of the review screenshot every column, Swift and
        # Thacher included, packs onto the first row right of ATLAS
        doc = self._doc("lightcurveplot_detail", self.all_groups, width=900)
        self.assertEqual(self._plot_attrs(doc)["width"], 880)
        blocks = legend_blocks(doc)
        self.assertEqual(len(blocks), 1)
        row = first_row(blocks)
        self.assertEqual(row[5:8], ["ATLAS g", "Swift UVW2", "Thacher J"])
        self.assertTrue(row[8].startswith("today ("))
        columns = blocks[0]["columns"]
        self.assertEqual([column["x"] for column in columns], [0, 72, 162, 248, 325, 393, 479, 583, 678])
        right_edge = columns[-1]["x"] + legend_column_width_px(columns[-1]["labels"])
        self.assertLessEqual(right_edge, legend_usable_width_px(880))
        self.assertEqual(self._plot_attrs(doc)["height"], 400 + legend_block_height_px(6))

    def test_narrow_plot_still_wraps(self):
        # 400 px: PS1, DECam and Swope fill the first block; Swift lands in the third
        blocks = legend_blocks(self._doc("lightcurveplot_detail", self.all_groups, width=400))
        self.assertEqual([len(block["columns"]) for block in blocks], [3, 3, 3])
        self.assertEqual(first_row(blocks), ["PS1 g", "DECam g", "Swope u"])
        self.assertEqual([column["labels"][0] for column in blocks[1]["columns"]], ["LSST u", "ZTF g", "ATLAS g"])
        self.assertEqual([column["labels"][0] for column in blocks[2]["columns"]][:2], ["Swift UVW2", "Thacher J"])
        # every block is as deep as its deepest column: PS1 (6), LSST / ZTF (3), Swift (6)
        self.assertEqual(
            [block["height"] for block in blocks],
            [legend_block_height_px(6), legend_block_height_px(3), legend_block_height_px(6)],
        )

    def test_subset_transient_keeps_relative_order_and_colours(self):
        full = self._doc("lightcurveplot_detail", width=1000)
        subset = self._doc("lightcurveplot_detail", self.subset, width=1000)
        full_labels = legend_series(legend_blocks(full))[:-1]
        subset_labels = legend_series(legend_blocks(subset))[:-1]
        self.assertEqual(subset_labels, ["PS1 g", "PS1 z", "ZTF g", "ZTF r", "ATLAS r"])
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
            for item in legend["attributes"].get("items", []):  # block spacers have none
                item_ref = refs[item["id"]]
                label = item_ref["attributes"]["label"]["value"]
                if label == "" or label.startswith("today"):
                    continue  # blank cell / default (black) line colour is not serialised
                renderer = refs[item_ref["attributes"]["renderers"][0]["id"]]
                glyph = refs[renderer["attributes"]["glyph"]["id"]]["attributes"]
                colour = next(
                    spec["value"] for spec in (glyph.get("fill_color"), glyph.get("line_color"))
                    if isinstance(spec, dict) and spec.get("value")
                )
                colours[item_ref["attributes"]["label"]["value"]] = colour
        return colours

    def test_detail_plot_height_grows_per_legend_block_not_per_band(self):
        doc = self._doc("lightcurveplot_detail")
        plot = self._plot_attrs(doc)
        blocks = legend_blocks(doc)
        depths = [max(len(column["labels"]) for column in block["columns"]) for block in blocks]
        self.assertEqual(depths, [6, 2])
        # bokeh 2.4 serialises plot_height as "height"
        self.assertEqual(plot["height"], 400 + legend_blocks_height_px(depths))
        self.assertEqual(plot["height"], 400 + sum(block["height"] for block in blocks))
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
        # flux: 5 instruments + today at 400 px -> Thacher and today wrap (4 + 2);
        # summary: no today entry, 500 px fits all 5 instruments in one block.
        for view, first_labels, expected_blocks, n_columns, base_height in (
            ("lightcurveplot_flux", ["PS1 g", "Swope u", "ZTF g", "ATLAS g"], [4, 2], 6, 200),
            ("lightcurveplot_summary", ["PS1 g", "Swope u", "ZTF g", "ATLAS g", "Thacher J"], [5], 5, None),
        ):
            with self.subTest(view=view):
                doc = self._doc(view)
                blocks = legend_blocks(doc)
                self.assertEqual([len(block["columns"]) for block in blocks], expected_blocks)
                self.assertEqual(first_row(blocks), first_labels)
                if base_height is not None:
                    self.assertEqual(
                        self._plot_attrs(doc)["height"],
                        base_height + sum(block["height"] for block in blocks),
                    )
                # wide enough: one block, one column per instrument, never more
                wide = legend_blocks(self._doc(view, width=900))
                self.assertEqual([len(block["columns"]) for block in wide], [n_columns])

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
