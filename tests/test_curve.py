import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import unittest

from amdoc import control
from amdoc.sysfs import SysfsError


class TestCurveSpec(unittest.TestCase):
    def test_parse(self):
        curve = control.Curve.from_spec("40:30,60:70,80:100")
        self.assertEqual(curve.points, [(40.0, 30.0), (60.0, 70.0), (80.0, 100.0)])
        self.assertEqual(curve.sensor, "junction")

    def test_points_are_sorted(self):
        curve = control.Curve.from_spec("80:100,40:30")
        self.assertEqual(curve.points[0][0], 40.0)

    def test_bad_point_rejected(self):
        with self.assertRaises(SysfsError):
            control.Curve.from_spec("40-30")

    def test_empty_rejected(self):
        with self.assertRaises(SysfsError):
            control.Curve.from_spec("")

    def test_pwm_out_of_percent_rejected(self):
        with self.assertRaises(SysfsError):
            control.Curve(points=[(40, 150)])


class TestInterpolation(unittest.TestCase):
    def setUp(self):
        self.curve = control.Curve(points=[(40, 30), (60, 70), (80, 100)])

    def test_below_first_point_uses_first(self):
        self.assertEqual(self.curve.pwm_percent(20), 30)

    def test_above_last_point_uses_last(self):
        self.assertEqual(self.curve.pwm_percent(95), 100)

    def test_exact_anchor(self):
        self.assertEqual(self.curve.pwm_percent(60), 70)

    def test_midpoint_interpolates(self):
        self.assertEqual(self.curve.pwm_percent(50), 50)  # halfway 40->60: 30->70

    def test_spec_roundtrip(self):
        curve = control.Curve.from_spec("40:30,60:70,80:100")
        self.assertEqual(curve.spec(), "40:30,60:70,80:100")


if __name__ == "__main__":
    unittest.main()
