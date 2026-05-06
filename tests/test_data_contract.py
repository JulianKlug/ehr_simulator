"""Drift gate for ``docs/data-contract.md``.

Runs the generator in ``--check`` mode against the committed file. Exits
non-zero on drift, with a unified diff snippet on stderr to help the
implementer regenerate.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_data_contract_md_no_drift() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/gen_data_contract.py", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"docs/data-contract.md drift detected.\nstderr:\n{result.stderr}"
    )
