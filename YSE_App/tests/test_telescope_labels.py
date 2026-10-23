"""Light-curve legend labels and symbols come from the series' own telescope (#788, #793).

Substring rules relabelled Nickel data (camera "Direct") as P200 and Lulin data (camera
"PIXIS") as LCO. Families now match whole words, the telescope name decides first, and only
cameras unique to one survey may set the family from the instrument name.
"""

from django.test import SimpleTestCase

from YSE_App.common.filter_display import (
    plot_legend_label,
    telescope_display_name,
    telescope_display_symbol,
    telescope_family,
)


class TelescopeLabelTests(SimpleTestCase):
    def test_reported_cases(self):
        self.assertEqual(telescope_display_name("Direct 2Kx2K", "Nickel"), "Nickel")
        self.assertEqual(plot_legend_label("B", instrument_name="Direct 2Kx2K", telescope_name="Nickel"), "Nickel B")
        self.assertEqual(telescope_display_name("PIXIS", "Lulin One-meter Telescope"), "Lulin One-meter Telescope")

    def test_generic_cameras_never_borrow_another_observatory(self):
        for instrument in ("Direct", "Direct 4Kx4K", "PIXIS", "PIXIS 1024", "ACP"):
            self.assertIsNone(telescope_family(instrument, "Some Observatory 1m"), instrument)
            self.assertEqual(telescope_display_symbol(instrument, "circle", "Some Observatory 1m"), "circle")
        # Whole words only: no family inside a longer word.
        self.assertIsNone(telescope_family("Falcon", "Telco"))

    def test_known_families(self):
        cases = {
            ("ZTF-Cam", "Palomar 48"): "ZTF",
            ("GPC1", "Pan-STARRS1"): "PS1",
            ("GPC2", "Pan-STARRS2"): "PS2",
            ("UVOT", "Swift"): "Swift",
            ("LSSTCam", None): "LSST",
            ("ACAM1", "ATLAS-HKO"): "ATLAS",
            ("Sinistro", "LCO 1m"): "LCO",
            ("Spectral", "LCOGT-2m"): "LCO",
            ("Direct", "P200"): "P200",
            ("DECam", "Blanco"): "DECam",
            (None, "1m-SWOPE"): "Swope",
        }
        for (instrument, telescope), label in cases.items():
            self.assertEqual(telescope_display_name(instrument, telescope), label, (instrument, telescope))

    def test_symbol_follows_the_family(self):
        self.assertEqual(telescope_display_symbol("Sinistro", None, "LCO 1m"), "dash")
        self.assertEqual(telescope_display_symbol("PIXIS", None, "Lulin"), "triangle")
        self.assertEqual(telescope_display_symbol("ZTF-Cam"), "diamond")
