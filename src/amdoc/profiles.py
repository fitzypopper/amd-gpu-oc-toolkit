"""Named overclocking profiles: capture the current state, save, validate, apply."""

from __future__ import annotations

import json
import os
import pwd
import re
import time
from pathlib import Path
from typing import Any

from . import control, od, sysfs
from .gpu import Gpu

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PROFILE_VERSION = 1


class ProfileError(sysfs.SysfsError):
    pass


def profiles_dir() -> Path:
    override = os.environ.get("AMDOC_PROFILE_DIR")
    if override:
        return Path(override)
    # Writes normally run under sudo, where HOME is /root. Use the invoking
    # user's home so `sudo amdoc profile apply NAME` sees profiles saved as
    # that user.
    home = Path.home()
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        try:
            home = Path(pwd.getpwnam(sudo_user).pw_dir)
        except (KeyError, OSError):
            pass
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    return Path(xdg) / "amd-gpu-oc" / "profiles"


def _profile_path(name: str) -> Path:
    if not NAME_RE.match(name or ""):
        raise ProfileError(
            f"invalid profile name '{name}' (use letters, digits, . _ -)"
        )
    return profiles_dir() / f"{name}.json"


def capture(gpu: Gpu, curve: control.Curve | None = None) -> dict[str, Any]:
    """Snapshot the current GPU state as a profile dict."""
    profile: dict[str, Any] = {
        "version": PROFILE_VERSION,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "gpu": {"card": gpu.card, "pci_id": gpu.pci_id, "name": gpu.name},
        "sclk": None,
        "mclk": None,
        "power_cap_w": None,
        "fan": None,
        "curve": None,
        "perf_level": None,
        "dpm_pin": None,
    }

    if od.available(gpu):
        table = od.read_table(gpu)
        sclk = table.values("SCLK")
        mclk = table.values("MCLK")
        if len(sclk) >= 2:
            profile["sclk"] = {"min": sclk[0], "max": sclk[1]}
        if len(mclk) >= 2:
            profile["mclk"] = {"min": mclk[0], "max": mclk[1]}

    info = control.power_info(gpu)
    profile["power_cap_w"] = info["cap"]

    fan = control.fan_state(gpu)
    mode = control.fan_mode(fan)
    profile["fan"] = {"mode": mode, "pwm": fan["pwm"] if mode == "manual" else None}

    if curve is not None:
        profile["curve"] = {"points": list(curve.points), "sensor": curve.sensor}

    level = control.get_perf_level(gpu)
    profile["perf_level"] = level
    if level == "manual":
        pin: dict[str, int] = {}
        for domain in ("sclk", "mclk"):
            for index, _mhz, active in control.dpm_states(gpu, domain):
                if active and index >= 0:
                    pin[domain] = index
        profile["dpm_pin"] = pin or None

    return profile


def save(gpu: Gpu, name: str, curve: control.Curve | None = None) -> Path:
    path = _profile_path(name)
    profile = capture(gpu, curve=curve)
    profile["name"] = name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2) + "\n")
    return path


def load(name: str) -> dict[str, Any]:
    path = _profile_path(name)
    if not path.exists():
        available = ", ".join(p.stem for p in list_profiles()) or "none"
        raise ProfileError(f"profile '{name}' not found (existing: {available})")
    try:
        profile = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"profile '{name}' is unreadable: {exc}")
    if not isinstance(profile, dict):
        raise ProfileError(f"profile '{name}' is not a valid profile file")
    return profile


def list_profiles() -> list[Path]:
    directory = profiles_dir()
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"), key=lambda p: p.stem)


def remove(name: str) -> None:
    path = _profile_path(name)
    if not path.exists():
        raise ProfileError(f"profile '{name}' not found")
    path.unlink()


def validate(profile: dict[str, Any], gpu: Gpu) -> list[str]:
    """Return a list of problems (empty list means the profile is applicable)."""
    errors: list[str] = []
    if profile.get("version") != PROFILE_VERSION:
        errors.append(f"unsupported profile version {profile.get('version')!r}")
        return errors

    od_table = od.read_table(gpu) if od.available(gpu) else None
    power = control.power_info(gpu)

    for domain in ("sclk", "mclk"):
        spec = profile.get(domain)
        if spec is None:
            continue
        if not isinstance(spec, dict) or "min" not in spec or "max" not in spec:
            errors.append(f"{domain}: malformed clock spec {spec!r}")
            continue
        lo, hi = spec["min"], spec["max"]
        if not isinstance(lo, int) or not isinstance(hi, int) or lo >= hi:
            errors.append(f"{domain}: min ({lo!r}) must be an int below max ({hi!r})")
            continue
        if od_table is None:
            continue  # overdrive unavailable: checked at apply time as a skip
        rng = od_table.range_for(domain.upper())
        if rng and (lo < rng.lo or hi > rng.hi):
            errors.append(
                f"{domain}: {lo}-{hi} MHz outside firmware limit "
                f"{rng.lo:g}-{rng.hi:g} MHz"
            )

    cap = profile.get("power_cap_w")
    if cap is not None:
        if not isinstance(cap, (int, float)):
            errors.append(f"power_cap_w: not a number ({cap!r})")
        else:
            if power["min"] is not None and cap < power["min"] - 0.001:
                errors.append(f"power_cap_w: {cap:g} W below minimum {power['min']:g} W")
            if power["max"] is not None and cap > power["max"] + 0.001:
                errors.append(f"power_cap_w: {cap:g} W above maximum {power['max']:g} W")

    fan = profile.get("fan")
    if fan is not None:
        mode = (fan or {}).get("mode")
        if mode not in ("auto", "manual"):
            errors.append(f"fan: unknown mode {mode!r}")
        elif mode == "manual":
            pwm = fan.get("pwm")
            if not isinstance(pwm, int) or not 0 <= pwm <= 255:
                errors.append(f"fan: pwm must be 0-255, got {pwm!r}")

    curve = profile.get("curve")
    if curve is not None:
        try:
            control.Curve(
                points=[tuple(p) for p in curve.get("points", [])],
                sensor=curve.get("sensor", "junction"),
            )
        except (sysfs.SysfsError, TypeError, ValueError) as exc:
            errors.append(f"curve: {exc}")

    return errors


def apply(gpu: Gpu, name: str, dry_run: bool = False) -> list[str]:
    """Apply a profile. Validates first — no sysfs writes happen on error."""
    profile = load(name)
    errors = validate(profile, gpu)
    if errors:
        raise ProfileError(
            f"profile '{name}' is not applicable:\n  - " + "\n  - ".join(errors)
        )

    actions: list[str] = []

    def do(description: str, action) -> None:
        if dry_run:
            actions.append(f"would {description}")
        else:
            action()
            actions.append(description)

    cap = profile.get("power_cap_w")
    if cap is not None:
        current = control.power_info(gpu)["cap"]
        if current is None or abs(current - float(cap)) > 0.01:
            do(f"power cap -> {cap:g} W", lambda: control.set_power_cap(gpu, float(cap)))
        else:
            actions.append(f"power cap already {cap:g} W")

    if od.available(gpu):
        for domain, setter in (("sclk", od.set_sclk), ("mclk", od.set_mclk)):
            spec = profile.get(domain)
            if spec:
                do(
                    f"{domain} -> {spec['min']}-{spec['max']} MHz",
                    lambda s=spec, fn=setter: fn(gpu, s["min"], s["max"]),
                )
    else:
        for domain in ("sclk", "mclk"):
            if profile.get(domain):
                actions.append(
                    f"skipped {domain} (overdrive unavailable on this system)"
                )

    pin = profile.get("dpm_pin") or {}
    level = profile.get("perf_level")
    if level == "manual" and pin:
        for domain, index in pin.items():
            do(
                f"pin {domain} to DPM state {index}",
                lambda d=domain, i=index: control.pin_state(gpu, d, i),
            )
    elif level == "auto" and control.get_perf_level(gpu) != "auto":
        do("performance level -> auto", lambda: control.unpin(gpu))

    fan = profile.get("fan") or {}
    mode = fan.get("mode")
    if mode == "auto":
        if control.fan_mode(control.fan_state(gpu)) != "auto":
            do("fan -> automatic", lambda: control.set_fan_auto(gpu))
        else:
            actions.append("fan already automatic")
    elif mode == "manual":
        pwm = int(fan.get("pwm") or 0)
        do(
            f"fan -> manual pwm {pwm}/255",
            lambda: control.set_fan_manual(gpu, pwm),
        )

    if profile.get("curve"):
        actions.append(
            "profile contains a fan curve — start it with: "
            f"amdoc fan curve --profile {name}"
        )

    return actions
