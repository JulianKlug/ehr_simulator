"""CLI test: lock the ``serve`` entry point so S5's Typer swap can be verified
by extending this pattern (Decision D7)."""

from __future__ import annotations

from typing import Any

import pytest

from ehr_simulator import cli


@pytest.fixture
def captured_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_run(app: str, **kwargs: Any) -> None:
        calls.append({"app": app, **kwargs})

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    return calls


def test_cli_serve_invokes_uvicorn(captured_calls: list[dict[str, Any]]) -> None:
    cli.main(["serve", "--port", "8123", "--reload"])

    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["app"] == "ehr_simulator.web.app:app"
    assert call["host"] == "127.0.0.1"
    assert call["port"] == 8123
    assert call["reload"] is True


def test_cli_serve_defaults(captured_calls: list[dict[str, Any]]) -> None:
    cli.main(["serve"])

    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["host"] == "127.0.0.1"
    assert call["port"] == 8000
    assert call["reload"] is False
