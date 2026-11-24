# AGENTS.md — amd-gpu-oc-toolkit

Project-specific rules for working on this repo. The global rules in `~/AGENTS.md` apply too (project files live in this directory only; save decisions here so they survive context limits).

## What this is

A stdlib-only Python CLI (`amdoc`) that overclocks/underclocks AMD GPUs via `amdgpu` sysfs:

- `pp_od_clk_voltage` — SCLK/MCLK min/max clocks, voltage curve points
- `pwm1` + `pp_dpm_fan` — manual fan PWM or a 5-point temp→fan% curve (or hwmon `fan*_curve`)
- `power_dpm_force_performance_level` + `power1_*_limit` — power limit
- `power_dpm_force_performance_level` — DPM state pinning (`low`/`mid`/`high`)
- `pp_dpm_sclk` / `pp_dpm_mclk` — read-only clock/voltage sensor dump

## Hard rules

- **Stdlib only.** No third-party dependencies ever — not even for tests. Python ≥ 3.9.
- **Root gate:** write commands must use `require_root()` (with `sudo_hint`) and read-back-verify after every write (`verify_readback`). Never drop the `dpm` level back to `auto` before verification.
- **Friendly errors:** user-facing failures raise `SysfsError`; `main()` catches it and prints "error: ...". Never let a traceback reach the user for an expected failure (missing sysfs node, permission, parse).
- **GPU writes are dangerous.** Default card must be the dGPU (PCI id ≥ `0x4000` and largest VRAM — `is_apu()`/`pick()`), never an APU. `doctor` is read-only and exits 0 even with warnings.
- Tests are unittest, run with `PYTHONPATH=src python -m unittest discover -s tests -t .` from the repo root.

## Verified facts (from kernel source, `amdgpu_pm.c`)

- `pp_od_clk_voltage` write commands: `s 0 <mhz>` (min SCLK), `s 1 <mhz>` (max SCLK),
  `m <index> <mhz>` (MCLK), `vc <index> <mhz> <uv>` (curve point), `vo <uv>` (max voltage),
  `c` (commit), `r` (reset). Parse errors return `-EINVAL` — invalid lines must be *rejected*.
- `pp_od_clk_voltage` only exists when `adev->pm.pp_feature & PP_OVERDRIVE_MASK (0x4000)`.
  amdgpu default `ppfeaturemask=0xfffd3fff` excludes that bit.
- `pp_dpm_fan` accepts `manual` or `auto`; writing `manual` switches to user-controlled fan.
- hwmon fan curve: `fan*_curve` with pairs `%`/`°C`; `auto_curve` is read-only.
- `pp_dpm_sclk`/`pp_dpm_mclk` list states as `0: <MHz> *` — the `*` marks the active one.
- Bootloader detection must prefer real systemd-boot artefacts (`/boot/loader/loader.conf`,
  `/boot/EFI/Linux`) over `/etc/default/grub` — GRUB's template can be installed on a
  systemd-boot machine. UKI kernel args live in `/etc/kernel/cmdline` and are baked in with
  `mkinitcpio -P`; type-1 entries use `/boot/loader/entries/*.conf` `options` lines.

## Layout

```
src/amdoc/
  sysfs.py      low-level read/write, parse helpers, SysfsError, require_root
  gpu.py        card discovery, dGPU pick, feature/mask detection
  sensors.py    structured sysfs reads → dicts (status, power, fan, clocks, pstate)
  output.py     JSON/CSV/text renderers
  od.py         overdrive file parse/edit (pure logic, fully unit-tested)
  control.py    apply/reset overdrive + verification; fan curve; power; DPM pin
  profiles.py   JSON save/apply/validate of every tunable (atomic write)
  monitor.py    sampling loop → console table or CSV file
  doctor.py     health check + exact remediation for missing overdrive
  cli.py        argparse subcommands; `main()` returns int, prints user text only
```

Machine-specific notes (not committed) live in `NOTES.md`.
