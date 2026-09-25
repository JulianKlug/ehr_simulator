"""E2E fixtures: boot a real uvicorn server in a subprocess so Playwright can
drive the page against actual HTTP. The server is shared across the e2e
session for speed.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "study"


def _run_cli(args: list[str], *, env: dict[str, str], cwd: Path) -> None:
    """Run one ``ehr_simulator.cli`` command to completion; fail loudly on refusal."""
    done = subprocess.run(
        [sys.executable, "-m", "ehr_simulator.cli", *args],
        env=env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise RuntimeError(f"{args[0]} failed:\n{done.stdout}\n{done.stderr}")


def _boot_server(
    tmp_path_factory: pytest.TempPathFactory,
    *,
    label: str,
    extra_args: list[str],
    activation: list[str] | None = None,
) -> Iterator[str]:
    """Boot ``serve`` in a subprocess; yield its base URL.

    ``activation`` is the ``activate-config`` argv (sans ``--db-path``) run
    first: S11b study mode refuses to boot without an active configuration.
    """
    port = _free_port()
    log_dir = tmp_path_factory.mktemp(f"{label}-logs")
    work_dir = tmp_path_factory.mktemp(f"{label}-work")
    db_path = work_dir / "e2e.db"
    backup_dir = work_dir / "backups"
    env = {
        **os.environ,
        "EHR_LOG_DIR": str(log_dir),
        # The uvicorn invocation below uses the module-level ``app`` whose
        # lifespan resolves ``data/ehr_simulator.db`` by default. Set the
        # CWD to the tmp work-dir so the lifespan writes the e2e DB +
        # backups there rather than polluting the repo.
        "PWD": str(work_dir),
    }
    if activation is not None:
        _run_cli([*activation, "--db-path", str(db_path)], env=env, cwd=work_dir)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ehr_simulator.cli",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--db-path",
            str(db_path),
            "--backup-dir",
            str(backup_dir),
            *extra_args,
        ],
        env=env,
        cwd=str(work_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read().decode() if proc.stdout else ""
            raise RuntimeError(f"uvicorn exited early:\n{output}")
        try:
            # /login is the unauthenticated landing page; it returns 200
            # without a cookie. Use it as the readiness probe.
            r = httpx.get(base_url + "/login", timeout=1.0)
            if r.status_code == 200:
                break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError(f"uvicorn did not become ready in 30s: {last_err}")

    try:
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def live_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Bare ``serve``: synthetic data, no study config, no questions pane."""
    yield from _boot_server(tmp_path_factory, label="e2e", extra_args=[])


@pytest.fixture(scope="session")
def live_study_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """``serve --config … --questions …`` on the synthetic study fixture.

    Paths are absolute: the subprocess runs with ``cwd`` set to a tmp dir,
    so repo-relative argv would resolve against the wrong directory.
    """
    study_yaml = str(_FIXTURES_DIR / "study_synthetic.yaml")
    questions_yaml = str(_FIXTURES_DIR / "questions.yaml")
    yield from _boot_server(
        tmp_path_factory,
        label="e2e-study",
        extra_args=["--config", study_yaml, "--questions", questions_yaml],
        activation=[
            "activate-config",
            study_yaml,
            questions_yaml,
            "--version",
            "e2e",
            "--description",
            "e2e baseline",
        ],
    )


@pytest.fixture(scope="session")
def live_lifecycle_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """S11e: Phase 2 study with ``case_lifecycle`` (pause enabled)."""
    study_yaml = str(_FIXTURES_DIR / "study_lifecycle.yaml")
    questions_yaml = str(_FIXTURES_DIR / "questions.yaml")
    yield from _boot_server(
        tmp_path_factory,
        label="e2e-lifecycle",
        extra_args=["--config", study_yaml, "--questions", questions_yaml],
        activation=[
            "activate-config",
            study_yaml,
            questions_yaml,
            "--version",
            "e2e",
            "--description",
            "e2e lifecycle",
        ],
    )
