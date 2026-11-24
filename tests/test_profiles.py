import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from amdoc import profiles
from amdoc.gpu import Gpu


def dummy_gpu() -> Gpu:
    return Gpu(card="card9", device_path="/nonexistent", pci_slot="0000:00:00.0",
               pci_id="1002:0000", boot_vga=True)


class ProfileDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["AMDOC_PROFILE_DIR"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("AMDOC_PROFILE_DIR", None)
        self.tmp.cleanup()


class TestProfileStorage(ProfileDirTest):
    def test_list_empty(self):
        self.assertEqual(profiles.list_profiles(), [])

    def test_save_load_roundtrip(self):
        path = profiles.profiles_dir() / "gaming.json"
        profile = {
            "version": profiles.PROFILE_VERSION,
            "name": "gaming",
            "sclk": {"min": 500, "max": 2600},
            "mclk": None,
            "power_cap_w": 170,
            "fan": {"mode": "manual", "pwm": 180},
            "curve": None,
            "perf_level": "auto",
            "dpm_pin": None,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(profile))

        loaded = profiles.load("gaming")
        self.assertEqual(loaded["sclk"]["max"], 2600)
        self.assertEqual([p.stem for p in profiles.list_profiles()], ["gaming"])

        profiles.remove("gaming")
        self.assertEqual(profiles.list_profiles(), [])

    def test_missing_profile_message(self):
        with self.assertRaises(profiles.ProfileError) as ctx:
            profiles.load("nope")
        self.assertIn("not found", str(ctx.exception))

    def test_invalid_name_rejected(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.load("../evil")

    def test_unreadable_profile(self):
        path = profiles.profiles_dir() / "broken.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        with self.assertRaises(profiles.ProfileError):
            profiles.load("broken")


class TestValidation(ProfileDirTest):
    def _profile(self, **overrides):
        base = {
            "version": profiles.PROFILE_VERSION,
            "sclk": {"min": 500, "max": 2600},
            "mclk": None,
            "power_cap_w": None,
            "fan": {"mode": "auto", "pwm": None},
            "curve": None,
            "perf_level": "auto",
            "dpm_pin": None,
        }
        base.update(overrides)
        return base

    def test_good_profile_validates(self):
        self.assertEqual(profiles.validate(self._profile(), dummy_gpu()), [])

    def test_wrong_version_rejected(self):
        errors = profiles.validate(self._profile(version=99), dummy_gpu())
        self.assertTrue(errors and "version" in errors[0])

    def test_inverted_clock_range_rejected(self):
        errors = profiles.validate(
            self._profile(sclk={"min": 2600, "max": 500}), dummy_gpu()
        )
        self.assertTrue(any("sclk" in e for e in errors))

    def test_bad_fan_mode_rejected(self):
        errors = profiles.validate(
            self._profile(fan={"mode": "turbo", "pwm": None}), dummy_gpu()
        )
        self.assertTrue(any("fan" in e for e in errors))

    def test_bad_fan_pwm_rejected(self):
        errors = profiles.validate(
            self._profile(fan={"mode": "manual", "pwm": 999}), dummy_gpu()
        )
        self.assertTrue(any("pwm" in e for e in errors))

    def test_bad_curve_rejected(self):
        errors = profiles.validate(
            self._profile(curve={"points": [[40, 200]], "sensor": "edge"}),
            dummy_gpu(),
        )
        self.assertTrue(any("curve" in e for e in errors))

    def test_non_numeric_power_rejected(self):
        errors = profiles.validate(self._profile(power_cap_w="lots"), dummy_gpu())
        self.assertTrue(any("power" in e for e in errors))


class TestSudoProfileDir(unittest.TestCase):
    def test_sudo_uses_invoking_users_home(self):
        env = {"SUDO_USER": "fitzypopper", "HOME": "/root"}
        for key in ("AMDOC_PROFILE_DIR", "XDG_CONFIG_HOME"):
            env[key] = None  # patch.dict with None removes the key
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(profiles.pwd, "getpwnam",
                               return_value=SimpleNamespace(pw_dir="/home/fitzypopper")):
            self.assertEqual(
                profiles.profiles_dir(),
                Path("/home/fitzypopper/.config/amd-gpu-oc/profiles"),
            )

    def test_explicit_override_still_wins(self):
        with mock.patch.dict(os.environ, {
            "AMDOC_PROFILE_DIR": "/tmp/custom-profiles",
            "SUDO_USER": "fitzypopper",
        }):
            self.assertEqual(profiles.profiles_dir(), Path("/tmp/custom-profiles"))


if __name__ == "__main__":
    unittest.main()
