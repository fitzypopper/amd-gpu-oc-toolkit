# amd-gpu-oc-toolkit

**`amdoc` — AMD GPU overclocking, fan control and monitoring toolkit for Linux.**

A zero-dependency Python CLI that talks directly to the `amdgpu` driver's sysfs
interface: clock/voltage overdrive, fan curves, power limits, DPM state pinning,
live monitoring with CSV logging, saved profiles — plus a `doctor` command that
tells you *exactly* why an interface is missing and how to enable it.

```
$ amdoc
amdoc 0.1.0 · AMD GPU overclocking toolkit
card1  Navi 33 [Radeon RX 7600 XT] @ 0000:03:00.0 [1002:7480]
  driver      amdgpu
  overdrive   locked — pp_od_clk_voltage not exposed (run: amdoc doctor)
  vram        16.0 GiB (961 MiB in use)
  perf level  auto
  clocks      sclk 479 MHz (state 1) · mclk 96 MHz (state 0)
  temps       edge 34C · junction 36C · mem 37C  (limits 110C · 110C · 108C)
  fan         auto · pwm 0/255 · 0 rpm
  power       14.0 W now · cap 165 W (default 165 W, range 155-165 W)
  load        gpu 6% · mem 1% · vddgfx 0.360 V
```

## Features

| Area | Commands | What it does |
|---|---|---|
| **Status** | `amdoc`, `amdoc status [--json]` | One-page GPU overview: clocks, temps, fan, power, load |
| **Monitoring** | `amdoc monitor [-i] [-n] [-d] [--csv FILE]` | Live readout + CSV logging for benchmarks |
| **Clocks** | `amdoc clocks`, `amdoc sclk`, `amdoc mclk` | Overdrive table viewer + DPM state table |
| **Overclocking** | `amdoc sclk 500 2600`, `amdoc mclk 125 2350` | Set clock ranges (validated against firmware `OD_RANGE`, committed, read back) |
| **No-overdrive fallback** | `amdoc sclk --pin 2` | Pin a clock to one DPM state (blocks downclocking) |
| **Fan** | `amdoc fan show/auto/set/curve` | Fixed speed or a temperature→PWM curve with critical-temp override |
| **Power limit** | `amdoc power [WATTS]` | Read/set `power1_cap`, range-checked |
| **Profiles** | `amdoc profile save/apply/list/show/rm` | Capture & replay full settings, with `--dry-run` validation |
| **Reset** | `amdoc reset [--what od,fan,power,dpm]` | Back to firmware defaults |
| **Diagnostics** | `amdoc doctor` | Explains what's locked and prints the exact fix |

All clock/voltage edits are validated against the firmware-reported range,
committed (`c`) and verified by read-back. Writes require root; reads never do.

## Why is overdrive "locked"? (the important part)

amdgpu only exposes `/sys/class/drm/cardX/device/pp_od_clk_voltage` when the
`PP_OVERDRIVE_MASK` feature bit (`0x4000`) is set in the driver's
`ppfeaturemask`. The amdgpu default mask (`0xfffd3fff`) deliberately leaves it
**off**, so on most systems overclocking is locked out of the box:

```
$ amdoc doctor
  [warn] Overdrive      pp_od_clk_voltage not exposed — clock/voltage tuning locked
                        cause: PP_OVERDRIVE_MASK (0x4000) is not set in the current ppfeaturemask (0xfff7bfff)
                        add  amdgpu.ppfeaturemask=0xfff7ffff  to GRUB_CMDLINE_LINUX_DEFAULT in /etc/default/grub
                        run  sudo grub-mkconfig -o /boot/grub/grub.cfg
                        reboot, then re-check with: amdoc doctor
                        note: once overdrive is enabled the kernel may report an 'out of spec' taint — expected behaviour, not an error
```

`amdoc doctor` computes the corrected mask from your *current* one (ORs in
`0x4000`), detects GRUB vs systemd-boot, and prints copy-pasteable steps. If the
node is still missing after a reboot with the overdrive bit set, the GPU
vBIOS/firmware does not expose overclocking for that board — nothing software
can do; fan control, power limit and monitoring keep working.

## Install

Requires Linux with the `amdgpu` driver and Python ≥ 3.9 (stdlib only — no
third-party packages).

```sh
# from a clone of this repo:
pipx install .            # recommended: isolated CLI tool
# or, if you use pip with --user support:
python -m pip install --user .
# or run straight from the source tree:
PYTHONPATH=src python -m amdoc status
```

Writes to sysfs need root, so most tuning commands are run with `sudo amdoc ...`.
`amdoc` tells you when that's needed — it never escalates on its own.

## Quick start

```sh
amdoc doctor               # 1. what works, what's locked, how to unlock it
amdoc                      # 2. snapshot of the current state
amdoc monitor -i 1         # 3. watch temps/clocks while you load the GPU
amdoc profile save stock   # 4. remember where you started

# with overdrive unlocked:
sudo amdoc sclk 500 2600   # core clock range
sudo amdoc mclk 125 2350   # memory clock range
sudo amdoc power 170       # power limit (W), if your BIOS allows it

# fan curve (works with or without overdrive):
sudo amdoc fan curve --points "40:30,60:70,80:100" --sensor junction

amdoc profile save tuned   # save everything you just set
sudo amdoc reset           # panic button: everything back to stock
```

## Command reference

```
amdoc [options] <command>

options:
  -c, --card CARD   target GPU (card0, card1, ...); default: the discrete GPU
  --json            machine-readable status output
  --no-color        disable ANSI colors

commands:
  status                 one-page overview (default)
  monitor                live sensors: -i interval, -n count, -d duration, --csv FILE
  clocks                 overdrive table + DPM states + performance level
  sclk [MIN MAX]         show or set the core clock range
  sclk --pin N           restrict sclk to DPM state N (manual mode)
  sclk --reset           restore firmware defaults (or unpin)
  mclk [MIN MAX]         same for the memory clock
  fan show|auto|set PWM  fan state / driver control / fixed speed
  fan curve              --points "40:30,60:70,80:100" | --profile NAME
                         --sensor edge|junction|mem  -i SECONDS  --keep
  power [WATTS]          show or set the power limit
  profile save NAME      capture current settings [--curve SPEC]
  profile apply NAME     apply a profile [--dry-run]
  profile list|show|rm
  reset [--what ...]     od,fan,power,dpm (default: all)
  doctor                 diagnostics with exact remediation steps
```

### Fan curve details

Points are `temperature:pwm-percent`, sorted automatically, interpolated
linearly between anchors; below the first anchor the first value is used, above
the last the fan runs at that speed. If the sensor comes within 10 °C of its
critical limit the curve is overridden to 100 %. Ctrl+C restores automatic fan
control (unless `--keep`).

### Profiles

Profiles are plain JSON in `~/.config/amd-gpu-oc/profiles/` (override with
`AMDOC_PROFILE_DIR`), so they're easy to read, edit and version-control:

```json
{
  "version": 1,
  "name": "gaming",
  "sclk": { "min": 500, "max": 2600 },
  "mclk": { "min": 125, "max": 2350 },
  "power_cap_w": 170,
  "fan": { "mode": "manual", "pwm": 180 },
  "curve": { "points": [[40, 30], [60, 70], [80, 100]], "sensor": "junction" },
  "perf_level": "auto"
}
```

`amdoc profile apply` validates everything against the firmware ranges *before*
touching the system, and reports anything it skips (e.g. clock settings when
overdrive is unavailable).

## Safety

- Overclocking can crash your GPU, corrupt a display session, or in the worst
  case stress hardware beyond its design. Start small (+5–10 % clocks), test
  stability under load, watch `amdoc monitor`, and keep the `stock` profile.
- `amdoc` refuses values outside the firmware `OD_RANGE`, verifies every write
  by read-back, and offers `amdoc reset` to return to defaults.
- The fan curve's critical-temperature override (full speed within 10 °C of the
  thermal limit) is intentionally not disableable.
- Enabling overdrive may taint the kernel ("out of spec") on kernels ≥ 6.14 —
  this is a deliberate upstream policy, not a crash.

## How it works

Everything goes through sysfs — no vendor blobs, no daemon:

| Interface | Path | Used for |
|---|---|---|
| Overdrive table | `.../device/pp_od_clk_voltage` | clock min/max, VDDC curve, voltage offset, reset |
| DPM states | `.../device/pp_dpm_sclk`, `pp_dpm_mclk` | state listing + pinning |
| Perf level | `.../device/power_dpm_force_performance_level` | manual/auto switching (required while editing OD) |
| hwmon fan | `.../hwmon/hwmon*/pwm1`, `pwm1_enable`, `fan1_input` | fan control + RPM |
| hwmon power | `.../hwmon/hwmon*/power1_cap*` | power limit |
| hwmon sensors | `temp*_input`, `freq*_input`, `in0_input`, `power1_average` | monitoring |
| Device attrs | `gpu_busy_percent`, `mem_info_vram_total`, ... | load / VRAM / PCIe link |

The overdrive write format follows the amdgpu driver (`s 0 <mhz>` = min,
`s 1 <mhz>` = max, `c` = commit, `r` = reset), matching
`drivers/gpu/drm/amd/pm/amdgpu/amdgpu_pm.c`.

## Development

```sh
python -m unittest discover -s tests -t .    # unit tests (no GPU/root needed)
PYTHONPATH=src python -m amdoc status        # manual check
```

Layout: `src/amdoc/` — `gpu.py` (discovery) · `sensors.py` (readings) ·
`od.py` (overdrive) · `control.py` (fan/power/DPM/curves) ·
`profiles.py` · `monitor.py` · `doctor.py` · `cli.py`.

## License

MIT — see [LICENSE](LICENSE). Not affiliated with AMD; use at your own risk.
