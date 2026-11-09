"""Overdrive clock/voltage table (pp_od_clk_voltage) parsing and edits.

Write format (verified against drivers/gpu/drm/amd/pm/amdgpu/amdgpu_pm.c):

    s <0|1> <mhz>            set sclk minimum (0) or maximum (1)
    m <0|1> <mhz>            set mclk minimum or maximum
    f <0|1> <mhz>            set fclk minimum or maximum
    vc <point> <mhz> <mv>    set a VDDC curve point
    vo <mv>                  set the vddgfx voltage offset (mV)
    c                        commit the edited table
    r                        reset the table to firmware defaults

amdgpu requires power_dpm_force_performance_level=manual while editing;
this module sets it for the duration of an edit and restores the previous
level afterwards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import control, sysfs
from .gpu import Gpu

_NUM = re.compile(r"(-?\d+(?:\.\d+)?)\s*([A-Za-z%]*)")
_SECTION = re.compile(r"^([A-Za-z][A-Za-z0-9_]*):\s*$")
_POINT = re.compile(r"^(\d+):\s*(.*)$")
_RANGE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*):\s*(.+)$")

# PP_OVERDRIVE_MASK — checked by doctor via the ppfeaturemask parameter
OVERDRIVE_FEATURE_BIT = 0x4000


class OdError(sysfs.SysfsError):
    """Overdrive-specific failure with a user-facing message."""


@dataclass
class OdRange:
    lo: float
    hi: float
    unit: str = ""


@dataclass
class OdTable:
    raw: str
    # section -> [(index, [values...])]  e.g. OD_SCLK -> [(0, [400]), (1, [2200])]
    points: dict[str, list[tuple[int, list[int]]]] = field(default_factory=dict)
    # label -> range, from OD_RANGE (e.g. SCLK -> OdRange(400, 2200, "Mhz"))
    ranges: dict[str, OdRange] = field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> "OdTable":
        table: OdTable = cls(raw=text)
        section = ""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            section_match = _SECTION.match(line)
            if section_match:
                section = section_match.group(1)
                continue
            if section == "OD_RANGE":
                range_match = _RANGE.match(line)
                if range_match:
                    label = range_match.group(1)
                    numbers = _NUM.findall(range_match.group(2))
                    if len(numbers) >= 2:
                        table.ranges[label] = OdRange(
                            lo=float(numbers[0][0]),
                            hi=float(numbers[1][0]),
                            unit=numbers[1][1] or numbers[0][1],
                        )
                continue
            point_match = _POINT.match(line)
            if point_match and section:
                index = int(point_match.group(1))
                values = [int(float(v)) for v, _u in _NUM.findall(point_match.group(2))]
                table.points.setdefault(section, []).append((index, values))
        return table

    @staticmethod
    def _key(mapping: dict, section: str) -> str:
        """Sections are stored as emitted ('OD_SCLK'); accept both spellings."""
        if section in mapping:
            return section
        prefixed = f"OD_{section}"
        return prefixed if prefixed in mapping else section

    def values(self, section: str) -> list[int]:
        """First value of each point in a section (SCLK/MCLK/FCLK style)."""
        entries = self.points.get(self._key(self.points, section), [])
        return [values[0] for _idx, values in entries if values]

    def range_for(self, section: str) -> OdRange | None:
        return self.ranges.get(self._key(self.ranges, section))

    @property
    def sclk_range(self) -> OdRange | None:
        return self.range_for("SCLK")

    @property
    def mclk_range(self) -> OdRange | None:
        return self.range_for("MCLK")

    @property
    def sections(self) -> list[str]:
        return list(self.points.keys())


def available(gpu: Gpu) -> bool:
    return gpu.od_available


def read_table(gpu: Gpu) -> OdTable:
    return OdTable.parse(sysfs.read_text(gpu.od_path))


def _write_od(gpu: Gpu, command: str) -> None:
    sysfs.write_value(gpu.od_path, command)


def _validate_range(name: str, lo: float, hi: float, rng: OdRange | None) -> None:
    if lo >= hi:
        raise OdError(f"{name}: minimum ({lo:g}) must be lower than maximum ({hi:g})")
    if rng is None:
        return  # driver will reject invalid values with EINVAL
    if lo < rng.lo or hi > rng.hi:
        raise OdError(
            f"{name}: {lo:g}-{hi:g} MHz is outside the firmware limit "
            f"{rng.lo:g}-{rng.hi:g} MHz"
        )


def _with_manual(gpu: Gpu, edit) -> None:
    """Run an overdrive edit with perf level forced to manual, then restore."""
    previous = control.get_perf_level(gpu)
    if previous != "manual":
        control.set_perf_level(gpu, "manual")
    try:
        edit()
    finally:
        if previous != "manual":
            try:
                control.set_perf_level(gpu, previous)
            except sysfs.SysfsError:
                pass  # best effort; manual mode still works


def set_clock(
    gpu: Gpu,
    domain: str,
    lo: int,
    hi: int,
    rng: OdRange | None,
    letter: str,
) -> None:
    """Set min/max for 'SCLK' or 'MCLK'. Verifies the readback."""
    _validate_range(domain, lo, hi, rng)

    def edit() -> None:
        _write_od(gpu, f"{letter} 0 {int(lo)}")
        _write_od(gpu, f"{letter} 1 {int(hi)}")
        _write_od(gpu, "c")  # commit

    _with_manual(gpu, edit)

    table = read_table(gpu)
    got = table.values(domain)
    if got and sorted(got) != sorted({int(lo), int(hi)}):
        raise OdError(
            f"{domain} readback mismatch: asked for {int(lo)}-{int(hi)} MHz, "
            f"driver reports {got}"
        )


def set_sclk(gpu: Gpu, lo: int, hi: int) -> None:
    table = read_table(gpu)
    set_clock(gpu, "SCLK", lo, hi, table.sclk_range, "s")


def set_mclk(gpu: Gpu, lo: int, hi: int) -> None:
    table = read_table(gpu)
    set_clock(gpu, "MCLK", lo, hi, table.mclk_range, "m")


def set_curve_point(gpu: Gpu, index: int, mhz: int, mv: int) -> None:
    if index not in (0, 1, 2):
        raise OdError("VDDC curve points are indexed 0, 1 or 2")

    def edit() -> None:
        _write_od(gpu, f"vc {index} {int(mhz)} {int(mv)}")
        _write_od(gpu, "c")

    _with_manual(gpu, edit)


def set_voffset(gpu: Gpu, mv: int) -> None:
    def edit() -> None:
        _write_od(gpu, f"vo {int(mv)}")
        _write_od(gpu, "c")

    _with_manual(gpu, edit)


def reset(gpu: Gpu) -> None:
    """Restore the firmware defaults (write 'r')."""
    _write_od(gpu, "r")
