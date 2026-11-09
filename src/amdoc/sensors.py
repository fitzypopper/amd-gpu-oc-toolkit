"""Sensor readings from hwmon and amdgpu device attributes."""

from __future__ import annotations

import glob
import os
import time
from dataclasses import dataclass, field

from . import sysfs
from .gpu import Gpu
_LINK_SPEEDS = {1: "2.5 GT/s", 2: "5 GT/s", 3: "8 GT/s", 4: "16 GT/s", 5: "32 GT/s"}


@dataclass
class Snapshot:
    """One consistent read of every available sensor."""

    ts: float
    temps: dict[str, float] = field(default_factory=dict)  # label -> deg C
    crits: dict[str, float] = field(default_factory=dict)  # label -> crit deg C
    clocks: dict[str, float] = field(default_factory=dict)  # label -> MHz
    dpm: dict[str, list[tuple[int, float, bool]]] = field(
        default_factory=dict
    )  # domain -> [(index, MHz, active)]
    fan_rpm: int | None = None
    fan_pwm: int | None = None
    fan_pwm_enable: int | None = None
    power_w: float | None = None
    power_cap_w: float | None = None
    vddgfx_v: float | None = None
    gpu_busy: int | None = None
    mem_busy: int | None = None
    vram_total_b: int | None = None
    vram_used_b: int | None = None
    link_speed: str | None = None
    link_width: str | None = None
    perf_level: str | None = None

    # --- convenience accessors --------------------------------------------
    def temp(self, label: str, default: float | None = None) -> float | None:
        for key, value in self.temps.items():
            if key.lower() == label.lower():
                return value
        return default

    @property
    def hottest(self) -> tuple[str, float] | None:
        if not self.temps:
            return None
        label, value = max(self.temps.items(), key=lambda kv: kv[1])
        return label, value

    @property
    def epoch(self) -> float:
        return self.ts


def _float(path: str) -> float | None:
    return sysfs.read_float(path)


def read_dpm(path: str) -> list[tuple[int, float, bool]]:
    """Parse pp_dpm_sclk style output: '0: 255Mhz', '1: 965Mhz *'."""
    try:
        text = sysfs.read_text(path)
    except sysfs.SysfsError:
        return []
    states: list[tuple[int, float, bool]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        active = line.endswith("*")
        line = line.rstrip("*").strip()
        # forms: "0: 255Mhz" or "S: 19Mhz"
        head, _, rest = line.partition(":")
        digits = "".join(ch for ch in rest if ch.isdigit() or ch == ".")
        if not digits:
            continue
        try:
            mhz = float(digits)
        except ValueError:
            continue
        index = int(head) if head.isdigit() else -1
        states.append((index, mhz, active))
    return states


def collect(gpu: Gpu) -> Snapshot:
    snap = Snapshot(ts=time.time())

    # --- hwmon sensors -----------------------------------------------------
    if gpu.hwmon_path:
        hw = gpu.hwmon_path
        for temp_file in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
            base = temp_file[: -len("_input")]
            label_file = base + "_label"
            try:
                label = open(label_file).read().strip()
            except OSError:
                label = os.path.basename(base)
            value = _float(temp_file)
            if value is not None:
                snap.temps[label] = value / 1000.0
            crit = _float(base + "_crit")
            if crit is not None and crit > -100:
                snap.crits[label] = crit / 1000.0

        for freq_file in sorted(glob.glob(os.path.join(hw, "freq*_input"))):
            base = freq_file[: -len("_input")]
            try:
                label = open(base + "_label").read().strip()
            except OSError:
                label = os.path.basename(base)
            value = _float(freq_file)
            if value is not None:
                snap.clocks[label] = value / 1e6  # kHz -> MHz

        snap.fan_rpm = sysfs.read_int(os.path.join(hw, "fan1_input"))
        snap.fan_pwm = sysfs.read_int(os.path.join(hw, "pwm1"))
        snap.fan_pwm_enable = sysfs.read_int(os.path.join(hw, "pwm1_enable"))

        power = _float(os.path.join(hw, "power1_average"))
        if power is not None:
            snap.power_w = power / 1e6  # uW -> W
        cap = _float(os.path.join(hw, "power1_cap"))
        if cap is not None:
            snap.power_cap_w = cap / 1e6

        vdd = _float(os.path.join(hw, "in0_input"))
        if vdd is not None:
            snap.vddgfx_v = vdd / 1000.0  # mV -> V

    # --- device attributes -------------------------------------------------
    busy = sysfs.read_int(gpu.path("gpu_busy_percent"))
    if busy is not None:
        snap.gpu_busy = busy
    mem_busy = sysfs.read_int(gpu.path("mem_busy_percent"))
    if mem_busy is not None:
        snap.mem_busy = mem_busy
    snap.vram_total_b = sysfs.read_int(gpu.path("mem_info_vram_total"))
    snap.vram_used_b = sysfs.read_int(gpu.path("mem_info_vram_used"))

    level = None
    try:
        level = sysfs.read_text(gpu.path("power_dpm_force_performance_level")).strip()
    except sysfs.SysfsError:
        pass
    snap.perf_level = level

    for domain in ("sclk", "mclk"):
        states = read_dpm(gpu.path(f"pp_dpm_{domain}"))
        if states:
            snap.dpm[domain] = states

    speed = sysfs.read_int(gpu.path("current_link_speed"))
    if speed is not None:
        snap.link_speed = _LINK_SPEEDS.get(speed, f"gen{speed}")
    width = sysfs.read_int(gpu.path("current_link_width"))
    if width is not None:
        snap.link_width = f"x{width}"

    return snap


def format_vram(bytes_value: int | None) -> str:
    if not bytes_value:
        return "n/a"
    gib = bytes_value / (1024**3)
    return f"{gib:.1f} GiB" if gib >= 1 else f"{bytes_value / (1024**2):.0f} MiB"


def dpm_active(domains: dict[str, list[tuple[int, float, bool]]], key: str) -> str:
    """Render '2539 MHz (state 2)' or '-' for a DPM domain."""
    for index, mhz, active in domains.get(key, []):
        if active:
            state = f"state {index}" if index >= 0 else "deep sleep"
            return f"{mhz:g} MHz ({state})"
    return "-"


def dpm_active_state(domains: dict[str, list[tuple[int, float, bool]]], key: str) -> str | None:
    """Name of the active DPM state, e.g. 'state 2' (or 'deep sleep')."""
    for index, _mhz, active in domains.get(key, []):
        if active:
            return "deep sleep" if index < 0 else f"state {index}"
    return None
