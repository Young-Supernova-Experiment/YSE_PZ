"""PS1 cutout URL handling for finder charts (no network)."""
from unittest import mock

from django.test import SimpleTestCase

from YSE_App.util import mkFinderChart

PS1_PAGE = (
    '<table><tr><th>stack 2345.067 r<br><a href="//ps1images.stsci.edu/cgi-bin/fitscut.cgi?red=x">Display</a>'
    ' <a title="Fits cutout" href="//ps1images.stsci.edu/cgi-bin/fitscut.cgi?ra=150&amp;dec=2&amp;red=/rings.v3.skycell.2345.067.stk.r.unconv.fits">'
    'FITS</a></th></tr></table>'
)


class PS1FitsUrlTests(SimpleTestCase):
    def test_protocol_relative_link_becomes_https(self):
        self.assertEqual(mkFinderChart._ps1_fits_url("//ps1images.stsci.edu/cgi-bin/fitscut.cgi?a=1&amp;b=2"),
                         "https://ps1images.stsci.edu/cgi-bin/fitscut.cgi?a=1&b=2")

    def test_http_link_becomes_https(self):
        self.assertEqual(mkFinderChart._ps1_fits_url("http://ps1images.stsci.edu/x.fits"),
                         "https://ps1images.stsci.edu/x.fits")

    def test_scraped_page_downloads_valid_url(self):
        response = mock.Mock(content=PS1_PAGE.encode())
        with mock.patch("requests.get", return_value=response), \
                mock.patch.object(mkFinderChart.urllib.request, "urlretrieve") as retrieve, \
                mock.patch.object(mkFinderChart.os.path, "exists", return_value=True):
            out = mkFinderChart.panstamps_lite(150.0, 2.0, "r", 2, "/tmp/finder/x.png")
        url = retrieve.call_args[0][0]
        self.assertTrue(url.startswith("https://ps1images.stsci.edu/cgi-bin/fitscut.cgi?ra=150&dec=2"), url)
        self.assertTrue(out.endswith(".PS1.fits"))

    def test_download_error_returns_none_for_dss_fallback(self):
        response = mock.Mock(content=PS1_PAGE.encode())
        with mock.patch("requests.get", return_value=response), \
                mock.patch.object(mkFinderChart.urllib.request, "urlretrieve",
                                  side_effect=mkFinderChart.urllib.error.URLError("down")):
            self.assertIsNone(mkFinderChart.panstamps_lite(150.0, 2.0, "r", 2, "/tmp/finder/x.png"))

    def test_no_cutouts_returns_none(self):
        with mock.patch("requests.get", return_value=mock.Mock(content=b"<html></html>")):
            self.assertIsNone(mkFinderChart.panstamps_lite(150.0, 2.0, "r", 2, "/tmp/finder/x.png"))
