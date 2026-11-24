"""amdoc — command line interface for the AMD GPU overclocking toolkit."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, control, doctor, monitor, od, output, profiles, sensors
from .gpu import Gpu, discover, pick
from .sysfs import SysfsError

EXAMPLES = """\
examples:
  amdoc                          status overview (same as 'amdoc status')
  amdoc status --json            machine-readable status
  amdoc monitor -i 1             live sensor readout, 1 sample/second
  amdoc monitor -d 60 --csv gpu.csv   log one minute to gpu.csv
  amdoc clocks                   overdrive table + DPM states
  amdoc sclk 500 2600            set core clock range (requires overdrive)
  amdoc mclk 125 2350            set memory clock range
  amdoc sclk --pin 2             pin sclk to DPM state 2 (no-overdrive fallback)
  amdoc fan set 180              manual fan, pwm 180/255
  amdoc fan curve --points "40:30,60:70,80:100"
  amdoc power 170                raise the power limit to 170 W
  amdoc profile save gaming      capture current settings
  amdoc profile apply gaming
  amdoc reset                    restore firmware defaults
  amdoc doctor                   diagnose overdrive/fan/power availability
"""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _resolve(args) -> tuple[list[Gpu], Gpu]:
    gpus = discover()
    if not gpus:
        raise SysfsError(
            "no AMD GPU found — is the amdgpu driver loaded? "
            "(check: lspci -k | grep -A3 VGA)"
        )
    gpu = pick(gpus, getattr(args, "card", None))
    if gpu is None:
        found = ", ".join(g.card for g in gpus)
        raise SysfsError(f"card '{args.card}' not found (available: {found})")
    return gpus, gpu


def _od_summary(gpu: Gpu) -> str:
    if not od.available(gpu):
        return "locked — pp_od_clk_voltage not exposed (run: amdoc doctor)"
    table = od.read_table(gpu)
    parts = []
    for section, label in (("SCLK", "SCLK"), ("MCLK", "MCLK")):
        rng = table.range_for(section)
        values = table.values(section)
        if rng:
            text = f"{label} {rng.lo:g}-{rng.hi:g} MHz"
            if len(values) >= 2:
                text = f"{label} {values[0]:g}-{values[1]:g} MHz (limit {rng.lo:g}-{rng.hi:g})"
            parts.append(text)
    return " · ".join(parts) if parts else "available"


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_status(args) -> int:
    _gpus, gpu = _resolve(args)
    snap = sensors.collect(gpu)

    if getattr(args, "json", False):
        payload = {
            "tool": "amdoc",
            "version": __version__,
            "gpu": {
                "card": gpu.card,
                "name": gpu.name,
                "pci_slot": gpu.pci_slot,
                "pci_id": gpu.pci_id,
                "boot_vga": gpu.boot_vga,
                "driver": gpu.driver,
                "overdrive_available": od.available(gpu),
            },
            "overdrive": None,
            "sensors": {
                "temps_c": snap.temps,
                "crits_c": snap.crits,
                "clocks_mhz": snap.clocks,
                "fan": {
                    "rpm": snap.fan_rpm,
                    "pwm": snap.fan_pwm,
                    "pwm_enable": snap.fan_pwm_enable,
                },
                "power_w": snap.power_w,
                "power_cap_w": snap.power_cap_w,
                "vddgfx_v": snap.vddgfx_v,
                "gpu_busy_pct": snap.gpu_busy,
                "mem_busy_pct": snap.mem_busy,
                "vram_total_bytes": snap.vram_total_b,
                "vram_used_bytes": snap.vram_used_b,
            },
            "power_limit": control.power_info(gpu),
            "perf_level": snap.perf_level,
            "dpm": {
                domain: [
                    {"index": i, "mhz": m, "active": a} for i, m, a in states
                ]
                for domain, states in snap.dpm.items()
            },
        }
        if od.available(gpu):
            table = od.read_table(gpu)
            payload["overdrive"] = {
                "sclk": table.values("SCLK"),
                "mclk": table.values("MCLK"),
                "ranges": {
                    label: {"lo": rng.lo, "hi": rng.hi, "unit": rng.unit}
                    for label, rng in table.ranges.items()
                },
                "raw": table.raw.strip(),
            }
        print(json.dumps(payload, indent=2))
        return 0

    name = gpu.name or gpu.pci_id
    print(f"{output.bold('amdoc')} {__version__} · AMD GPU overclocking toolkit")
    print(f"{output.bold(gpu.card)}  {name} @ {gpu.pci_slot} [{gpu.pci_id}]")
    rows: list[tuple[str, str]] = []
    rows.append(("driver", gpu.driver))
    od_text = _od_summary(gpu)
    if od.available(gpu):
        rows.append(("overdrive", output.green(od_text)))
    else:
        rows.append(("overdrive", output.yellow(od_text)))
    rows.append(
        (
            "vram",
            f"{sensors.format_vram(snap.vram_total_b)}"
            + (
                f" ({sensors.format_vram(snap.vram_used_b)} in use)"
                if snap.vram_used_b
                else ""
            ),
        )
    )
    if snap.link_speed and snap.link_width:
        rows.append(("link", f"{snap.link_speed} {snap.link_width}"))
    if snap.perf_level:
        rows.append(("perf level", snap.perf_level))

    clock_parts = []
    for domain in ("sclk", "mclk"):
        actual = snap.clocks.get(domain)
        if actual is None:
            continue
        text = f"{domain} {actual:.0f} MHz"
        state = sensors.dpm_active_state(snap.dpm, domain)
        if state:
            text += f" ({state})"
        clock_parts.append(text)
    if clock_parts:
        rows.append(("clocks", " · ".join(clock_parts)))

    if snap.temps:
        temp_bits = []
        for label, value in snap.temps.items():
            if label.lower() == "junction":
                temp_bits.append(f"junction {output.temp_color(value)}")
            else:
                temp_bits.append(f"{label} {value:.0f}C")
        limits = " · ".join(f"{v:.0f}C" for v in snap.crits.values())
        text = " · ".join(temp_bits)
        if limits:
            text += f"  (limits {limits})"
        rows.append(("temps", text))

    if snap.fan_pwm is None and snap.fan_rpm is None and snap.fan_pwm_enable is None:
        rows.append(("fan", "no fan sensor"))
    else:
        fan_mode = control.fan_mode(
            {"pwm": snap.fan_pwm, "enable": snap.fan_pwm_enable, "rpm": snap.fan_rpm}
        )
        fan_bits = [fan_mode]
        if snap.fan_pwm is not None:
            fan_bits.append(f"pwm {snap.fan_pwm}/255")
        if snap.fan_rpm is not None:
            fan_bits.append(f"{snap.fan_rpm} rpm")
        rows.append(("fan", " · ".join(fan_bits)))

    power = control.power_info(gpu)
    power_bits = []
    if snap.power_w is not None:
        power_bits.append(f"{snap.power_w:.1f} W now")
    if power["cap"] is not None:
        text = f"cap {power['cap']:g} W"
        if power["default"] is not None:
            text += f" (default {power['default']:g} W"
            if power["min"] is not None and power["max"] is not None and power["min"] != power["max"]:
                text += f", range {power['min']:g}-{power['max']:g} W"
            text += ")"
        power_bits.append(text)
    if power_bits:
        rows.append(("power", " · ".join(power_bits)))

    load_bits = []
    if snap.gpu_busy is not None:
        load_bits.append(f"gpu {snap.gpu_busy}%")
    if snap.mem_busy is not None:
        load_bits.append(f"mem {snap.mem_busy}%")
    if snap.vddgfx_v is not None:
        load_bits.append(f"vddgfx {snap.vddgfx_v:.3f} V")
    if load_bits:
        rows.append(("load", " · ".join(load_bits)))

    width = max(len(key) for key, _ in rows)
    for key, value in rows:
        print(f"  {output.dim(key.ljust(width))}  {value}")
    return 0


def cmd_monitor(args) -> int:
    _gpus, gpu = _resolve(args)
    return monitor.run(
        gpu,
        interval=args.interval,
        count=args.count,
        duration=args.duration,
        csv_path=args.csv,
    )


def _print_dpm(gpu: Gpu, snap: sensors.Snapshot) -> None:
    for domain in ("sclk", "mclk"):
        states = snap.dpm.get(domain)
        if not states:
            continue
        print(f"{domain} DPM states:")
        for index, mhz, active in states:
            marker = f"  {output.green('<- active')}" if active else ""
            label = f"{index:>2}" if index >= 0 else " S"
            print(f"  {label}: {mhz:>7.0f} MHz{marker}")


def cmd_clocks(args) -> int:
    _gpus, gpu = _resolve(args)
    if od.available(gpu):
        print(f"overdrive table ({gpu.od_path}):")
        print(od.read_table(gpu).raw.rstrip())
    else:
        print(
            output.yellow(
                "overdrive table unavailable (pp_od_clk_voltage not exposed) — "
                "run 'amdoc doctor' for the fix"
            )
        )
    print()
    snap = sensors.collect(gpu)
    _print_dpm(gpu, snap)
    print(f"performance level: {snap.perf_level or 'unknown'}")
    if not od.available(gpu):
        print(
            "hint: without overdrive you can still pin clocks with "
            "'amdoc sclk --pin <state>'"
        )
    return 0


def _clock_command(args, domain: str) -> int:
    _gpus, gpu = _resolve(args)
    letter = "s" if domain == "sclk" else "m"
    setter = od.set_sclk if domain == "sclk" else od.set_mclk

    if args.pin is not None:
        control.pin_state(gpu, domain, args.pin)
        print(f"pinned {domain} to DPM state {args.pin} (performance level: manual)")
        return 0

    if args.reset:
        if od.available(gpu):
            od.reset(gpu)
            print(f"overdrive table reset to firmware defaults ({domain} restored)")
        else:
            control.unpin(gpu)
            print(f"performance level restored to auto ({domain} unpinned)")
        return 0

    values = args.values
    if len(values) == 2:
        if not od.available(gpu):
            raise SysfsError(
                f"cannot set {domain}: overdrive interface not available on this "
                "system — run 'amdoc doctor' to see why and how to enable it"
            )
        lo, hi = int(values[0]), int(values[1])
        setter(gpu, lo, hi)
        print(f"{domain} set to {lo}-{hi} MHz (committed to the driver)")
        return 0
    if len(values) == 1:
        raise SysfsError(f"{domain} needs both a minimum and a maximum (e.g. 500 2600)")

    # show current state
    if od.available(gpu):
        table = od.read_table(gpu)
        values_now = table.values(domain.upper())
        rng = table.range_for(domain.upper())
        if len(values_now) >= 2:
            print(f"{domain}: {values_now[0]:g}-{values_now[1]:g} MHz")
        if rng:
            print(f"firmware limit: {rng.lo:g}-{rng.hi:g} {rng.unit or 'MHz'}")
        print(f"path: {gpu.od_path}")
    else:
        snap = sensors.collect(gpu)
        print(f"{domain} overdrive is unavailable on this system")
        _print_dpm(gpu, snap)
        print(
            f"fallback: restrict clocks to one DPM state with "
            f"'amdoc {domain} --pin <state>'"
        )
    return 0


def cmd_sclk(args) -> int:
    return _clock_command(args, "sclk")


def cmd_mclk(args) -> int:
    return _clock_command(args, "mclk")


def cmd_fan(args) -> int:
    _gpus, gpu = _resolve(args)
    action = args.fan_action or "show"

    if action == "show":
        state = control.fan_state(gpu)
        mode = control.fan_mode(state)
        print(f"fan mode: {mode}")
        if state["pwm"] is not None:
            print(f"pwm: {state['pwm']}/255")
        if state["rpm"] is not None:
            rpm_max = state["rpm_max"] or "?"
            print(f"speed: {state['rpm']} rpm (max {rpm_max})")
        if mode == "manual" and (state["pwm"] or 0) == 0:
            print(output.yellow("warning: manual mode with pwm 0 — fan is stopped"))
        return 0

    if action == "auto":
        control.set_fan_auto(gpu)
        print("fan control set to automatic")
        return 0

    if action == "set":
        pwm = args.pwm
        control.set_fan_manual(gpu, pwm)
        if pwm < 51:
            print(
                output.yellow(
                    f"fan set to pwm {pwm}/255 ({pwm * 100 // 255}%) — "
                    "low airflow, watch temperatures"
                )
            )
        else:
            print(f"fan set to manual pwm {pwm}/255 ({pwm * 100 // 255}%)")
        return 0

    if action == "curve":
        sensor = args.sensor
        if args.points:
            curve = control.Curve.from_spec(args.points, sensor=sensor)
        elif args.profile:
            profile = profiles.load(args.profile)
            spec = profile.get("curve")
            if not spec:
                raise SysfsError(
                    f"profile '{args.profile}' has no fan curve "
                    "(save one with: amdoc profile save NAME --curve 40:30,80:100)"
                )
            curve = control.Curve(
                points=[tuple(p) for p in spec["points"]],
                sensor=spec.get("sensor", sensor),
            )
        else:
            raise SysfsError(
                "fan curve needs --points '40:30,60:70,80:100' or --profile NAME"
            )
        control.run_curve(gpu, curve, interval=args.interval, keep=args.keep)
        return 0

    raise SysfsError(f"unknown fan action '{action}'")


def cmd_power(args) -> int:
    _gpus, gpu = _resolve(args)
    info = control.power_info(gpu)
    if args.watts is None:
        print(f"current cap: {info['cap'] if info['cap'] is not None else 'n/a'} W")
        if info["default"] is not None:
            print(f"default:     {info['default']:g} W")
        if info["min"] is not None and info["max"] is not None:
            if info["min"] == info["max"]:
                print(f"range:       locked at {info['min']:g} W by firmware")
            else:
                print(f"range:       {info['min']:g}-{info['max']:g} W")
        snap = sensors.collect(gpu)
        if snap.power_w is not None:
            print(f"draw now:    {snap.power_w:.1f} W")
        return 0

    before = info["cap"]
    control.set_power_cap(gpu, args.watts)
    print(f"power cap: {before:g} W -> {args.watts:g} W")
    return 0


def cmd_profile(args) -> int:
    action = args.profile_action
    if action == "list":
        paths = profiles.list_profiles()
        if not paths:
            print(f"no profiles yet (directory: {profiles.profiles_dir()})")
            return 0
        for path in paths:
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                print(f"  {path.stem}  (unreadable)")
                continue
            bits = []
            if data.get("sclk"):
                s = data["sclk"]
                bits.append(f"sclk {s['min']}-{s['max']}")
            if data.get("power_cap_w"):
                bits.append(f"{data['power_cap_w']:g} W")
            fan = data.get("fan") or {}
            if fan.get("mode"):
                bits.append(f"fan {fan['mode']}")
            if data.get("curve"):
                bits.append("curve")
            print(f"  {path.stem:<20} {' · '.join(bits) or 'empty'}")
        return 0

    if action == "save":
        _gpus, gpu = _resolve(args)
        curve = None
        if args.curve:
            curve = control.Curve.from_spec(args.curve, sensor=args.sensor)
        path = profiles.save(gpu, args.name, curve=curve)
        print(f"profile '{args.name}' saved to {path}")
        if not od.available(gpu):
            print(
                output.yellow(
                    "note: overdrive is unavailable, so no clock settings were captured"
                )
            )
        return 0

    if action == "show":
        profile = profiles.load(args.name)
        print(json.dumps(profile, indent=2))
        return 0

    if action == "apply":
        _gpus, gpu = _resolve(args)
        actions = profiles.apply(gpu, args.name, dry_run=args.dry_run)
        for line in actions:
            print(f"  {line}" if args.dry_run else f"  ✓ {line}")
        print(f"profile '{args.name}' {'validated' if args.dry_run else 'applied'}")
        return 0

    if action == "rm":
        profiles.remove(args.name)
        print(f"profile '{args.name}' removed")
        return 0

    if action == "autostart":
        if args.disable:
            profiles.autostart_disable()
            print("boot autostart disabled and unit removed")
            return 0
        if args.name:
            path = profiles.autostart_install(args.name)
            print(f"boot autostart enabled for profile '{args.name}' ({path})")
            print("test it with: sudo systemctl start amdoc-oc.service")
            return 0
        status = profiles.autostart_status()
        print(f"installed: {status['installed']}")
        print(f"profile:   {status['profile']}")
        print(f"enabled:   {status['enabled']}")
        print(f"active:    {status['active']}")
        return 0

    raise SysfsError(f"unknown profile action '{action}'")


def cmd_reset(args) -> int:
    _gpus, gpu = _resolve(args)
    wanted = [w.strip() for w in args.what.split(",") if w.strip()]
    unknown = [w for w in wanted if w not in ("od", "fan", "power", "dpm")]
    if unknown:
        raise SysfsError(
            f"unknown reset target(s): {', '.join(unknown)} "
            "(choose from: od, fan, power, dpm)"
        )

    if "od" in wanted:
        if od.available(gpu):
            od.reset(gpu)
            print("overdrive table reset to firmware defaults")
        else:
            print("overdrive table not available — skipped")

    if "fan" in wanted:
        control.set_fan_auto(gpu)
        print("fan control restored to automatic")

    if "power" in wanted:
        info = control.power_info(gpu)
        if info["default"] is not None:
            control.set_power_cap(gpu, info["default"])
            print(f"power cap restored to default ({info['default']:g} W)")
        else:
            print("power cap default unknown — skipped")

    if "dpm" in wanted:
        level = control.get_perf_level(gpu)
        if level != "auto":
            control.unpin(gpu)
            print("performance level restored to auto")
        else:
            print("performance level already auto")

    print("done — GPU back to stock behaviour")
    return 0


def cmd_doctor(args) -> int:
    _gpus, gpu = _resolve(args)
    return doctor.run(gpu)


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amdoc",
        description=(
            "AMD GPU overclocking, fan control and monitoring toolkit "
            "(amdgpu sysfs, zero dependencies)"
        ),
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"amdoc {__version__}")
    parser.add_argument(
        "-c", "--card", metavar="CARD",
        help="target GPU: card0, card1, ... (default: the discrete GPU)",
    )
    parser.add_argument(
        "--json", action="store_true", help="machine-readable output (status)"
    )
    parser.add_argument(
        "--no-color", action="store_true", help="disable ANSI colors"
    )

    sub = parser.add_subparsers(dest="command")

    p_status = sub.add_parser("status", help="show GPU status (default)")
    p_status.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS,
        help="machine-readable output",
    )
    p_status.set_defaults(handler=cmd_status)

    p_mon = sub.add_parser("monitor", help="live sensor readout / CSV logging")
    p_mon.add_argument("-i", "--interval", type=float, default=1.0,
                       help="seconds between samples (default 1.0)")
    p_mon.add_argument("-n", "--count", type=int, default=None,
                       help="stop after N samples")
    p_mon.add_argument("-d", "--duration", type=float, default=None,
                       help="stop after SECONDS")
    p_mon.add_argument("--csv", metavar="PATH", default=None,
                       help="append samples to a CSV file")
    p_mon.set_defaults(handler=cmd_monitor)

    sub.add_parser("clocks", help="overdrive table and DPM states").set_defaults(
        handler=cmd_clocks
    )

    for domain, handler in (("sclk", cmd_sclk), ("mclk", cmd_mclk)):
        p = sub.add_parser(domain, help=f"show or set the {domain} range")
        p.add_argument("values", nargs="*", type=int, metavar="MHZ",
                       help="min max (e.g. 500 2600)")
        p.add_argument("--pin", type=int, metavar="STATE",
                       help="pin to a DPM state instead (no-overdrive fallback)")
        p.add_argument("--reset", action="store_true",
                       help="restore firmware defaults / unpin")
        p.set_defaults(handler=handler)

    p_fan = sub.add_parser("fan", help="fan control")
    fan_sub = p_fan.add_subparsers(dest="fan_action")
    fan_sub.add_parser("show", help="current fan state (default)")
    fan_sub.add_parser("auto", help="return fan control to the driver")
    p_fan_set = fan_sub.add_parser("set", help="set a fixed fan speed")
    p_fan_set.add_argument("pwm", type=int, help="pwm value 0-255")
    p_fan_curve = fan_sub.add_parser("curve", help="run a temperature->fan curve")
    p_fan_curve.add_argument("--points", metavar="SPEC",
                             help="curve like '40:30,60:70,80:100' (temp:pwm percent)")
    p_fan_curve.add_argument("--profile", metavar="NAME",
                             help="load the curve from a saved profile")
    p_fan_curve.add_argument("--sensor", default="junction",
                             choices=["edge", "junction", "mem"],
                             help="temperature sensor to follow (default junction)")
    p_fan_curve.add_argument("-i", "--interval", type=float, default=2.0,
                             help="seconds between updates (default 2)")
    p_fan_curve.add_argument("--keep", action="store_true",
                             help="leave the fan manual on exit")
    p_fan.set_defaults(handler=cmd_fan)

    p_power = sub.add_parser("power", help="show or set the power limit")
    p_power.add_argument("watts", nargs="?", type=float, metavar="W",
                         help="new power limit in watts")
    p_power.set_defaults(handler=cmd_power)

    p_prof = sub.add_parser("profile", help="save/apply overclocking profiles")
    prof_sub = p_prof.add_subparsers(dest="profile_action")
    p_save = prof_sub.add_parser("save", help="capture current settings")
    p_save.add_argument("name")
    p_save.add_argument("--curve", metavar="SPEC",
                        help="also store a fan curve, e.g. 40:30,80:100")
    p_save.add_argument("--sensor", default="junction",
                        help="curve sensor (default junction)")
    p_apply = prof_sub.add_parser("apply", help="apply a saved profile")
    p_apply.add_argument("name")
    p_apply.add_argument("--dry-run", action="store_true",
                         help="validate and list actions without writing")
    prof_sub.add_parser("list", help="list saved profiles")
    p_show = prof_sub.add_parser("show", help="print a profile as JSON")
    p_show.add_argument("name")
    p_rm = prof_sub.add_parser("rm", help="delete a profile")
    p_rm.add_argument("name")
    p_auto = prof_sub.add_parser("autostart", help="apply a profile at boot via systemd")
    p_auto.add_argument("name", nargs="?", help="profile to apply at boot")
    p_auto.add_argument("--disable", action="store_true",
                        help="disable and remove the boot unit")
    p_prof.set_defaults(handler=cmd_profile)

    p_reset = sub.add_parser("reset", help="restore stock behaviour")
    p_reset.add_argument("--what", default="od,fan,power,dpm",
                         help="comma list of: od,fan,power,dpm (default all)")
    p_reset.set_defaults(handler=cmd_reset)

    sub.add_parser("doctor", help="diagnose availability and how to enable it").set_defaults(
        handler=cmd_doctor
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output.set_color(False if args.no_color else None)
    handler = getattr(args, "handler", None) or cmd_status
    try:
        return handler(args) or 0
    except SysfsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
