"""System diagnostics: what works, what doesn't, and exactly how to fix it."""

from __future__ import annotations

import glob
import os
import subprocess

from . import control, od, output, sensors
from .gpu import Gpu

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
INFO = "INFO"

_LABELS = {PASS: "ok  ", WARN: "warn", FAIL: "FAIL", INFO: "info"}


def _read_ppfeaturemask() -> int | None:
    try:
        text = open("/sys/module/amdgpu/parameters/ppfeaturemask").read().strip()
        return int(text, 0)
    except (OSError, ValueError):
        return None


def _bootloader_steps(new_mask: str) -> list[str]:
    """Give fix steps for the *active* bootloader.

    Many systems have /etc/default/grub installed even though they boot with
    systemd-boot, so check for actual systemd-boot artefacts first.
    """
    arg = f"amdgpu.ppfeaturemask={new_mask}"
    reboot = "reboot, then re-check with: amdoc doctor"

    systemd_boot = os.path.exists("/boot/loader/loader.conf") or os.path.isdir(
        "/boot/EFI/Linux"
    )
    if systemd_boot:
        # Unified kernel image: the command line lives in /etc/kernel/cmdline
        # and is baked into the .efi by mkinitcpio/ukify.
        if os.path.exists("/etc/kernel/cmdline"):
            steps = [f"add  {arg}  to /etc/kernel/cmdline"]
            if glob.glob("/etc/mkinitcpio.d/*.preset"):
                steps.append("run  sudo mkinitcpio -P  (rebuild the unified kernel image)")
            else:
                steps.append("rebuild your UKI with the new command line (e.g. sudo ukify build)")
            return steps + [reboot]
        entries = sorted(glob.glob("/boot/loader/entries/*.conf"))
        if entries:
            names = ", ".join(os.path.basename(p) for p in entries)
            return [
                f"add  {arg}  to the 'options' line of your systemd-boot entry ({names})",
                reboot,
            ]
        return [f"add  {arg}  to your systemd-boot kernel command line", reboot]

    if os.path.exists("/boot/grub/grub.cfg") or os.path.exists("/etc/default/grub"):
        return [
            f"add  {arg}  to GRUB_CMDLINE_LINUX_DEFAULT in /etc/default/grub",
            "run  sudo grub-mkconfig -o /boot/grub/grub.cfg",
            reboot,
        ]
    return [f"add  {arg}  to your bootloader's kernel command line", reboot]


def _kernel_log_check() -> tuple[str, str]:
    try:
        proc = subprocess.run(["dmesg"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return INFO, "could not run dmesg"
    if proc.returncode != 0:
        return INFO, "kernel log not readable without root (re-run with sudo)"
    interesting = [
        line
        for line in proc.stdout.splitlines()
        if "amdgpu" in line.lower()
        and any(key in line.lower() for key in ("overdrive", "od settings", "pp_od"))
    ]
    if interesting:
        return WARN, "kernel log: " + " | ".join(interesting[-3:])
    return PASS, "no amdgpu overdrive errors in the kernel log"


def run(gpu: Gpu) -> int:
    """Print a diagnostic report. Returns a process exit code (1 on FAIL)."""
    results: list[tuple[str, str, str]] = []

    def add(level: str, title: str, detail: str, *hints: str) -> None:
        results.append((level, title, detail, hints))

    # --- GPU / driver ------------------------------------------------------
    label = gpu.name or gpu.pci_id
    add(PASS, "GPU", f"{gpu.card} · {label} · {gpu.pci_slot} · driver {gpu.driver}")

    # --- sensors -----------------------------------------------------------
    snap = sensors.collect(gpu)
    snap_temps = snap.temps
    if snap_temps:
        reading = " · ".join(f"{k} {v:.0f}C" for k, v in snap_temps.items())
        add(PASS, "Sensors", f"temperatures: {reading}")
    else:
        add(FAIL, "Sensors", "no temperature sensors readable")

    # --- overdrive ---------------------------------------------------------
    if od.available(gpu):
        table = od.read_table(gpu)
        sclk = table.sclk_range
        mclk = table.mclk_range
        parts = []
        if sclk:
            parts.append(f"SCLK {sclk.lo:g}-{sclk.hi:g} MHz")
        if mclk:
            parts.append(f"MCLK {mclk.lo:g}-{mclk.hi:g} MHz")
        add(PASS, "Overdrive", "pp_od_clk_voltage available — " + (" · ".join(parts) or "table readable"))
    else:
        mask = _read_ppfeaturemask()
        if mask is None:
            add(
                WARN,
                "Overdrive",
                "pp_od_clk_voltage not exposed and ppfeaturemask is unreadable",
                "check /proc/cmdline and the amdgpu module parameters",
            )
        elif not mask & od.OVERDRIVE_FEATURE_BIT:
            new_mask = mask | od.OVERDRIVE_FEATURE_BIT
            hints = [
                f"cause: PP_OVERDRIVE_MASK (0x{od.OVERDRIVE_FEATURE_BIT:04x}) is not set "
                f"in the current ppfeaturemask (0x{mask:08x})",
            ] + _bootloader_steps(f"0x{new_mask:08x}") + [
                "note: once overdrive is enabled the kernel may report an 'out of spec' "
                "taint — expected behaviour, not an error",
            ]
            add(
                WARN,
                "Overdrive",
                "pp_od_clk_voltage not exposed — clock/voltage tuning locked",
                *hints,
            )
        else:
            add(
                WARN,
                "Overdrive",
                f"ppfeaturemask has the overdrive bit (0x{mask:08x}) but the interface "
                "is still absent",
                "the GPU vBIOS/firmware likely does not expose overclocking for this "
                "board (amdgpu only shows the node when the SMU reports OD support)",
                "fan control, power limit and monitoring remain available",
            )

    # --- fan ---------------------------------------------------------------
    if gpu.hwmon("pwm1") and gpu.hwmon("pwm1_enable"):
        add(PASS, "Fan control", "pwm1 / pwm1_enable available (0-255, auto/manual)")
    else:
        add(WARN, "Fan control", "pwm1 interface not found — fan curve unavailable")

    # --- power -------------------------------------------------------------
    power = control.power_info(gpu)
    if power["cap"] is None:
        add(WARN, "Power limit", "power1_cap not available")
    elif power["min"] is not None and power["max"] is not None:
        if power["min"] == power["max"]:
            add(INFO, "Power limit", f"locked to {power['cap']:g} W by firmware")
        else:
            add(
                PASS,
                "Power limit",
                f"adjustable {power['min']:g}-{power['max']:g} W "
                f"(current {power['cap']:g} W)",
            )

    # --- thermals ----------------------------------------------------------
    hottest = snap.hottest
    if hottest:
        label, temp = hottest
        crit = snap.crits.get(label)
        if crit and temp >= crit - 15:
            add(WARN, "Temperatures", f"{label} at {temp:.0f}C — near the {crit:.0f}C limit")
        else:
            detail = f"{label} {temp:.0f}C"
            if crit:
                detail += f" (limit {crit:.0f}C)"
            add(PASS, "Temperatures", detail)

    # --- DPM ---------------------------------------------------------------
    states = control.dpm_states(gpu, "sclk")
    if states:
        level = control.get_perf_level(gpu)
        add(
            PASS,
            "DPM states",
            f"{len(states)} sclk states, performance level '{level}'",
        )

    # --- kernel log --------------------------------------------------------
    level, detail = _kernel_log_check()
    add(level, "Kernel log", detail)

    # --- print -------------------------------------------------------------
    print(f"amdoc doctor — {gpu.card} ({gpu.name or gpu.pci_id})")
    counts = {PASS: 0, WARN: 0, FAIL: 0, INFO: 0}
    for level, title, detail, hints in results:
        counts[level] += 1
        tag = _LABELS[level]
        colored_tag = {
            PASS: output.green,
            WARN: output.yellow,
            FAIL: output.red,
            INFO: output.cyan,
        }[level](f"[{tag}]")
        print(f"  {colored_tag} {title:<14} {detail}")
        for hint in hints:
            print(f"         {'':<14} {hint}")
    verdict = "no action needed" if counts[FAIL] == 0 and counts[WARN] == 0 else (
        "fixes available — see warnings above" if counts[FAIL] == 0 else "failures present"
    )
    print(
        f"summary: {counts[PASS]} pass · {counts[WARN]} warnings · "
        f"{counts[FAIL]} failures — {verdict}"
    )
    return 1 if counts[FAIL] else 0
