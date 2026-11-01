"""Band colours (#860): the UX-audit palette, Okabe-Ito spectra and upper-limit arrows.

Synthetic transients only.
"""

import datetime
import os
from unittest import mock

from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from YSE_App.common.filter_display import (
    FAMILY_SYMBOLS,
    FILTER_COLORS,
    SPECTRUM_COLORS,
    band_display_color,
    contrast_ratio,
    filter_color_groups_for_display,
    filter_color_key,
    telescope_display_symbol,
)
from YSE_App.models import TransientSpecData, TransientSpectrum
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_spectrum,
    audit_fields,
    create_minimal_transient,
    create_test_user,
)
from YSE_App.tests.test_lightcurve_legend import (
    bokeh_doc_from_html,
    legends_in_doc,
    seed_many_band_transient,
)

# The "Revised palette" of the UX audit (2026-10-25), as listed in #860.
AUDIT_PALETTE = {
    'UVW2': '#f61f80', 'UVM2': '#8158ca', 'UVW1': '#a07dce', 'u': '#643380', 'B': '#2d6dee',
    'V': '#576602', 'g': '#28763d', 'c': '#2890a1', 'r': '#d44e46', 'o': '#dd703d', 'i': '#b58214',
    'w': '#7b787d', 'z': '#000000', 'y': '#684121', 'J': '#223163', 'H': '#62475a', 'K': '#1e5845',
}


class PaletteTests(SimpleTestCase):
    def test_palette_is_the_audit_palette(self):
        self.assertEqual(FILTER_COLORS, AUDIT_PALETTE)

    def test_every_colour_is_at_least_3_to_1_on_white(self):
        # z is pure black (21:1) and passes trivially
        for key, colour in FILTER_COLORS.items():
            with self.subTest(band=key, colour=colour):
                self.assertGreaterEqual(contrast_ratio(colour, '#ffffff'), 3.0)

    def test_contrast_ratio_matches_wcag_reference_values(self):
        self.assertAlmostEqual(contrast_ratio('#000000'), 21.0, places=3)
        self.assertAlmostEqual(contrast_ratio('#ffffff'), 1.0, places=6)
        # WCAG's example: #767676 is just above 4.5:1 on white
        self.assertAlmostEqual(contrast_ratio('#767676'), 4.54, places=2)

    def test_colour_keys(self):
        for name, key in (
            ('UVW2', 'UVW2'), ('uvm2-uvot', 'UVM2'), ('UVW1', 'UVW1'), ('U', 'u'), ('up', 'u'),
            ('u-UVOT', 'u'), ('B', 'B'), ('V-Johnson', 'V'), ('g-ZTF', 'g'), ('cyan', 'c'),
            ('cyan-ATLAS', 'c'), ('orange', 'o'), ('orange-ATLAS', 'o'), ('R-Cousins', 'r'),
            ('I', 'i'), ('w-PS1', 'w'), ('zp', 'z'), ('y-LSST', 'y'), ('lssty', 'y'),
            ('J', 'J'), ('H', 'H'), ('K', 'K'), ('Ks', 'K'),
            ('Halpha-narrow', None), ('bp-Gaia', None), ('', None), (None, None),
        ):
            with self.subTest(name=name):
                self.assertEqual(filter_color_key(name), key)

    def test_band_colours(self):
        self.assertEqual(band_display_color('cyan-ATLAS'), AUDIT_PALETTE['c'])
        self.assertEqual(band_display_color('orange-ATLAS'), AUDIT_PALETTE['o'])
        self.assertNotEqual(band_display_color('cyan-ATLAS'), band_display_color('g-ZTF'))
        self.assertNotEqual(band_display_color('orange-ATLAS'), band_display_color('r-ZTF'))
        # the palette wins over a database colour
        self.assertEqual(band_display_color('UVW2', '#123456'), AUDIT_PALETTE['UVW2'])
        self.assertEqual(band_display_color('J'), AUDIT_PALETTE['J'])
        # a band without a fixed colour keeps the database value, else a stable fallback
        self.assertEqual(band_display_color('Halpha-narrow', '#123456'), '#123456')
        self.assertEqual(
            band_display_color('Halpha-narrow', fallback_index=0),
            band_display_color('Halpha-narrow', fallback_index=5),
        )

    def test_colour_groups_cover_every_colour(self):
        groups = filter_color_groups_for_display()
        self.assertEqual([g['filter'] for g in groups], list(FILTER_COLORS))
        by_key = {g['filter']: g for g in groups}
        self.assertIn('cyan-ATLAS', by_key['c']['examples'])
        self.assertNotIn('cyan-ATLAS', by_key['g']['examples'])

    def test_close_pairs_are_carried_by_marker_shape(self):
        # o (orange) is ATLAS-only; r and g come from the other surveys, whose markers differ
        atlas = telescope_display_symbol('ACAM1', None, 'ATLAS')
        self.assertEqual(atlas, FAMILY_SYMBOLS['ATLAS'])
        for family, symbol in FAMILY_SYMBOLS.items():
            if family != 'ATLAS':
                self.assertNotEqual(symbol, atlas, family)

    def test_spectrum_palette_is_okabe_ito_without_yellow(self):
        self.assertEqual(SPECTRUM_COLORS[:3], ('#0072B2', '#D55E00', '#009E73'))
        self.assertNotIn('#F0E442', SPECTRUM_COLORS)


def _glyphs(doc, glyph_type):
    refs = {ref['id']: ref for ref in doc['roots']['references']}
    out = []
    for ref in refs.values():
        if ref['type'] != 'GlyphRenderer':
            continue
        glyph = refs[ref['attributes']['glyph']['id']]
        if glyph['type'] == glyph_type:
            out.append((glyph['attributes'], refs[ref['attributes']['data_source']['id']]['attributes']))
    return out


def _upper_limit_glyphs(doc):
    return [g for g, _src in _glyphs(doc, 'Scatter') if g.get('marker', {}).get('value') == 'inverted_triangle']


def _series_colours(doc):
    refs = {ref['id']: ref for ref in doc['roots']['references']}
    colours = {}
    for legend in legends_in_doc(doc):
        for item in legend['attributes'].get('items', []):
            attrs = refs[item['id']]['attributes']
            label = attrs['label']['value']
            if not label or label.startswith('today'):
                continue
            renderer = refs[attrs['renderers'][0]['id']]
            glyph = refs[renderer['attributes']['glyph']['id']]['attributes']
            colours[label] = next(
                spec['value'] for spec in (glyph.get('fill_color'), glyph.get('line_color'))
                if isinstance(spec, dict) and spec.get('value')
            ).lower()
    return colours


class PlotColourTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_user('band_colour_user', is_staff=True)
        cls.transient = seed_many_band_transient(
            cls.user, name='band-colour-sn',
            series=(
                ('Pan-STARRS1', 'GPC1', ['g', 'r']),
                ('ATLAS', 'ACAM1', ['cyan-ATLAS', 'orange-ATLAS']),
                ('Swift', 'UVOT', ['UVW2']),
            ),
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _doc(self, view_name, transient=None):
        transient = transient or self.transient
        with mock.patch.dict(os.environ, {'YSE_PLOT_HTML_CACHE': '0'}):
            response = self.client.get(reverse(view_name, args=[transient.id]) + '?w=1000')
        self.assertEqual(response.status_code, 200)
        return bokeh_doc_from_html(response.content.decode())

    def test_light_curves_use_the_palette(self):
        for view in ('lightcurveplot_detail', 'lightcurveplot_flux', 'lightcurveplot_summary'):
            with self.subTest(view=view):
                colours = _series_colours(self._doc(view))
                self.assertEqual(colours['PS1 g'], AUDIT_PALETTE['g'])
                self.assertEqual(colours['PS1 r'], AUDIT_PALETTE['r'])
                # ATLAS c and o keep their g / r legend labels but have their own colours
                self.assertEqual(colours['ATLAS g'], AUDIT_PALETTE['c'])
                self.assertEqual(colours['ATLAS r'], AUDIT_PALETTE['o'])
                self.assertEqual(colours['Swift UVW2'], AUDIT_PALETTE['UVW2'])

    def test_upper_limits_are_hollow_triangles_with_a_downward_stem(self):
        doc = self._doc('lightcurveplot_detail')
        limits = _upper_limit_glyphs(doc)
        self.assertEqual(len(limits), 5)
        for glyph in limits:
            self.assertEqual(glyph['fill_alpha']['value'], 0.0)
        # one stem renderer per series, from the limit towards fainter magnitudes
        stems = _glyphs(doc, 'Segment')
        self.assertEqual(len(stems), 5)
        for _glyph, source in stems:
            data = source['data']
            self.assertTrue(data['y0'])
            for y0, y1 in zip(data['y0'], data['y1']):
                self.assertGreater(y1, y0)

    def test_stems_hide_with_their_legend_entry(self):
        doc = self._doc('lightcurveplot_detail')
        refs = {ref['id']: ref for ref in doc['roots']['references']}
        followers = set()
        for ref in refs.values():
            if ref['type'] == 'CustomJS' and 'followers' in ref['attributes']['args']:
                followers.update(f['id'] for f in ref['attributes']['args']['followers'])
        stems = {
            ref['id'] for ref in refs.values()
            if ref['type'] == 'GlyphRenderer'
            and refs[ref['attributes']['glyph']['id']]['type'] == 'Segment'
        }
        self.assertTrue(stems)
        self.assertLessEqual(stems, followers)

    def test_spectra_use_okabe_ito(self):
        transient = create_minimal_transient(self.user, name='band-colour-spec')
        first = attach_synthetic_spectrum(self.user, transient)
        second = attach_synthetic_spectrum(
            self.user, transient, obs_group=first.obs_group, instrument=first.instrument,
        )
        TransientSpectrum.objects.filter(pk=second.pk).update(
            obs_date=first.obs_date - datetime.timedelta(days=3)
        )
        for spectrum in (first, second):
            TransientSpecData.objects.bulk_create([
                TransientSpecData(spectrum=spectrum, wavelength=4000 + 10 * i, flux=1.0 + 0.1 * i,
                                  **audit_fields(self.user))
                for i in range(20)
            ])
        for view in ('spectrumplot', 'spectrumplot_summary'):
            with self.subTest(view=view):
                # bokeh serialises a colour as a plain string or as {"value": ...}
                specs = [g['line_color'] for g, _src in _glyphs(self._doc(view, transient), 'Step')]
                colours = {(s['value'] if isinstance(s, dict) else s).lower() for s in specs}
                self.assertEqual(colours, {c.lower() for c in SPECTRUM_COLORS[:2]})
