import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import unittest

from amdoc import doctor


MASK = "0xfff7ffff"
ARG = f"amdgpu.ppfeaturemask={MASK}"


def _fake_fs(existing, globs=None):
    """Patch doctor's filesystem probes with a fixed set of existing paths."""
    globs = globs or {}

    def exists(path):
        return path in existing

    def isdir(path):
        return path in existing

    def glob(pattern):
        return globs.get(pattern, [])

    return (
        mock.patch.object(doctor.os.path, "exists", side_effect=exists),
        mock.patch.object(doctor.os.path, "isdir", side_effect=isdir),
        mock.patch.object(doctor.glob, "glob", side_effect=glob),
    )


class TestBootloaderSteps(unittest.TestCase):
    def _steps(self, existing, globs=None):
        patches = _fake_fs(existing, globs)
        for patch in patches:
            patch.start()
        try:
            return doctor._bootloader_steps(MASK)
        finally:
            for patch in reversed(patches):
                patch.stop()

    def test_systemd_boot_uki_with_mkinitcpio(self):
        steps = self._steps(
            {
                "/boot/loader/loader.conf",
                "/etc/kernel/cmdline",
                "/etc/default/grub",  # present but NOT the active bootloader
            },
            {"/etc/mkinitcpio.d/*.preset": ["/etc/mkinitcpio.d/linux.preset"]},
        )
        joined = " ".join(steps)
        self.assertIn(f"add  {ARG}  to /etc/kernel/cmdline", joined)
        self.assertIn("sudo mkinitcpio -P", joined)
        self.assertNotIn("grub", joined.lower())

    def test_systemd_boot_type1_entries(self):
        steps = self._steps(
            {"/boot/loader/loader.conf", "/boot/EFI/Linux"},
            {"/boot/loader/entries/*.conf": ["/boot/loader/entries/arch.conf"]},
        )
        joined = " ".join(steps)
        self.assertIn("'options' line", joined)
        self.assertIn("arch.conf", joined)
        self.assertNotIn("GRUB_CMDLINE", joined)

    def test_grub_when_no_systemd_boot(self):
        steps = self._steps({"/etc/default/grub", "/boot/grub/grub.cfg"})
        joined = " ".join(steps)
        self.assertIn("GRUB_CMDLINE_LINUX_DEFAULT", joined)
        self.assertIn("grub-mkconfig", joined)

    def test_generic_fallback(self):
        steps = self._steps(set())
        self.assertEqual(len(steps), 2)
        self.assertIn("your bootloader's kernel command line", steps[0])
        self.assertIn("reboot", steps[1])

    def test_uki_without_preset_still_gives_rebuild_hint(self):
        steps = self._steps({"/boot/EFI/Linux", "/etc/kernel/cmdline"})
        joined = " ".join(steps)
        self.assertIn("/etc/kernel/cmdline", joined)
        self.assertIn("uki", joined.lower())


if __name__ == "__main__":
    unittest.main()
