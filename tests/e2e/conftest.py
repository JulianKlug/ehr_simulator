"""E2E fixtures: boot a real uvicorn server in a subprocess so Playwright can
drive the page against actual HTTP. The server is shared across the e2e
session for speed.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn

from ehr_simulator.web.app import app_from_study_config


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "study"
_DB_FILENAME = "e2e.db"
_READY_TIMEOUT_SECONDS = 30


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
    work_dir: Path | None = None,
) -> Iterator[str]:
    """Boot ``serve`` in a subprocess; yield its base URL.

    ``activation`` is the ``activate-config`` argv (sans ``--db-path``) run
    first: S11b study mode refuses to boot without an active configuration.
    ``work_dir`` pins where the DB lives, for tests that also touch it.
    """
    port = _free_port()
    log_dir = tmp_path_factory.mktemp(f"{label}-logs")
    work_dir = work_dir or tmp_path_factory.mktemp(f"{label}-work")
    db_path = work_dir / _DB_FILENAME
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


_LIFECYCLE_STUDY = _FIXTURES_DIR / "study_lifecycle.yaml"


@pytest.fixture(scope="session")
def lifecycle_work_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Work dir (and so DB) of ``live_lifecycle_server``."""
    return tmp_path_factory.mktemp("e2e-lifecycle-work")


@pytest.fixture(scope="session")
def live_lifecycle_server(
    tmp_path_factory: pytest.TempPathFactory, lifecycle_work_dir: Path
) -> Iterator[str]:
    """S11e: Phase 2 study with ``case_lifecycle`` (pause enabled)."""
    study_yaml = str(_LIFECYCLE_STUDY)
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
        work_dir=lifecycle_work_dir,
    )


@pytest.fixture
def lifecycle_cli(lifecycle_work_dir: Path) -> Callable[..., None]:
    """Run an operator command on ``live_lifecycle_server``'s live DB.

    Example: ``lifecycle_cli("abandon-case", "--clinician", "Dr. X", "--patient", pid)``
    becomes ``abandon-case <study_lifecycle.yaml> … --db-path <work>/e2e.db``.
    """
    env = {**os.environ, "EHR_LOG_DIR": str(lifecycle_work_dir / "cli-logs")}

    def run(command: str, *args: str) -> None:
        argv = [command, str(_LIFECYCLE_STUDY), *args]
        _run_cli(
            [*argv, "--db-path", str(lifecycle_work_dir / _DB_FILENAME)],
            env=env,
            cwd=lifecycle_work_dir,
        )

    return run


class FakeClock:
    """Injected ``app.state.clock``: time moves only when a test says so."""

    def __init__(self) -> None:
        self.moment = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


@dataclass(frozen=True)
class ClockServer:
    base_url: str
    clock: FakeClock


@pytest.fixture
def live_clock_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ClockServer]:
    """S11f: replacement-enabled Phase 2 study served in-process on a ``FakeClock``.

    In-process (uvicorn in a thread, not ``serve``) so the test holds the
    clock and can jump past the reconnection grace instead of waiting it out.
    """
    work_dir = tmp_path_factory.mktemp("e2e-clock-work")
    db_path = work_dir / _DB_FILENAME
    log_dir = work_dir / "logs"
    study_yaml = _FIXTURES_DIR / "study_lifecycle_replacement.yaml"
    questions_yaml = _FIXTURES_DIR / "questions.yaml"
    env = {**os.environ, "EHR_LOG_DIR": str(log_dir)}
    activation = ["activate-config", str(study_yaml), str(questions_yaml)]
    activation += ["--version", "e2e", "--description", "e2e replacement"]
    _run_cli([*activation, "--db-path", str(db_path)], env=env, cwd=work_dir)

    clock = FakeClock()
    app = app_from_study_config(
        study_yaml,
        questions_yaml,
        log_dir=log_dir,
        db_path=db_path,
        backup_dir=work_dir / "backups",
        clock=clock,
    )
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="none")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Readiness: uvicorn flips ``started`` once the lifespan has run.
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("in-process uvicorn did not start")
        time.sleep(0.05)

    try:
        yield ClockServer(f"http://127.0.0.1:{port}", clock)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
