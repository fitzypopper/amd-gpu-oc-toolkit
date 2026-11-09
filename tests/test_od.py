import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import unittest

from amdoc import od
from amdoc.gpu import Gpu


SAMPLE = """OD_SCLK:
0:        400Mhz
1:       2200Mhz
OD_MCLK:
0:         96Mhz
1:       2250Mhz
OD_RANGE:
SCLK:     400Mhz       3000Mhz
MCLK:      96Mhz       2400Mhz
"""


class TestOdParse(unittest.TestCase):
    def test_parse_points_and_ranges(self):
        table = od.OdTable.parse(SAMPLE)
        self.assertEqual(table.values("SCLK"), [400, 2200])
        self.assertEqual(table.values("MCLK"), [96, 2250])
        self.assertIsNotNone(table.sclk_range)
        self.assertEqual((table.sclk_range.lo, table.sclk_range.hi), (400, 3000))
        self.assertEqual(table.mclk_range.unit, "Mhz")
        self.assertIn("OD_SCLK", table.sections)

    def test_parse_handles_extra_whitespace(self):
        table = od.OdTable.parse("OD_SCLK:\n0:  500 MHz\n1:\t2655MHz\n")
        self.assertEqual(table.values("SCLK"), [500, 2655])

    def test_parse_empty(self):
        table = od.OdTable.parse("\n")
        self.assertEqual(table.values("SCLK"), [])
        self.assertIsNone(table.sclk_range)

    def test_parse_curve_section(self):
        sample = "OD_VDDC_CURVE:\n0: 300MHz 750mV\n1: 1500MHz 950mV\n"
        table = od.OdTable.parse(sample)
        self.assertEqual(table.points["OD_VDDC_CURVE"][0], (0, [300, 750]))


class TestValidation(unittest.TestCase):
    def test_min_must_be_below_max(self):
        with self.assertRaises(od.OdError):
            od._validate_range("SCLK", 2600, 500, od.OdRange(400, 3000))

    def test_equal_values_rejected(self):
        with self.assertRaises(od.OdError):
            od._validate_range("SCLK", 2000, 2000, od.OdRange(400, 3000))

    def test_out_of_range_rejected(self):
        with self.assertRaises(od.OdError) as ctx:
            od._validate_range("SCLK", 500, 3500, od.OdRange(400, 3000))
        self.assertIn("3000", str(ctx.exception))

    def test_in_range_passes(self):
        od._validate_range("SCLK", 500, 2600, od.OdRange(400, 3000))

    def test_missing_range_defers_to_driver(self):
        od._validate_range("SCLK", 500, 2600, None)


class TestUnavailable(unittest.TestCase):
    def test_available_reflects_missing_file(self):
        gpu = Gpu(card="card9", device_path="/nonexistent", pci_slot="0000:00:00.0",
                  pci_id="1002:0000", boot_vga=True)
        self.assertFalse(od.available(gpu))
        with self.assertRaises(Exception):
            od.read_table(gpu)


if __name__ == "__main__":
    unittest.main()
