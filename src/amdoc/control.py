"""Fan, power limit and DPM state control via hwmon / amdgpu sysfs."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

from . import sensors, sysfs
from .gpu import Gpu

# hwmon pwm1_enable values (kernel Documentation/hwmon)
FAN_OFF = 0
FAN_MANUAL = 1
FAN_AUTO = 2

PERF_LEVELS = ("auto", "low", "high", "manual", "profile_standard",
               "profile_min_sclk", "profile_min_mclk", "profile_peak")


# --------------------------------------------------------------------------
# Fan
# --------------------------------------------------------------------------
def fan_state(gpu: Gpu) -> dict[str, int | None]:
    return {
        "pwm": sysfs.read_int(gpu.hwmon("pwm1") or ""),
        "enable": sysfs.read_int(gpu.hwmon("pwm1_enable") or ""),
        "rpm": sysfs.read_int(gpu.hwmon("fan1_input") or ""),
        "rpm_max": sysfs.read_int(gpu.hwmon("fan1_max") or ""),
    }


def fan_mode(state: dict[str, int | None]) -> str:
    enable = state.get("enable")
    if enable == FAN_MANUAL:
        return "manual"
    if enable == FAN_AUTO:
        return "auto"
    if enable == FAN_OFF:
        return "off"
    return "unknown"


def set_fan_auto(gpu: Gpu) -> None:
    path = gpu.hwmon("pwm1_enable")
    if not path:
        raise sysfs.SysfsError("fan control interface (pwm1_enable) not available")
    sysfs.write_value(path, str(FAN_AUTO))


def set_fan_manual(gpu: Gpu, pwm: int, verify: bool = True) -> None:
    if not 0 <= pwm <= 255:
        raise sysfs.SysfsError(f"fan pwm must be 0-255, got {pwm}")
    enable = gpu.hwmon("pwm1_enable")
    target = gpu.hwmon("pwm1")
    if not enable or not target:
        raise sysfs.SysfsError("fan control interface (pwm1/pwm1_enable) not available")
    sysfs.write_value(enable, str(FAN_MANUAL))
    sysfs.write_value(target, str(pwm))
    if verify:
        readback = sysfs.read_int(target)
        if readback is not None and readback != pwm:
            raise sysfs.SysfsError(
                f"fan pwm readback mismatch: wrote {pwm}, read {readback}"
            )


# --------------------------------------------------------------------------
# Power limit
# --------------------------------------------------------------------------
def power_info(gpu: Gpu) -> dict[str, float | None]:
    def micro(path_name: str) -> float | None:
        path = gpu.hwmon(path_name)
        if not path:
            return None
        raw = sysfs.read_float(path)
        return None if raw is None else raw / 1e6

    return {
        "cap": micro("power1_cap"),
        "default": micro("power1_cap_default"),
        "min": micro("power1_cap_min"),
        "max": micro("power1_cap_max"),
    }


def set_power_cap(gpu: Gpu, watts: float) -> None:
    path = gpu.hwmon("power1_cap")
    if not path:
        raise sysfs.SysfsError("power cap interface (power1_cap) not available")
    info = power_info(gpu)
    if info["min"] is not None and watts < info["min"] - 0.001:
        raise sysfs.SysfsError(
            f"{watts:g} W is below the minimum power limit of {info['min']:g} W"
        )
    if info["max"] is not None and watts > info["max"] + 0.001:
        raise sysfs.SysfsError(
            f"{watts:g} W is above the maximum power limit of {info['max']:g} W"
        )
    sysfs.write_value(path, str(int(round(watts * 1_000_000))))
    readback = sysfs.read_float(path)
    # The driver rounds power caps to its own granularity (e.g. writing 180 W
    # on Navi 33 reads back as 179 W), so allow a small rounding margin while
    # still catching genuine mismatches.
    if readback is not None and abs(readback / 1e6 - watts) > 2.0:
        raise sysfs.SysfsError(
            f"power cap readback mismatch: wrote {watts:g} W, "
            f"driver reports {readback / 1e6:g} W"
        )


# --------------------------------------------------------------------------
# Performance level / DPM state pinning
# --------------------------------------------------------------------------
def get_perf_level(gpu: Gpu) -> str:
    try:
        return sysfs.read_text(gpu.path("power_dpm_force_performance_level")).strip()
    except sysfs.SysfsError:
        return "unknown"


def set_perf_level(gpu: Gpu, level: str) -> None:
    if level not in PERF_LEVELS:
        raise sysfs.SysfsError(
            f"invalid performance level '{level}' (choose from: {', '.join(PERF_LEVELS)})"
        )
    sysfs.write_value(gpu.path("power_dpm_force_performance_level"), level)


def dpm_states(gpu: Gpu, domain: str) -> list[tuple[int, float, bool]]:
    """Available DPM states for 'sclk' or 'mclk' as [(index, MHz, active)]."""
    return sensors.read_dpm(gpu.path(f"pp_dpm_{domain}"))


def pin_state(gpu: Gpu, domain: str, index: int) -> None:
    """Restrict a clock domain to a single DPM state (manual mode).

    This is the fallback when the overdrive table is unavailable: it does not
    change clocks, but it prevents downclocking (useful for sustained loads).
    """
    if domain not in ("sclk", "mclk"):
        raise sysfs.SysfsError(f"unsupported DPM domain '{domain}'")
    states = dpm_states(gpu, domain)
    valid = [i for i, _mhz, _active in states if i >= 0]
    if index not in valid:
        raise sysfs.SysfsError(
            f"state {index} does not exist for {domain} "
            f"(available: {', '.join(str(v) for v in valid) or 'none'})"
        )
    set_perf_level(gpu, "manual")
    sysfs.write_value(gpu.path(f"pp_dpm_{domain}"), str(index))
    readback = [i for i, _m, active in dpm_states(gpu, domain) if active]
    if readback and index not in readback:
        raise sysfs.SysfsError(
            f"pin readback mismatch: asked for state {index}, active {readback}"
        )


def unpin(gpu: Gpu) -> None:
    """Return clock selection to the driver ('auto')."""
    set_perf_level(gpu, "auto")


# --------------------------------------------------------------------------
# Fan curve
# --------------------------------------------------------------------------
@dataclass
class Curve:
    """Temperature -> fan curve. Points are (deg C, pwm percent 0-100)."""

    points: list[tuple[float, float]]
    sensor: str = "junction"

    def __post_init__(self) -> None:
        if not self.points:
            raise sysfs.SysfsError("fan curve needs at least one point")
        cleaned: list[tuple[float, float]] = []
        for temp, pwm in self.points:
            if not 0 <= pwm <= 100:
                raise sysfs.SysfsError(f"fan curve pwm must be 0-100%, got {pwm:g}%")
            cleaned.append((float(temp), float(pwm)))
        cleaned.sort(key=lambda pt: pt[0])
        self.points = cleaned

    @classmethod
    def from_spec(cls, spec: str, sensor: str = "junction") -> "Curve":
        """Parse '40:30,60:75,80:100' (temp:pwm-percent pairs)."""
        points: list[tuple[float, float]] = []
        for chunk in spec.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ":" not in chunk:
                raise sysfs.SysfsError(
                    f"bad fan curve point '{chunk}' (expected temp:pwm like 60:75)"
                )
            temp_s, _, pwm_s = chunk.partition(":")
            try:
                points.append((float(temp_s), float(pwm_s)))
            except ValueError:
                raise sysfs.SysfsError(f"bad fan curve point '{chunk}'")
        if not points:
            raise sysfs.SysfsError("empty fan curve")
        return cls(points=points, sensor=sensor)

    def pwm_percent(self, temp: float) -> float:
        pts = self.points
        if temp <= pts[0][0]:
            return pts[0][1]
        if temp >= pts[-1][0]:
            return pts[-1][1]
        for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
            if t0 <= temp <= t1:
                if t1 == t0:
                    return p1
                frac = (temp - t0) / (t1 - t0)
                return p0 + frac * (p1 - p0)
        return pts[-1][1]

    def spec(self) -> str:
        return ",".join(f"{t:g}:{p:g}" for t, p in self.points)


def run_curve(
    gpu: Gpu,
    curve: Curve,
    interval: float = 2.0,
    keep: bool = False,
) -> None:
    """Apply a fan curve until Ctrl+C. Restores auto mode on exit unless --keep."""
    print(
        f"applying fan curve ({curve.spec()}) on {curve.sensor} every "
        f"{interval:g}s — Ctrl+C to stop",
        flush=True,
    )
    applied_pwm: int | None = None
    samples = 0
    try:
        while True:
            snap = sensors.collect(gpu)
            temp = snap.temp(curve.sensor)
            if temp is None:
                # fall back to whatever temperature is available
                hottest = snap.hottest
                if hottest is None:
                    raise sysfs.SysfsError(
                        f"no temperature sensor named '{curve.sensor}' "
                        f"(available: {', '.join(snap.temps) or 'none'})"
                    )
                temp = hottest[1]
            crit = snap.crits.get(curve.sensor) or max(
                snap.crits.values(), default=110.0
            )
            if temp >= crit - 10:
                pwm_percent, forced = 100.0, True
            else:
                pwm_percent, forced = curve.pwm_percent(temp), False
            pwm = int(round(pwm_percent * 255 / 100))
            if pwm != applied_pwm:
                set_fan_manual(gpu, pwm, verify=(applied_pwm is None))
                applied_pwm = pwm
            stamp = time.strftime("%H:%M:%S")
            flag = " CRITICAL - full speed" if forced else ""
            print(
                f"{stamp}  {curve.sensor} {temp:5.1f}C -> fan {pwm_percent:3.0f}% "
                f"(pwm {pwm:3d}/255, {snap.fan_rpm or 0} rpm){flag}"
            )
            samples += 1
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        if keep:
            print(f"curve left running (pwm {applied_pwm}) — restore with: amdoc fan auto")
        else:
            try:
                set_fan_auto(gpu)
                print("fan control restored to automatic")
            except sysfs.SysfsError as exc:
                print(f"warning: could not restore auto fan mode: {exc}", file=sys.stderr)
        if samples:
            print(f"applied {samples} curve updates")
