"""Deterministic synthetic Phase 2 export bundles for the S12 analysis reader.

Drives the app (FakeClock, three synthetic clinicians, two configuration
versions) and exports with the shipped S11n exporter::

    v1  X  two completed cases; branches Yes / No; ``death_3mo`` rule rows
        Z  first case silent past grace → incomplete → replacement left
           open at t1 (reached_unanswered, pending)
    v2  (adds a multi-select and a probability question)
        Y  AI case whose AI panel never mounts, t1 without telemetry;
           no AI case that leaks

    walked/  that study          empty/  an activated study without cases

Run dependent values are pinned (spec S12 §6, amended): on the scratch DB,
every exported SQLite ``CURRENT_TIMESTAMP`` becomes ``T0 + row order``
seconds before export; ``session_id`` / ``render_id`` (uuid4) become
first appearance ordinals and ``generated_at_utc`` a constant, with the
manifest digests recomputed. The app and exporter are unchanged.

Run:
    uv run python tests/fixtures/analysis_bundle/build_analysis_bundle.py
    uv run python tests/fixtures/analysis_bundle/build_analysis_bundle.py --check
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import yaml  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ehr_simulator.db import connect  # noqa: E402
from ehr_simulator.db.connection import AccessMode  # noqa: E402
from ehr_simulator.export_bundle import MANIFEST_NAME, write_bundle  # noqa: E402
from ehr_simulator.export_phase2 import build_phase2_bundle  # noqa: E402
from tests.support.cases import HX, Config, _start, _started_patient  # noqa: E402
from tests.support.lifecycle import GRACE, T0, LifecycleHarness, _harness  # noqa: E402
from tests.support.pseudonym import TEST_SECRET  # noqa: E402
from tests.support.tab_guard import TAB_A, _claim, _owner, _post  # noqa: E402
from tests.support.telemetry import _event  # noqa: E402

FIXTURE_DIR = Path(__file__).parent
CONFIGS = REPO / "configs"
WALKED = "walked"
EMPTY = "empty"

HTTP_OK = 200
HTTP_NO_CONTENT = 204
TIMEPOINTS = 3
LAST_T_INDEX = TIMEPOINTS - 1
ARM_AI = "ai"

#: Pinned stand ins for run dependent export values.
GENERATED_AT_UTC = T0.strftime("%Y-%m-%dT%H:%M:%SZ")
SQLITE_TS = "%Y-%m-%d %H:%M:%S"

#: Exported columns SQLite fills with ``CURRENT_TIMESTAMP``: (table, column,
#: row order). Row order is deterministic because the walk is.
PINNED_TIMESTAMPS = (
    ("events", "server_ts", "event_id"),
    ("answers", "ts_recorded", "rowid"),
    ("arm_assignments", "activated_at", "rowid"),
    ("arm_assignments", "assigned_at", "rowid"),
    ("randomisation_schedules", "generated_at", "rowid"),
    ("configuration_history", "activated_at", "rowid"),
)
#: ``behavioral_events.csv`` columns holding uuid4 ids → ordinal prefix.
RUN_IDS = (("session_id", "session"), ("render_id", "render"))
EVENTS_CSV = "behavioral_events.csv"

V2_QUESTIONS = [
    {
        "question_id": "supporting_findings",
        "prompt": "Which findings support your prediction?",
        "response_type": "multi-select",
        # The leading "+" exercises the S9c formula guard on real output.
        "options": [
            "Clinical worsening",
            "+ new imaging finding",
            "Laboratory trend",
            "None of these",
        ],
    },
    {
        "question_id": "deterioration_probability",
        "prompt": "Probability of neurological deterioration within 6 hours?",
        "response_type": "probability-0-100",
    },
]

Answer = tuple[str, str | list[str]]


def _yes(confidence: int) -> list[Answer]:
    """Deterioration Yes shows ``primary_cause``; good outcome Yes derives death."""
    return [
        ("deterioration_6h", "Yes"),
        ("confidence", str(confidence)),
        ("primary_cause", "Placeholder cause A"),
        ("good_outcome_3mo", "Yes"),
    ]


def _no(confidence: int) -> list[Answer]:
    """Deterioration No hides ``primary_cause``; death answered explicitly."""
    return [
        ("deterioration_6h", "No"),
        ("confidence", str(confidence)),
        ("good_outcome_3mo", "No"),
        ("death_3mo", "No"),
    ]


def _v2(base: list[Answer], findings: list[str], probability: int) -> list[Answer]:
    return [
        *base,
        ("supporting_findings", findings),
        ("deterioration_probability", str(probability)),
    ]


# ---------------------------------------------------------------------------
# Walk
# ---------------------------------------------------------------------------


def _study_yaml(tmp: Path) -> Path:
    data = yaml.safe_load((CONFIGS / "example_phase2_config.yaml").read_text())
    data["case_lifecycle"]["replacement_cases_enabled"] = True
    path = tmp / "study_analysis.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def _v2_questions(tmp: Path) -> Path:
    data = yaml.safe_load((CONFIGS / "example_phase2_questions.yaml").read_text())
    data["questions"].extend(V2_QUESTIONS)
    path = tmp / "questions_v2.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


@contextmanager
def _tab(
    h: LifecycleHarness, config: Config | None = None, clinician_id: str | None = None
) -> Iterator[TestClient]:
    """A booted client that claims tabs itself, like the browser."""
    with h.client(config, clinician_id) as client:
        client.event_hooks["response"].clear()
        del client.tab_views
        yield client


def _render_id(html: str) -> str:
    return BeautifulSoup(html, "html.parser").select_one("#patient-view")["data-render-id"]


def _arm(h: LifecycleHarness, clinician_id: str, patient_id: str) -> str:
    with h.conn() as conn:
        return conn.execute(
            "SELECT arm FROM arm_assignments WHERE clinician_id = ? AND patient_id = ?",
            (clinician_id, patient_id),
        ).fetchone()[0]


def _telemetry(client: TestClient, render_id: str, *, ai_mount: bool) -> None:
    """Complete telemetry: vitals on screen; with ``ai_mount`` the AI panel too."""
    mounts = [("vitals", 2, 3)] + ([("ai", 4, 5)] if ai_mount else [])
    events = [_event(render_id, 1, "browser.timepoint_enter", client_mono_ms=0.0)]
    for panel, mount_seq, view_seq in mounts:
        events.append(
            _event(
                render_id,
                mount_seq,
                "panel.mount",
                client_mono_ms=50.0 * mount_seq,
                payload={
                    "panel_id": panel,
                    "expanded": True,
                    "collapsible": True,
                    "state": "loading",
                },
            )
        )
        events.append(
            _event(
                render_id,
                view_seq,
                "panel.viewport",
                client_mono_ms=50.0 * view_seq,
                payload={"panel_id": panel, "intersection_ratio": 1.0},
            )
        )
    events.append(_event(render_id, 9, "browser.timepoint_exit", client_mono_ms=5000.0))
    assert _post(client, TAB_A, events).status_code == HTTP_NO_CONTENT


def _answer(
    client: TestClient, patient_id: str, t_index: int, headers: dict, answers: list[Answer]
):
    for question_id, value in answers:
        response = client.post(
            f"/patient/{patient_id}/timepoint/{t_index}/answer",
            data={"question_id": question_id, "value": value},
            headers=headers,
        )
        assert response.status_code == HTTP_OK, response.text


def _walk(
    h: LifecycleHarness,
    client: TestClient,
    clinician_id: str,
    answers: Callable[[str], list[list[Answer]]],
    *,
    ai_mount: Callable[[str], bool] = lambda arm: arm == ARM_AI,
    silent: Callable[[str], frozenset[int]] = lambda arm: frozenset(),
    stop_at: int | None = None,
) -> str:
    """Start, claim, report, answer and advance one case; returns its patient.

    ``answers(arm)`` lists one answer batch per timepoint walked; ``silent(arm)``
    names timepoints whose browser never reports telemetry.
    """
    patient_id = _started_patient(_start(client))
    arm = _arm(h, clinician_id, patient_id)
    plan = answers(arm)
    render_id = _render_id(client.get(f"/patient/{patient_id}/timepoint/0").text)
    assert _claim(client, patient_id, TAB_A, render_id).status_code == HTTP_NO_CONTENT

    for t_index in range(TIMEPOINTS):
        if t_index not in silent(arm):
            _telemetry(client, render_id, ai_mount=ai_mount(arm))
        headers = {**HX, **_owner(TAB_A, render_id)}
        _answer(client, patient_id, t_index, headers, plan[t_index])
        if stop_at == t_index:
            break
        response = client.post(
            f"/patient/{patient_id}/timepoint/{t_index}/advance", headers=headers
        )
        assert response.status_code == HTTP_OK, response.text
        if t_index < LAST_T_INDEX:
            render_id = _render_id(response.text)
    return patient_id


def _walk_study(tmp: Path) -> LifecycleHarness:
    h = _harness(tmp, _study_yaml(tmp), CONFIGS / "example_phase2_questions.yaml")
    y_id = h.add_clinician("Dr. Analysis Y")
    z_id = h.add_clinician("Dr. Analysis Z")

    with _tab(h) as client:
        _walk(h, client, h.clinician_id, lambda arm: [_yes(4), _no(2), _yes(5)])
        _walk(h, client, h.clinician_id, lambda arm: [_no(3), _no(3), _yes(4)])
    with _tab(h, clinician_id=z_id) as client:
        lost = _walk(h, client, z_id, lambda arm: [_yes(2)[:1]], stop_at=0)
        h.clock.advance(GRACE + 1)
        client.post(f"/case/{lost}/heartbeat")
        _walk(h, client, z_id, lambda arm: [_no(1), _yes(3)[:2]], stop_at=1)

    v2 = Config("v2", h.v1.study_yaml, _v2_questions(tmp))
    h.activate(v2)
    with _tab(h, v2, y_id) as client:
        y_answers = [
            _v2(_yes(5), ["Clinical worsening", "+ new imaging finding"], 80),
            _v2(_no(2), ["None of these"], 15),
            # Starts with "+": exported guarded as "'+ new imaging finding|…".
            _v2(_yes(4), ["+ new imaging finding", "Laboratory trend"], 65),
        ]
        for _ in range(2):
            _walk(
                h,
                client,
                y_id,
                lambda arm: y_answers,
                # The AI case never mounts its panel; the no AI case leaks one.
                ai_mount=lambda arm: arm != ARM_AI,
                silent=lambda arm: frozenset({1}) if arm == ARM_AI else frozenset(),
            )
    return h


# ---------------------------------------------------------------------------
# Export + normalisation
# ---------------------------------------------------------------------------


def _pin_clock(db_path: Path) -> None:
    """Scratch DB only: CURRENT_TIMESTAMP columns → ``T0 + row order`` seconds."""
    conn = connect(db_path)
    try:
        triggers = conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        for (name,) in triggers.fetchall():
            conn.execute(f"DROP TRIGGER {name}")
        base = T0.strftime(SQLITE_TS)
        for table, column, order in PINNED_TIMESTAMPS:
            conn.execute(
                f"UPDATE {table} SET {column} = "
                f"strftime('{SQLITE_TS}', ?, '+' || {order} || ' seconds')",
                (base,),
            )
        conn.commit()
    finally:
        conn.close()


def _export(db_path: Path, study_id: str, out: Path) -> dict[str, bytes]:
    conn = connect(db_path, access=AccessMode.READ_ONLY)
    try:
        bundle = build_phase2_bundle(conn, study_id=study_id, pseudonym_secret=TEST_SECRET)
    finally:
        conn.close()
    write_bundle(bundle, out)
    return {p.name: p.read_bytes() for p in sorted(out.iterdir())}


def _run_id_map(events_csv: bytes) -> dict[str, str]:
    """uuid4 → ``<prefix>-0001`` in first appearance order."""
    mapping: dict[str, str] = {}
    counts = {prefix: 0 for _, prefix in RUN_IDS}
    for row in csv.DictReader(io.StringIO(events_csv.decode("utf-8"))):
        for column, prefix in RUN_IDS:
            value = row[column]
            if not value or value in mapping:
                continue
            counts[prefix] += 1
            mapping[value] = f"{prefix}-{counts[prefix]:04d}"
    return mapping


def _rows(data: bytes) -> int:
    return sum(1 for _ in csv.reader(io.StringIO(data.decode("utf-8")))) - 1


def _normalised(files: dict[str, bytes]) -> dict[str, bytes]:
    mapping = _run_id_map(files[EVENTS_CSV])
    out: dict[str, bytes] = {}
    for name, data in files.items():
        if name == MANIFEST_NAME:
            continue
        text = data.decode("utf-8")
        for value, placeholder in mapping.items():
            text = text.replace(value, placeholder)
        out[name] = text.encode("utf-8")

    manifest = json.loads(files[MANIFEST_NAME])
    manifest["generated_at_utc"] = GENERATED_AT_UTC
    manifest["files"] = {
        name: {"rows": _rows(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in out.items()
    }
    out[MANIFEST_NAME] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return out


def build() -> dict[str, bytes]:
    """Both bundles as ``relative path → bytes``."""
    out: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        for name, walk in ((WALKED, _walk_study), (EMPTY, _empty_study)):
            work = tmp / name
            work.mkdir()
            h = walk(work)
            _pin_clock(h.db_path)
            files = _export(h.db_path, h.v1.study.study_id, work / "bundle")
            out.update({f"{name}/{n}": data for n, data in _normalised(files).items()})
    return out


def _empty_study(tmp: Path) -> LifecycleHarness:
    return _harness(tmp, _study_yaml(tmp), CONFIGS / "example_phase2_questions.yaml")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def write(directory: Path, files: dict[str, bytes]) -> None:
    for relative, data in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _on_disk(directory: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(directory)): p.read_bytes()
        for name in (WALKED, EMPTY)
        if (directory / name).is_dir()
        for p in sorted((directory / name).iterdir())
    }


def _check() -> int:
    built = build()
    on_disk = _on_disk(FIXTURE_DIR)
    drift = sorted(n for n in built.keys() | on_disk.keys() if built.get(n) != on_disk.get(n))
    if drift:
        print(f"analysis bundle fixture drift: {drift}; rerun without --check", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 when on-disk files drift")
    parser.add_argument("--out", type=Path, default=None, help="write into this directory")
    args = parser.parse_args(argv[1:])

    if args.check:
        return _check()

    write(args.out or FIXTURE_DIR, build())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
