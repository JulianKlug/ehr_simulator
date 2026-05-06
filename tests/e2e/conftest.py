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

import httpx
import pytest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    port = _free_port()
    log_dir = tmp_path_factory.mktemp("e2e-logs")
    env = {**os.environ, "EHR_LOG_DIR": str(log_dir)}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "ehr_simulator.web.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
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
            r = httpx.get(base_url + "/", timeout=1.0)
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
