"""Live sensor monitoring with optional CSV logging."""

from __future__ import annotations

import csv
import os
import time
from datetime import datetime, timezone

from . import output, sensors
from .gpu import Gpu

CSV_FIELDS = [
    "ts_epoch",
    "ts_iso",
    "card",
    "temp_edge_c",
    "temp_junction_c",
    "temp_mem_c",
    "sclk_mhz",
    "mclk_mhz",
    "fan_rpm",
    "fan_pwm",
    "power_w",
    "gpu_busy_pct",
    "mem_busy_pct",
    "vddgfx_v",
]


def _csv_row(snap: sensors.Snapshot, card: str) -> list:
    clocks = snap.clocks
    return [
        f"{snap.ts:.3f}",
        datetime.fromtimestamp(snap.ts, tz=timezone.utc).isoformat(timespec="seconds"),
        card,
        _fmt(snap.temp("edge")),
        _fmt(snap.temp("junction")),
        _fmt(snap.temp("mem")),
        _fmt(clocks.get("sclk")),
        _fmt(clocks.get("mclk")),
        _fmt(snap.fan_rpm),
        _fmt(snap.fan_pwm),
        _fmt(snap.power_w),
        _fmt(snap.gpu_busy),
        _fmt(snap.mem_busy),
        _fmt(snap.vddgfx_v),
    ]


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def _open_csv(path: str):
    needs_header = not os.path.exists(path) or os.path.getsize(path) == 0
    handle = open(path, "a", newline="")
    writer = csv.writer(handle)
    if needs_header:
        writer.writerow(CSV_FIELDS)
        handle.flush()
    return handle, writer


def _line(snap: sensors.Snapshot) -> str:
    stamp = time.strftime("%H:%M:%S", time.localtime(snap.ts))
    temps = []
    for label in ("edge", "junction", "mem"):
        value = snap.temp(label)
        if value is not None:
            colored = output.temp_color(value) if label == "junction" else f"{value:.0f}C"
            temps.append(f"{label} {colored}")
    temps_str = "  ".join(temps) if temps else "no temp sensors"

    clocks = snap.clocks
    sclk = f"{clocks['sclk']:.0f}" if "sclk" in clocks else "?"
    mclk = f"{clocks['mclk']:.0f}" if "mclk" in clocks else "?"

    fan = []
    if snap.fan_rpm is not None:
        fan.append(f"{snap.fan_rpm} rpm")
    if snap.fan_pwm is not None:
        fan.append(f"pwm {snap.fan_pwm}")
    fan_str = " ".join(fan) if fan else "fan n/a"

    power = f"{snap.power_w:.1f} W" if snap.power_w is not None else "? W"
    busy = ""
    if snap.gpu_busy is not None:
        busy = f"  gpu {snap.gpu_busy}%"
        if snap.mem_busy is not None:
            busy += f" mem {snap.mem_busy}%"
    level = f"  [{snap.perf_level}]" if snap.perf_level else ""

    return (
        f"{stamp}  {temps_str} | sclk {sclk:>4} mclk {mclk:>4} MHz "
        f"| {fan_str} | {power}{busy}{level}"
    )


def run(
    gpu: Gpu,
    interval: float = 1.0,
    count: int | None = None,
    duration: float | None = None,
    csv_path: str | None = None,
) -> int:
    """Print sensor samples until Ctrl+C, --count or --duration is reached."""
    if interval <= 0:
        interval = 0.2
    writer = None
    handle = None
    if csv_path:
        handle, writer = _open_csv(csv_path)
        print(f"logging to {csv_path}")

    samples = 0
    junction_vals: list[float] = []
    power_max: float | None = None
    sclk_max: float | None = None
    start = time.time()
    try:
        while True:
            loop_start = time.time()
            snap = sensors.collect(gpu)
            print(_line(snap), flush=True)
            if writer:
                writer.writerow(_csv_row(snap, gpu.card))
                handle.flush()
            samples += 1

            junction = snap.temp("junction") or snap.temp("edge")
            if junction is not None:
                junction_vals.append(junction)
            if snap.power_w is not None:
                power_max = max(power_max or 0, snap.power_w)
            if "sclk" in snap.clocks:
                sclk_max = max(sclk_max or 0, snap.clocks["sclk"])

            if count is not None and samples >= count:
                break
            if duration is not None and time.time() - start >= duration:
                break
            remaining = interval - (time.time() - loop_start)
            if remaining > 0:
                time.sleep(remaining)
    except KeyboardInterrupt:
        print()
    finally:
        if handle:
            handle.close()

    if samples > 1 and junction_vals:
        avg = sum(junction_vals) / len(junction_vals)
        summary = (
            f"\n{samples} samples — junction {min(junction_vals):.0f}/"
            f"{avg:.0f}/{max(junction_vals):.0f} C (min/avg/max)"
        )
        if power_max is not None:
            summary += f", peak power {power_max:.1f} W"
        if sclk_max is not None:
            summary += f", peak sclk {sclk_max:.0f} MHz"
        print(summary)
    return 0
