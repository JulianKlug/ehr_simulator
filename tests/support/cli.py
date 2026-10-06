"""Installed console script locator for OS exit-status tests."""

from __future__ import annotations

from pathlib import Path


def _console_script() -> Path:
    import sys

    exe = Path(sys.executable).parent / "ehr-simulator"
    assert exe.is_file(), "installed console script missing"
    return exe
