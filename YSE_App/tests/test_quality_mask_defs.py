"""quality_mask bit definitions (#79; YSE_App/common/quality_mask.py, docs/quality-mask.md).

Pins the FINAL v6 spec from issue #79: bit numbers and names, the even-bit
spacing, the fixed REJECTED/OTHER/NOT_PUBLISHED positions, the ordering
property (any set higher bit compares worse than every combination of lower
bits), the thresholds, and the helper round-trips.
"""

import itertools
import os
import re

from django.test import SimpleTestCase

from YSE_App.common import quality_mask as qm
from YSE_App.common.quality_mask import (
    HOST_PHOT_ALLOWED,
    HostSpecFlag,
    NO_OTHER,
    PhotFlag,
    PixelFlag,
    SpecFlag,
    TRANSIENT_PHOT_ALLOWED,
    USABLE,
)

# The v6 tables with the v7 additions (23, 41), transcribed from issue #79.
V6_PHOT = {
    0: 'NOT_PUBLISHED', 2: 'NON_PHOTOMETRIC', 4: 'HIGH_BACKGROUND', 6: 'APERTURE',
    8: 'NOT_FORCED', 10: 'NOT_FULLY_REDUCED', 12: 'NO_SUPERCAL', 14: 'NOT_DIFFERENCED',
    16: 'NOT_BKG_SUBTRACTED', 18: 'POOR_SEEING', 20: 'LOW_SN', 22: 'TEMPLATE_SUSPECT', 23: 'BAD_SUBTRACTION',
    24: 'QUICKLOOK', 26: 'BLENDED', 28: 'SHREDDED', 30: 'TRANSIENT_CONTAMINATED',
    32: 'NEAR_EDGE', 34: 'NOT_CALIBRATED', 36: 'CR', 38: 'TRAIL_GHOST', 40: 'BAD_PIXEL',
    41: 'MASKED', 42: 'SATURATED', 50: 'OTHER', 51: 'REJECTED',
}
V6_PHOT_TRANSIENT_ONLY = {'NON_PHOTOMETRIC', 'APERTURE', 'NOT_FORCED', 'NO_SUPERCAL',
                          'NOT_DIFFERENCED', 'NOT_BKG_SUBTRACTED', 'TEMPLATE_SUSPECT'}
V6_PHOT_HOST_ONLY = {'LOW_SN', 'SHREDDED', 'TRANSIENT_CONTAMINATED'}
V6_SPEC = {
    0: 'NOT_PUBLISHED', 2: 'NOT_ABS_CALIBRATED', 4: 'STITCHED', 6: 'BOXCAR_EXTRACTION',
    8: 'DEREDSHIFTED', 10: 'DEREDDENED', 12: 'HIGH_AIRMASS', 14: 'LOW_SN',
    16: 'NOT_FULLY_REDUCED', 18: 'HOST_CONTAMINATED', 20: 'QUICKLOOK', 22: 'NOT_PARALLACTIC',
    24: 'WAVELENGTH_SUSPECT', 26: 'SECOND_ORDER', 28: 'NOT_TELLURIC_CORRECTED',
    30: 'WAVELENGTH_MEDIUM_UNKNOWN', 32: 'NOT_FLUX_CALIBRATED', 50: 'OTHER', 51: 'REJECTED',
}
V6_PIXEL = {0: 'BAD_PIXEL', 1: 'CR', 2: 'SKY_RESIDUAL', 3: 'FRINGING'}

DOC_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'docs', 'quality-mask.md')


def bits(flag_cls):
    return {int(f).bit_length() - 1: f.name for f in flag_cls.__members__.values()}


class SpecTablesTests(SimpleTestCase):
    def test_photometry_bits_match_v6(self):
        self.assertEqual(bits(PhotFlag), V6_PHOT)

    def test_transient_spectrum_bits_match_v6(self):
        self.assertEqual(bits(SpecFlag), V6_SPEC)

    def test_host_spectrum_differs_only_at_bit_18(self):
        expected = {**V6_SPEC, 18: 'TRANSIENT_CONTAMINATED'}
        self.assertEqual(bits(HostSpecFlag), expected)
        for f in SpecFlag:
            if f.name != 'HOST_CONTAMINATED':
                self.assertEqual(int(HostSpecFlag[f.name]), int(f))

    def test_pixel_mask_bits(self):
        self.assertEqual(bits(PixelFlag), V6_PIXEL)
        self.assertLess(qm.PIXEL_ALLOWED, 1 << 15)  # fits PositiveSmallIntegerField

    def test_fixed_positions_in_every_set(self):
        for cls in (PhotFlag, SpecFlag, HostSpecFlag):
            self.assertEqual(int(cls.REJECTED), 1 << 51)
            self.assertEqual(int(cls.OTHER), 1 << 50)
            self.assertEqual(int(cls.NOT_PUBLISHED), 1)

    def test_flags_on_even_bits_except_rejected(self):
        for cls in (PhotFlag, SpecFlag, HostSpecFlag):
            for bit, name in bits(cls).items():
                # v7 put BAD_SUBTRACTION (23) and MASKED (41) on reserved odd bits
                if name not in ('REJECTED', 'BAD_SUBTRACTION', 'MASKED'):
                    self.assertEqual(bit % 2, 0, (cls.__name__, name))

    def test_masks_are_javascript_safe(self):
        for cls in (PhotFlag, SpecFlag, HostSpecFlag):
            everything = qm.mask_from_flags(list(cls.__members__.values()), cls)
            self.assertLess(everything, 2 ** 53)

    def test_transient_and_host_photometry_subsets(self):
        transient = set(qm.flags_from_mask(TRANSIENT_PHOT_ALLOWED, PhotFlag))
        host = set(qm.flags_from_mask(HOST_PHOT_ALLOWED, PhotFlag))
        everything = set(V6_PHOT.values())
        self.assertEqual(transient, everything - V6_PHOT_HOST_ONLY)
        self.assertEqual(host, everything - V6_PHOT_TRANSIENT_ONLY)
        self.assertEqual(qm.allowed_mask('TransientPhotData'), TRANSIENT_PHOT_ALLOWED)
        self.assertEqual(qm.allowed_mask('HostPhotData'), HOST_PHOT_ALLOWED)


class OrderingTests(SimpleTestCase):
    def assert_ordered(self, flag_cls):
        members = sorted(flag_cls.__members__.values(), key=int)
        for i, higher in enumerate(members):
            lower = members[:i]
            # every lower flag at once is still better than the single higher flag
            self.assertLess(qm.mask_from_flags(lower, flag_cls), int(higher))
            # and a higher flag on top of any lower combination is worse than all lower flags
            for r in (1, 2):
                for combo in itertools.combinations(lower, r):
                    self.assertGreater(int(higher) | qm.mask_from_flags(combo, flag_cls),
                                       qm.mask_from_flags(lower, flag_cls))

    def test_set_higher_bit_always_compares_worse(self):
        for cls in (PhotFlag, SpecFlag, HostSpecFlag, PixelFlag):
            self.assert_ordered(cls)

    def test_zero_is_best(self):
        for cls in (PhotFlag, SpecFlag, HostSpecFlag):
            for f in cls:
                self.assertGreater(int(f), 0)


class ThresholdTests(SimpleTestCase):
    def test_usable_and_no_other(self):
        self.assertEqual(USABLE, 2 ** 51)
        self.assertEqual(NO_OTHER, 2 ** 50)
        for cls in (PhotFlag, SpecFlag, HostSpecFlag):
            below_other = [f for f in cls if int(f) < NO_OTHER]
            all_below = qm.mask_from_flags(below_other, cls)
            self.assertLess(all_below, NO_OTHER)
            self.assertLess(all_below | int(cls.OTHER), USABLE)
            self.assertGreaterEqual(int(cls.OTHER), NO_OTHER)
            self.assertGreaterEqual(int(cls.REJECTED), USABLE)
            self.assertGreaterEqual(int(cls.REJECTED) | 1, USABLE)

    def test_group_boundaries_sit_on_flag_bits(self):
        boundaries = {
            PhotFlag: (qm.PHOT_BEST, qm.PHOT_REDUCED, qm.PHOT_SCIENCE, qm.PHOT_CLEAN),
            SpecFlag: (qm.SPEC_BEST, qm.SPEC_GOOD_CONDITIONS, qm.SPEC_SCIENCE, qm.SPEC_CALIBRATED),
        }
        for cls, values in boundaries.items():
            self.assertEqual(list(values), sorted(values))
            flag_values = {int(f) for f in cls}
            for v in values:
                self.assertIn(v, flag_values)
                self.assertLess(v, NO_OTHER)

    def test_threshold_cuts(self):
        self.assertLess(int(PhotFlag.QUICKLOOK), qm.PHOT_CLEAN)
        self.assertGreaterEqual(int(PhotFlag.QUICKLOOK), qm.PHOT_SCIENCE)
        self.assertGreaterEqual(int(PhotFlag.NEAR_EDGE), qm.PHOT_CLEAN)
        self.assertGreaterEqual(int(SpecFlag.WAVELENGTH_MEDIUM_UNKNOWN), qm.SPEC_CALIBRATED)
        self.assertLess(int(SpecFlag.QUICKLOOK), qm.SPEC_CALIBRATED)
        self.assertGreaterEqual(int(SpecFlag.QUICKLOOK), qm.SPEC_SCIENCE)

    def test_cuts_ignore_publication(self):
        for threshold in (USABLE, NO_OTHER, qm.PHOT_CLEAN, qm.PHOT_SCIENCE, qm.PHOT_REDUCED,
                          qm.PHOT_BEST, qm.SPEC_CALIBRATED, qm.SPEC_BEST):
            self.assertLess(0 | int(PhotFlag.NOT_PUBLISHED), threshold)

    def test_quality_lt(self):
        q = qm.quality_lt(USABLE)
        self.assertEqual(q.children, [('quality_mask__lt', USABLE)])
        self.assertEqual(qm.quality_lt(qm.PHOT_CLEAN, field='x').children, [('x__lt', qm.PHOT_CLEAN)])


class HelperTests(SimpleTestCase):
    def test_round_trip_every_single_flag(self):
        for cls in (PhotFlag, SpecFlag, HostSpecFlag, PixelFlag):
            for f in cls:
                self.assertEqual(qm.flags_from_mask(int(f), cls), [f.name])
                self.assertEqual(qm.mask_from_flags([f.name], cls), int(f))

    def test_round_trip_combinations(self):
        for cls in (PhotFlag, SpecFlag, HostSpecFlag):
            members = sorted(cls.__members__.values(), key=int)
            for combo in itertools.combinations(members, 3):
                mask = qm.mask_from_flags(combo, cls)
                names = qm.flags_from_mask(mask, cls)
                self.assertEqual(names, [f.name for f in combo])
                self.assertEqual(qm.mask_from_flags(names, cls), mask)

    def test_empty_and_none(self):
        self.assertEqual(qm.flags_from_mask(0), [])
        self.assertEqual(qm.flags_from_mask(None), [])
        self.assertEqual(qm.mask_from_flags([]), 0)

    def test_reserved_bits_are_reported_not_dropped(self):
        self.assertEqual(qm.flags_from_mask((1 << 3) | 1, PhotFlag), ['NOT_PUBLISHED', 'BIT_3'])

    def test_names_are_case_insensitive_and_unknown_names_raise(self):
        self.assertEqual(qm.mask_from_flags(['quicklook'], PhotFlag), int(PhotFlag.QUICKLOOK))
        with self.assertRaises(KeyError):
            qm.mask_from_flags(['NOT_A_FLAG'], PhotFlag)
        with self.assertRaises(TypeError):
            qm.mask_from_flags([SpecFlag.QUICKLOOK], PhotFlag)

    def test_is_published_is_a_bit_test(self):
        self.assertTrue(qm.is_published(0))
        self.assertTrue(qm.is_published(None))
        self.assertFalse(qm.is_published(1))
        self.assertFalse(qm.is_published(int(PhotFlag.REJECTED) | 1))
        self.assertTrue(qm.is_published(int(PhotFlag.REJECTED)))

    def test_wavelength_medium_bit(self):
        bit = int(SpecFlag.WAVELENGTH_MEDIUM_UNKNOWN)
        self.assertEqual(bit, int(HostSpecFlag.WAVELENGTH_MEDIUM_UNKNOWN))
        for medium in (None, '', 'unknown', qm.WavelengthMedium.UNKNOWN):
            self.assertTrue(qm.wavelength_medium_unknown(medium))
            self.assertEqual(qm.apply_wavelength_medium(0, medium), bit)
        for medium in ('air', 'vacuum', qm.WavelengthMedium.AIR, qm.WavelengthMedium.VACUUM):
            self.assertFalse(qm.wavelength_medium_unknown(medium))
            self.assertEqual(qm.apply_wavelength_medium(bit | 5, medium), 5)
        # other bits are kept either way
        rejected = int(SpecFlag.REJECTED)
        self.assertEqual(qm.apply_wavelength_medium(rejected, 'unknown'), rejected | bit)

    def test_choices(self):
        self.assertEqual(qm.WavelengthMedium.values, ['vacuum', 'air', 'unknown'])
        self.assertEqual(qm.HostSubtraction.values, ['local', 'archival', 'template', 'none', 'unknown'])
        self.assertEqual(qm.PhotType.values,
                         ['total', 'r25', 'kron', 'petrosian', 'aperture', 'model', 'psf', 'other'])


class DocsTests(SimpleTestCase):
    """docs/quality-mask.md lists every flag at its bit."""

    def test_doc_tables_match_code(self):
        with open(DOC_PATH) as fh:
            text = fh.read()
        rows = set(re.findall(r'^\|\s*(\d+)\s*\|\s*`?([A-Z_]+)`?', text, flags=re.M))
        for label, entries in qm.describe().items():
            for bit, name in entries:
                self.assertIn((str(bit), name), rows, (label, bit, name))
