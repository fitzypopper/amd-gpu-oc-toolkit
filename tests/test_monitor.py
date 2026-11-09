import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import unittest

from amdoc import monitor, sensors
from amdoc.gpu import Gpu


class TestCsvRow(unittest.TestCase):
    def test_row_matches_header(self):
        snap = sensors.Snapshot(ts=1700000000.0)
        row = monitor._csv_row(snap, "card1")
        self.assertEqual(len(row), len(monitor.CSV_FIELDS))
        self.assertEqual(row[monitor.CSV_FIELDS.index("card")], "card1")

    def test_row_fills_values(self):
        snap = sensors.Snapshot(
            ts=1700000000.0,
            temps={"edge": 40.5, "junction": 55.25, "mem": 42.0},
            clocks={"sclk": 812.0, "mclk": 96.0},
            fan_rpm=1200,
            fan_pwm=96,
            power_w=42.5,
            gpu_busy=77,
            mem_busy=10,
            vddgfx_v=0.658,
        )
        row = dict(zip(monitor.CSV_FIELDS, monitor._csv_row(snap, "card0")))
        self.assertEqual(row["temp_junction_c"], "55.25")
        self.assertEqual(row["sclk_mhz"], "812")
        self.assertEqual(row["power_w"], "42.5")
        self.assertEqual(row["gpu_busy_pct"], "77")


class TestDpmParse(unittest.TestCase):
    def test_read_dpm_from_text(self):
        # read_dpm reads a path; exercise the same logic through a temp file
        import tempfile

        content = "0: 255Mhz\n1: 965Mhz *\n2: 2539Mhz\n"
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write(content)
            path = fh.name
        try:
            states = sensors.read_dpm(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(states), 3)
        self.assertEqual(states[1], (1, 965.0, True))
        self.assertEqual(states[0][2], False)

    def test_read_dpm_missing_file(self):
        self.assertEqual(sensors.read_dpm("/nonexistent/pp_dpm_sclk"), [])


class TestHelpers(unittest.TestCase):
    def test_format_vram(self):
        self.assertEqual(sensors.format_vram(None), "n/a")
        self.assertEqual(sensors.format_vram(8 * 1024**3), "8.0 GiB")
        self.assertEqual(sensors.format_vram(512 * 1024**2), "512 MiB")

    def test_dpm_active(self):
        domains = {"sclk": [(0, 255.0, False), (1, 965.0, True)]}
        self.assertIn("965", sensors.dpm_active(domains, "sclk"))
        self.assertEqual(sensors.dpm_active(domains, "mclk"), "-")

    def test_snapshot_temp_lookup(self):
        snap = sensors.Snapshot(ts=0.0, temps={"junction": 55.0})
        self.assertEqual(snap.temp("JUNCTION"), 55.0)
        self.assertIsNone(snap.temp("edge"))
        self.assertEqual(snap.hottest, ("junction", 55.0))

    def test_pick_prefers_discrete(self):
        a = Gpu(card="card0", device_path="/a", pci_slot="", pci_id="1002:164e",
                boot_vga=False)
        b = Gpu(card="card1", device_path="/b", pci_slot="", pci_id="1002:7480",
                boot_vga=True, hwmon_path="/b/hwmon")
        from amdoc.gpu import pick

        self.assertTrue(a.is_apu)
        self.assertFalse(b.is_apu)
        self.assertIs(pick([a, b]), b)
        self.assertIs(pick([a, b], "card0"), a)
        self.assertIsNone(pick([a, b], "card7"))

    def test_pick_largest_vram_when_both_discrete(self):
        a = Gpu(card="card0", device_path="/a", pci_slot="", pci_id="1002:744c",
                boot_vga=True, hwmon_path="/a/hwmon")
        b = Gpu(card="card1", device_path="/b", pci_slot="", pci_id="1002:7480",
                boot_vga=False, hwmon_path="/b/hwmon")
        from amdoc.gpu import pick

        # both look discrete; the one with more reported VRAM wins
        # (vram_bytes reads sysfs and returns 0 for both here, so order is stable)
        self.assertIs(pick([a, b]), a)


if __name__ == "__main__":
    unittest.main()
