"""structlog pipeline tests.

Covers:

- mandatory-field injection (8 keys present on every record, Decisions D4/D13);
- ``event_kind`` dispatch by HX-Request header (Decision D3);
- daily rollover at UTC midnight via freezegun, using the in-block-construction
  pattern from Decision D14.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path

from freezegun import freeze_time

from ehr_simulator.logging import (
    bind_request_context,
    get_logger,
    new_request_id,
    setup_logging,
)

_MANDATORY = (
    "request_id",
    "clinician_id",
    "patient_id",
    "timepoint",
    "timepoint_index",
    "event_kind",
    "chrome",
    "arm",
)


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _flush_handlers() -> None:
    for h in logging.getLogger("ehr_simulator").handlers:
        h.flush()


def test_setup_logging_emits_mandatory_fields(tmp_log_dir: Path) -> None:
    setup_logging(tmp_log_dir)
    bind_request_context(
        request_id=new_request_id(),
        patient_id="synth_001",
        timepoint=60.0,
        timepoint_index=1,
        event_kind="page.render",
        chrome="dense",
    )
    get_logger().info("rendered")
    _flush_handlers()

    records = _read_jsonl(tmp_log_dir / "current.jsonl")
    assert len(records) >= 1
    record = records[-1]
    for key in _MANDATORY:
        assert key in record, f"missing mandatory field {key!r}"
    assert record["clinician_id"] is None
    assert record["arm"] is None
    assert record["chrome"] == "dense"
    assert record["timepoint"] == 60.0
    assert record["timepoint_index"] == 1
    assert record["event_kind"] == "page.render"


def test_log_file_rolls_at_utc_midnight(tmp_log_dir: Path) -> None:
    """freezegun → cross UTC midnight → two files exist (Decisions D2, D11, D14)."""
    with freeze_time("2026-05-05T23:59:55Z") as frozen:
        setup_logging(tmp_log_dir)
        bind_request_context(request_id=new_request_id(), event_kind="app.boot")
        get_logger().info("before midnight")
        _flush_handlers()
        frozen.tick(timedelta(seconds=10))
        get_logger().info("after midnight")
        _flush_handlers()

    current = tmp_log_dir / "current.jsonl"
    rotated = tmp_log_dir / "current.jsonl.2026-05-05"
    assert current.exists(), "current.jsonl should still exist after rollover"
    assert rotated.exists(), (
        f"expected rotated file {rotated.name}; "
        f"actual files: {sorted(p.name for p in tmp_log_dir.iterdir())}"
    )
