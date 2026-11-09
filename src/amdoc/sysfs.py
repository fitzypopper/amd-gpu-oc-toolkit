"""Low-level sysfs access helpers with actionable error messages."""

from __future__ import annotations

from pathlib import Path


class SysfsError(RuntimeError):
    """A sysfs read/write failed. The message is written for end users."""


def exists(path: str) -> bool:
    return Path(path).exists()


def read_text(path: str) -> str:
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        raise SysfsError(f"{path} does not exist (interface not available)")
    except PermissionError:
        raise SysfsError(f"permission denied reading {path}")
    except OSError as exc:
        raise SysfsError(f"cannot read {path}: {exc.strerror or exc}")


def read_int(path: str) -> int | None:
    """Read an integer sysfs attribute; return None if absent or unparsable."""
    try:
        return int(read_text(path).strip())
    except (SysfsError, ValueError):
        return None


def read_float(path: str) -> float | None:
    try:
        return float(read_text(path).strip())
    except (SysfsError, ValueError):
        return None


def write_value(path: str, value: str) -> None:
    """Write a value to a sysfs attribute (a trailing newline is added, as echo would)."""
    target = Path(path)
    try:
        with target.open("w") as handle:
            handle.write(f"{value}\n")
    except PermissionError:
        raise SysfsError(
            f"permission denied writing {path} — re-run this command with sudo"
        )
    except FileNotFoundError:
        raise SysfsError(f"{path} does not exist (interface not available)")
    except OSError as exc:
        raise SysfsError(f"write to {path} failed: {exc.strerror or exc}")
