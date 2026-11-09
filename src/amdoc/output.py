"""Small terminal styling helpers (color is auto-disabled when piped or NO_COLOR)."""

from __future__ import annotations

import os
import sys

_forced: bool | None = None


def set_color(enabled: bool | None) -> None:
    """Force colors on (True), off (False), or follow the terminal (None)."""
    global _forced
    _forced = enabled


def _enabled() -> bool:
    if _forced is not None:
        return _forced
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(text: str, code: str) -> str:
    if not _enabled():
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(text: str) -> str:
    return paint(text, "1")


def dim(text: str) -> str:
    return paint(text, "2")


def red(text: str) -> str:
    return paint(text, "31")


def green(text: str) -> str:
    return paint(text, "32")


def yellow(text: str) -> str:
    return paint(text, "33")


def blue(text: str) -> str:
    return paint(text, "34")


def cyan(text: str) -> str:
    return paint(text, "36")


def temp_color(celsius: float) -> str:
    """Color a temperature reading: green < 70, yellow < 90, red beyond."""
    text = f"{celsius:.0f}C"
    if celsius >= 90:
        return red(text)
    if celsius >= 70:
        return yellow(text)
    return green(text)
