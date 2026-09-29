"""Legend order of light-curve series: instrument group, then wavelength (YSE_App/common/band_order.py).

PS1/2, DECam, Swope, LSST, ZTF, ATLAS, Swift come first in that order, every
other instrument follows alphabetically, and within an instrument the bands
run bluest to reddest with unknown bands last.  The keys depend on names
only, so they are stable across transients.
"""

import random

from django.test import SimpleTestCase

from YSE_App.common.band_order import (
    INSTRUMENT_GROUP_ORDER,
    OTHER_GROUP,
    band_sort_key,
    band_wavelength,
    group_consecutive,
    instrument_group,
    instrument_group_rank,
    legend_column_key,
    legend_sort_key,
)
from YSE_App.common.filter_display import telescope_display_name
from YSE_App.common.legend_layout import ROW_HEIGHT_PX, legend_column_grid, legend_grid_height_px

# (band, instrument, telescope) in the expected legend order.
ORDERED_SERIES = [
    ("g", "GPC1", "Pan-STARRS1"),
    ("w", "GPC1", "Pan-STARRS1"),
    ("r", "GPC1", "Pan-STARRS1"),
    ("i", "GPC1", "Pan-STARRS1"),
    ("z", "GPC1", "Pan-STARRS1"),
    ("y", "GPC1", "Pan-STARRS1"),
    ("g", "GPC2", "Pan-STARRS2"),
    ("r", "GPC2", "Pan-STARRS2"),
    ("g-DECam", "DECam", "Blanco"),
    ("r-DECam", "DECam", "Blanco"),
    ("i-DECam", "DECam", "Blanco"),
    ("z-DECam", "DECam", "Blanco"),
    ("u", "Direct/4Kx4K", "Swope"),
    ("B", "Direct/4Kx4K", "Swope"),
    ("g", "Direct/4Kx4K", "Swope"),
    ("V", "Direct/4Kx4K", "Swope"),
    ("r", "Direct/4Kx4K", "Swope"),
    ("i", "Direct/4Kx4K", "Swope"),
    ("u-LSST", "LSSTCam", "Simonyi Survey Telescope"),
    ("g-LSST", "LSSTCam", "Simonyi Survey Telescope"),
    ("r-LSST", "LSSTCam", "Simonyi Survey Telescope"),
    ("i-LSST", "LSSTCam", "Simonyi Survey Telescope"),
    ("z-LSST", "LSSTCam", "Simonyi Survey Telescope"),
    ("y-LSST", "LSSTCam", "Simonyi Survey Telescope"),
    ("g-ZTF", "ZTF-Cam", "Palomar 48"),
    ("r-ZTF", "ZTF-Cam", "Palomar 48"),
    ("i-ZTF", "ZTF-Cam", "Palomar 48"),
    ("cyan-ATLAS", "ACAM1", "ATLAS"),
    ("orange-ATLAS", "ACAM1", "ATLAS"),
    ("UVW2", "UVOT", "Swift"),
    ("UVM2", "UVOT", "Swift"),
    ("UVW1", "UVOT", "Swift"),
    ("U", "UVOT", "Swift"),
    ("B", "UVOT", "Swift"),
    ("V", "UVOT", "Swift"),
    # other instruments: alphabetical by legend label
    ("g-Sloan", "Sinistro", "LCOGT 1m"),
    ("rp", "Sinistro", "LCOGT 1m"),
    ("J", "Thacher-Cam", "Thacher"),
    ("H", "Thacher-Cam", "Thacher"),
    ("Clear", "Thacher-Cam", "Thacher"),
    ("Other", "Thacher-Cam", "Thacher"),
]


def _key(item):
    band, inst, tel = item
    return legend_sort_key(band, instrument_name=inst, telescope_name=tel)


class InstrumentGroupTests(SimpleTestCase):
    def test_known_instruments_map_to_their_group(self):
        cases = {
            ("GPC1", "Pan-STARRS1"): "PS1/2",
            ("GPC2", "Pan-STARRS2"): "PS1/2",
            ("GPC2", None): "PS1/2",
            ("DECam", "Blanco"): "DECam",
            ("Direct/4Kx4K", "Swope"): "Swope",
            ("LSSTCam", "Simonyi Survey Telescope"): "LSST",
            ("LSSTCam", None): "LSST",
            ("ZTF-Cam", "Palomar 48"): "ZTF",
            ("ACAM1", "ATLAS"): "ATLAS",
            ("UVOT", "Swift"): "Swift",
        }
        for (inst, tel), group in cases.items():
            self.assertEqual(instrument_group(inst, tel), group, (inst, tel))

    def test_gpc2_gets_its_own_label_inside_the_ps_group(self):
        self.assertEqual(telescope_display_name("GPC2", "Pan-STARRS2"), "PS2")
        self.assertEqual(telescope_display_name("GPC1", "Pan-STARRS1"), "PS1")
        self.assertLess(
            legend_column_key("GPC1", "Pan-STARRS1"), legend_column_key("GPC2", "Pan-STARRS2")
        )
        self.assertEqual(legend_column_key("GPC1", None)[0], legend_column_key("GPC2", None)[0])

    def test_unknown_instruments_are_other_and_sort_after_known_groups(self):
        for inst, tel in (("Thacher-Cam", "Thacher"), ("Sinistro", "LCOGT 1m"), (None, None), ("", "")):
            self.assertEqual(instrument_group(inst, tel), OTHER_GROUP, (inst, tel))
        self.assertEqual(instrument_group_rank(OTHER_GROUP), len(INSTRUMENT_GROUP_ORDER))
        self.assertGreater(legend_column_key("Thacher-Cam", "Thacher"), legend_column_key("UVOT", "Swift"))
        # other instruments alphabetically by label
        self.assertLess(legend_column_key("Sinistro", "LCOGT 1m"), legend_column_key("Thacher-Cam", "Thacher"))

    def test_group_ranks_follow_the_requested_order(self):
        self.assertEqual(
            INSTRUMENT_GROUP_ORDER, ("PS1/2", "DECam", "Swope", "LSST", "ZTF", "ATLAS", "Swift")
        )
        ranks = [instrument_group_rank(g) for g in INSTRUMENT_GROUP_ORDER]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(instrument_group_rank("not a group"), len(INSTRUMENT_GROUP_ORDER))


class BandWavelengthOrderTests(SimpleTestCase):
    def test_swift_uvot_bands_run_blue_to_red(self):
        bands = ["V", "U", "UVW1", "B", "UVW2", "UVM2"]
        self.assertEqual(sorted(bands, key=band_sort_key), ["UVW2", "UVM2", "UVW1", "U", "B", "V"])

    def test_ps1_bands_use_effective_wavelength(self):
        # w (g+r+i, 6080 A) sits between g and r
        self.assertEqual(sorted("yzirwg", key=band_sort_key), list("gwrizy"))

    def test_lsst_and_decam_aliases_share_the_family_wavelengths(self):
        self.assertEqual(sorted(["z-LSST", "u-LSST", "y-LSST", "g-LSST", "i-LSST", "r-LSST"], key=band_sort_key),
                         ["u-LSST", "g-LSST", "r-LSST", "i-LSST", "z-LSST", "y-LSST"])
        self.assertEqual(sorted(["z-DECam", "g-DECam", "i-DECam", "r-DECam"], key=band_sort_key),
                         ["g-DECam", "r-DECam", "i-DECam", "z-DECam"])
        self.assertEqual(band_wavelength("g-DECam"), band_wavelength("g"))

    def test_unknown_bands_follow_known_ones_alphabetically(self):
        self.assertIsNone(band_wavelength("Clear"))
        self.assertIsNone(band_wavelength("Halpha"))
        self.assertIsNone(band_wavelength(None))
        self.assertIsNone(band_wavelength(""))
        self.assertEqual(
            sorted(["Other", "K", "Clear", "H", "Halpha", "J"], key=band_sort_key),
            ["J", "H", "K", "Clear", "Halpha", "Other"],
        )

    def test_gaia_g_is_a_broad_red_band_not_sdss_g(self):
        self.assertEqual(band_wavelength("G-Gaia"), 6730.0)
        self.assertLess(band_sort_key("r"), band_sort_key("G-Gaia"))
        self.assertLess(band_sort_key("G-Gaia"), band_sort_key("i"))

    def test_atlas_and_near_infrared(self):
        self.assertLess(band_sort_key("cyan-ATLAS"), band_sort_key("orange-ATLAS"))
        self.assertLess(band_sort_key("orange-ATLAS"), band_sort_key("J"))
        self.assertLess(band_sort_key("J"), band_sort_key("H"))


class LegendSortKeyTests(SimpleTestCase):
    def test_mixed_instruments_sort_into_the_documented_order(self):
        shuffled = list(ORDERED_SERIES)
        random.Random(7).shuffle(shuffled)
        self.assertEqual(sorted(shuffled, key=_key), ORDERED_SERIES)

    def test_order_is_stable_and_independent_of_the_other_series(self):
        subset = [ORDERED_SERIES[i] for i in (30, 3, 27, 12, 38, 8)]
        expected = [item for item in ORDERED_SERIES if item in subset]
        for seed in range(5):
            shuffled = list(subset)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(sorted(shuffled, key=_key), expected)
        # sorting twice changes nothing
        once = sorted(ORDERED_SERIES, key=_key)
        self.assertEqual(sorted(once, key=_key), once)

    def test_same_names_give_the_same_key(self):
        self.assertEqual(_key(("r-ZTF", "ZTF-Cam", "Palomar 48")), _key(("r-ZTF", "ZTF-Cam", "Palomar 48")))
        self.assertEqual(legend_sort_key("r-ZTF", instrument_name="ZTF-Cam"),
                         legend_sort_key("r-ZTF", instrument_name="ZTF-Cam", telescope_name="Palomar 48"))
        self.assertEqual(legend_sort_key(None)[:1], (len(INSTRUMENT_GROUP_ORDER),))

    def test_group_consecutive_keeps_runs_in_order(self):
        items = [("PS1", "g"), ("PS1", "r"), ("ZTF", "g"), ("ATLAS", "c"), ("ATLAS", "o")]
        self.assertEqual(
            group_consecutive(items, key=lambda item: item[0]),
            [("PS1", items[:2]), ("ZTF", items[2:3]), ("ATLAS", items[3:])],
        )
        self.assertEqual(group_consecutive([], key=lambda item: item), [])


class LegendColumnGridTests(SimpleTestCase):
    def test_columns_read_down_with_blank_cells(self):
        columns = [["PS1 g", "PS1 r", "PS1 i"], ["ZTF g", "ZTF r"], ["today"]]
        self.assertEqual(
            legend_column_grid(columns, 3),
            [["PS1 g", "ZTF g", "today"], ["PS1 r", "ZTF r", None], ["PS1 i", None, None]],
        )

    def test_extra_columns_wrap_to_a_new_block_of_rows(self):
        columns = [["a1", "a2"], ["b1"], ["c1", "c2", "c3"], ["d1"]]
        self.assertEqual(
            legend_column_grid(columns, 2, placeholder=""),
            [["a1", "b1"], ["a2", ""], ["c1", "d1"], ["c2", ""], ["c3", ""]],
        )
        self.assertEqual(legend_grid_height_px(5), 5 * ROW_HEIGHT_PX)

    def test_empty_columns_are_skipped_and_ncols_is_at_least_one(self):
        self.assertEqual(legend_column_grid([[], ["x"], []], 0), [["x"]])
        self.assertEqual(legend_column_grid([], 3), [])
        self.assertEqual(legend_grid_height_px(0), 0)
