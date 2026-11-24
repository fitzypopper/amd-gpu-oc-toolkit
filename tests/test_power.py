import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import unittest

from amdoc import control, sysfs
from amdoc.gpu import Gpu


class TestPowerCapVerification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = os.path.join(self.tmp.name, "power1_cap")
        with open(path, "w") as fh:
            fh.write("179000000\n")
        self.gpu = Gpu("card1", "/sys/fake", "0000:03:00.0", "1002:7480", False,
                       hwmon_path=self.tmp.name)

    def _set(self, watts, readback):
        info = {"cap": 179.0, "default": 165.0, "min": 155.0, "max": 198.0}
        with mock.patch.object(control, "power_info", return_value=info), \
             mock.patch.object(control.sysfs, "write_value") as write, \
             mock.patch.object(control.sysfs, "read_float", return_value=readback):
            control.set_power_cap(self.gpu, watts)
            return write

    def test_driver_rounding_is_accepted(self):
        write = self._set(180, 179_000_000.0)
        write.assert_called_once()

    def test_real_mismatch_is_rejected(self):
        with self.assertRaises(sysfs.SysfsError):
            self._set(180, 150_000_000.0)

    def test_above_max_rejected_before_write(self):
        info = {"cap": 179.0, "default": 165.0, "min": 155.0, "max": 198.0}
        with mock.patch.object(control, "power_info", return_value=info), \
             mock.patch.object(control.sysfs, "write_value") as write:
            with self.assertRaises(sysfs.SysfsError):
                control.set_power_cap(self.gpu, 220)
            write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
