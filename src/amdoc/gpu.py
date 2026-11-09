"""GPU discovery: find amdgpu devices in sysfs and describe them."""

from __future__ import annotations

import glob
import os
import re
import subprocess
from dataclasses import dataclass

from . import sysfs


@dataclass
class Gpu:
    """One amdgpu device (a DRM card backed by a PCI function)."""

    card: str  # e.g. "card1"
    device_path: str  # sysfs device directory
    pci_slot: str  # e.g. "0000:03:00.0"
    pci_id: str  # e.g. "1002:7480"
    boot_vga: bool
    driver: str = ""
    name: str = ""  # human readable, from lspci when available
    hwmon_path: str | None = None

    # --- convenience paths -------------------------------------------------
    @property
    def od_path(self) -> str:
        return os.path.join(self.device_path, "pp_od_clk_voltage")

    @property
    def od_available(self) -> bool:
        return os.path.exists(self.od_path)

    def path(self, *parts: str) -> str:
        return os.path.join(self.device_path, *parts)

    def hwmon(self, name: str) -> str | None:
        if not self.hwmon_path:
            return None
        candidate = os.path.join(self.hwmon_path, name)
        return candidate if os.path.exists(candidate) else None

    @property
    def is_apu(self) -> bool:
        """Heuristic: discrete GPUs use high PCI device ids (0x66xx Polaris
        through 0x75xx RDNA3), APUs/iGPUs use low ids (0x13xx..0x16xx)."""
        try:
            device_id = int(self.pci_id.split(":")[1], 16)
        except (ValueError, IndexError):
            return False
        return device_id < 0x4000

    @property
    def is_discrete(self) -> bool:
        return not self.is_apu

    @property
    def vram_bytes(self) -> int:
        return sysfs.read_int(os.path.join(self.device_path, "mem_info_vram_total")) or 0

    @property
    def short_label(self) -> str:
        """Compact label like 'card1 (Radeon RX 7600)'."""
        name = self.name or self.pci_id
        return f"{self.card} ({name})"


def _norm_slot(slot: str) -> str:
    """lspci prints '03:00.0', sysfs prints '0000:03:00.0' — normalize both."""
    parts = slot.split(":")
    # "0000:03:00.0" -> ["0000", "03", "00.0"]: drop a 4-digit domain prefix
    if len(parts) == 3 and len(parts[0]) == 4:
        return ":".join(parts[1:])
    return slot


def _lspci_names() -> dict[str, str]:
    """Map normalized PCI slot -> description, using lspci when available."""
    try:
        proc = subprocess.run(
            ["lspci", "-nn"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    mapping: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        # "03:00.0 VGA compatible controller [0300]: <desc> [1002:7480] (rev c0)"
        match = re.match(r"^(\S+).*?\]:\s*(.+?)\s*\[[0-9a-f]{4}:[0-9a-f]{4}\]", line)
        if not match:
            continue
        desc = match.group(2)
        desc = re.sub(r"^Advanced Micro Devices, Inc\. ", "", desc)
        desc = re.sub(r"^\[(AMD/ATI|AMD)\]\s*", "", desc)
        mapping[_norm_slot(match.group(1))] = desc
    return mapping


def _read_uevent(device_path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    uevent = os.path.join(device_path, "uevent")
    try:
        with open(uevent) as handle:
            for line in handle:
                if "=" in line:
                    key, _, value = line.partition("=")
                    values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def discover() -> list[Gpu]:
    """Return every amdgpu-backed DRM card on the system."""
    names = _lspci_names()
    gpus: list[Gpu] = []
    for card_dir in glob.glob("/sys/class/drm/card*"):
        card = os.path.basename(card_dir)
        if not re.fullmatch(r"card\d+", card):
            continue  # skip connectors like card1-DP-1
        device_path = os.path.join(card_dir, "device")
        if not os.path.isdir(device_path):
            continue
        uevent = _read_uevent(device_path)
        if uevent.get("DRIVER") != "amdgpu":
            continue
        pci_slot = uevent.get("PCI_SLOT_NAME", "")
        pci_id = uevent.get("PCI_ID", "")
        hwmon_path = None
        for candidate in sorted(glob.glob(os.path.join(device_path, "hwmon", "hwmon*"))):
            try:
                with open(os.path.join(candidate, "name")) as handle:
                    if handle.read().strip() == "amdgpu":
                        hwmon_path = candidate
                        break
            except OSError:
                continue
        boot_vga = False
        try:
            with open(os.path.join(device_path, "boot_vga")) as handle:
                boot_vga = handle.read().strip() == "1"
        except OSError:
            pass
        gpus.append(
            Gpu(
                card=card,
                device_path=device_path,
                pci_slot=pci_slot,
                pci_id=pci_id,
                boot_vga=boot_vga,
                driver="amdgpu",
                name=names.get(_norm_slot(pci_slot), ""),
                hwmon_path=hwmon_path,
            )
        )
    gpus.sort(key=lambda g: int(g.card[4:]))
    return gpus


def pick(gpus: list[Gpu], card: str | None = None) -> Gpu | None:
    """Choose the target GPU.

    Order: explicit --card, else a discrete GPU (APU heuristic), else the GPU
    with the most reported VRAM, else the first one with sensors.
    """
    if not gpus:
        return None
    if card:
        wanted = card if card.startswith("card") else f"card{card}"
        for gpu in gpus:
            if gpu.card == wanted:
                return gpu
        return None

    candidates = [g for g in gpus if g.hwmon_path] or list(gpus)
    if len(candidates) == 1:
        return candidates[0]
    discrete = [g for g in candidates if not g.is_apu]
    pool = discrete or candidates
    pool.sort(key=lambda g: g.vram_bytes, reverse=True)
    return pool[0]
