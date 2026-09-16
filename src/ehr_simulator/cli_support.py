"""Pure helpers consumed by the Typer CLI commands.

The CLI itself is thin — every interesting decision lives here so it can
be unit-tested without spinning up Typer. The three helpers:

- :func:`build_dataset_loader` — closure that maps a :class:`StudyConfig`
  to a callable returning a dataset (synthetic / Geneva / MIMIC). Encodes
  the S5 EHR_SIM_DATA_ROOT contract: non-synthetic datasets MUST set
  ``csv_path`` + ``params_dir`` in the YAML; the env var (when set) sandboxes
  paths via ``_path_traversal_guard`` but never discovers files (per
  /plan-eng-review issue 1.1 + refinement F).

- :func:`walk_preflight` — headless walk of every
  ``(patient_id, t_minutes)`` cell against the loaded dataset. Returns a
  :class:`PreflightReport` with OK / WARN / FAIL rows. Exit code at the CLI
  is 1 iff any FAIL row is present; WARN rows are non-fatal.

- :func:`render_preview` — text summary of one patient's panels per
  timepoint. ``--html-out`` mode delegates to a TestClient against the
  config-driven app factory so the rendering pipeline is exercised exactly
  as the live server would run it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ehr_simulator.config import ConfigError, Questions, StudyConfig
from ehr_simulator.web.panels import DatasetLike, slice_to_timepoint


def build_dataset_loader(study: StudyConfig) -> Callable[[], DatasetLike]:
    """Return a zero-arg loader closure routing to the right adapter.

    For ``dataset == "synthetic"``: ignore any inline paths (forbidden by
    StudyConfig validators) and return ``load_synthetic``.

    For ``dataset in {"geneva", "mimic"}``: ``study.csv_path`` and
    ``study.params_dir`` MUST both be set. Raise :class:`ConfigError` with
    the standard remediation message otherwise — this is the S5 tightening
    of EHR_SIM_DATA_ROOT (per /plan-eng-review issue 1.1, refinement F): the
    env var sandboxes paths but does not discover files.
    """
    dataset_name = study.dataset
    if dataset_name == "synthetic":
        from ehr_simulator.ingestion.synthetic import load_synthetic

        return load_synthetic

    if study.csv_path is None or study.params_dir is None:
        raise ConfigError(
            "study_config.yaml must specify csv_path and params_dir for non-synthetic "
            "datasets. EHR_SIM_DATA_ROOT (optional) restricts paths to a sandbox "
            "directory but does not discover files."
        )

    csv_path = Path(study.csv_path)
    params_dir = Path(study.params_dir)
    # Filter at ingestion time so a pilot config (3-50 patients) doesn't
    # pay the full-dataset memory + load-time cost (~600 MB / 51 s on
    # Geneva real data). Skipped (None) only when no study config is in
    # scope — `validate-adapter` and friends always pass a study, so the
    # filter is always active when the CLI builds the loader.
    pids = tuple(study.patient_ids)

    if dataset_name == "geneva":
        from ehr_simulator.ingestion.geneva import load_geneva

        def _load_geneva() -> DatasetLike:
            return load_geneva(csv_path, params_dir, strict=False, patient_ids=pids)

        return _load_geneva

    if dataset_name == "mimic":
        from ehr_simulator.ingestion.mimic import load_mimic

        def _load_mimic() -> DatasetLike:
            return load_mimic(csv_path, params_dir, strict=False, patient_ids=pids)

        return _load_mimic

    raise ConfigError(f"unsupported dataset: {dataset_name!r}")


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


PreflightStatus = Literal["OK", "WARN", "FAIL"]


@dataclass(frozen=True)
class PreflightRow:
    patient_id: str
    t_minutes: float
    status: PreflightStatus
    message: str


@dataclass(frozen=True)
class PreflightReport:
    rows: list[PreflightRow]

    @property
    def has_fail(self) -> bool:
        return any(r.status == "FAIL" for r in self.rows)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.rows if r.status == "FAIL")

    @property
    def warn_count(self) -> int:
        return sum(1 for r in self.rows if r.status == "WARN")

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.rows if r.status == "OK")


def walk_preflight(
    study: StudyConfig,
    questions: Questions,  # noqa: ARG001  (accepted for forward-compat with S9)
    dataset: DatasetLike,
) -> PreflightReport:
    """Walk every ``(patient_id, t_minutes)`` cell and aggregate per-cell status.

    - ``FAIL: patient X not found in dataset`` — the patient_id in the study
      config is absent from the dataset's ADMISSION frame. Fatal.
    - ``WARN: patient X has no scalar_ts data at t=N`` — patient exists but
      has zero ``scalar_ts`` rows at or before this timepoint. Surfaces the
      empty-expected vs empty-unexpected ambiguity from S2 panel-state
      taxonomy. Non-fatal.
    - ``OK`` — at least one ``scalar_ts`` row at-or-before the timepoint.
    """
    rows: list[PreflightRow] = []
    known_pids = set(dataset.admission["patient_id"].astype(str).unique().tolist())

    for patient_id in study.patient_ids:
        if patient_id not in known_pids:
            for t in study.timepoints_minutes:
                rows.append(
                    PreflightRow(
                        patient_id=patient_id,
                        t_minutes=t,
                        status="FAIL",
                        message=f"patient {patient_id} not found in dataset",
                    )
                )
            continue

        for t in study.timepoints_minutes:
            sliced = slice_to_timepoint(
                dataset,
                patient_id,
                t_minutes=t,
                timepoint_index=0,
            )
            if sliced.scalar_ts.empty:
                rows.append(
                    PreflightRow(
                        patient_id=patient_id,
                        t_minutes=t,
                        status="WARN",
                        message=f"patient {patient_id} has no scalar_ts data at t={t:g}",
                    )
                )
            else:
                rows.append(
                    PreflightRow(
                        patient_id=patient_id,
                        t_minutes=t,
                        status="OK",
                        message=f"patient {patient_id} t={t:g}",
                    )
                )

    return PreflightReport(rows=rows)


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreviewRow:
    t_minutes: float
    counts: dict[str, int]
    notes: list[str]


@dataclass(frozen=True)
class PreviewReport:
    patient_id: str
    dataset_name: str
    rows: list[PreviewRow]


def render_preview(
    study: StudyConfig,
    patient_id: str,
    dataset: DatasetLike,
) -> PreviewReport:
    """Per-timepoint text summary for one patient: row counts + WARN notes."""
    rows: list[PreviewRow] = []
    known_pids = set(dataset.admission["patient_id"].astype(str).unique().tolist())
    if patient_id not in known_pids:
        raise ConfigError(f"patient {patient_id!r} not found in dataset")

    for t in study.timepoints_minutes:
        sliced = slice_to_timepoint(dataset, patient_id, t_minutes=t, timepoint_index=0)
        counts: dict[str, int] = {
            "scalar_ts": int(len(sliced.scalar_ts)),
            "admission": int(len(sliced.admission)),
            "imaging": int(len(sliced.imaging)),
            "ai_output": int(len(sliced.ai_output)),
        }
        notes: list[str] = []
        for panel, state in sliced.panel_states.items():
            if state == "empty-unexpected":
                notes.append(f"WARN: {panel} empty-unexpected at t={t:g}")
        rows.append(PreviewRow(t_minutes=t, counts=counts, notes=notes))

    return PreviewReport(patient_id=patient_id, dataset_name=study.dataset, rows=rows)


def format_preview_text(report: PreviewReport) -> str:
    """Render a :class:`PreviewReport` as the stdout-facing summary."""
    lines = [f"Patient: {report.patient_id} (dataset={report.dataset_name})"]
    for row in report.rows:
        counts = " ".join(f"{k}={v}" for k, v in row.counts.items())
        line = f"  t={row.t_minutes:g}  {counts}"
        if row.notes:
            line += "   " + " | ".join(row.notes)
        lines.append(line)
    return "\n".join(lines)


def format_preflight_text(report: PreflightReport) -> str:
    """Render a :class:`PreflightReport` as the stdout-facing summary."""
    lines: list[str] = []
    for row in report.rows:
        lines.append(f"{row.status}: {row.message}")
    lines.append(
        f"Summary: {report.ok_count} OK, {report.warn_count} WARN, {report.fail_count} FAIL"
    )
    return "\n".join(lines)


def render_html_for_preview(
    study_path: Path,
    questions_path: Path,
    patient_id: str,
    log_dir: Path,
    out_dir: Path,
) -> list[Path]:
    """Render each timepoint to a standalone HTML file via TestClient.

    Reuses :func:`web.app.app_from_study_config` (the same factory ``serve
    --config`` uses) so the rendered HTML matches what a clinician would see
    at runtime — including the study-bound t_index → t_minutes mapping
    (per /plan-eng-review issue 1.2).
    """
    from fastapi.testclient import TestClient

    from ehr_simulator.db import apply_migrations, clinicians, connect, progress
    from ehr_simulator.web.app import app_from_study_config

    out_dir.mkdir(parents=True, exist_ok=True)
    # Preview is a dev-only rendering tool — write to a scratch DB so the
    # default ``data/ehr_simulator.db`` is never touched, and seed a
    # synthetic clinician so the protected-route preamble lets us in.
    scratch_db = out_dir / "_preview_scratch.db"
    seed_conn = connect(scratch_db)
    apply_migrations(seed_conn)
    clinician_id = clinicians.lookup_or_create(seed_conn, "Dr. Preview")
    seed_conn.close()

    app = app_from_study_config(
        study_path,
        questions_path,
        log_dir=log_dir,
        db_path=scratch_db,
        backup_dir=out_dir / "_preview_backups",
    )
    written: list[Path] = []
    # follow_redirects off: a gate redirect must fail loudly here instead of
    # silently writing the t=0 body into every file.
    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set("ehrsim_clinician_id", clinician_id)
        study = _load_study_for_app(study_path)
        for idx, _ in enumerate(study.timepoints_minutes):
            # S9b: walk the frontier step-wise so each file shows the OPEN
            # pane a clinician would see at that timepoint.
            if idx > 0:
                progress.unlock(
                    app.state.db,
                    clinician_id=clinician_id,
                    patient_id=patient_id,
                    from_t_index=idx - 1,
                    to_t_index=idx,
                    config_hash=app.state.config_hash,
                )
            response = client.get(f"/patient/{patient_id}/timepoint/{idx}")
            response.raise_for_status()
            target = out_dir / f"{patient_id}_t{idx}.html"
            target.write_text(response.text, encoding="utf-8")
            written.append(target)
    return written


def _load_study_for_app(study_path: Path) -> StudyConfig:
    """Local re-loader so :func:`render_html_for_preview` doesn't depend on
    being passed the parsed model — keeps the public callable signature
    minimal."""
    from ehr_simulator.config import load_study_config

    return load_study_config(study_path)


def walk_preflight_report(study: StudyConfig, questions: Questions) -> tuple[PreflightReport, Any]:
    """Convenience: build the loader, run :func:`walk_preflight`, return both.

    Returns ``(report, dataset)`` so the CLI can also surface
    ``dataset.issues`` (Geneva/MIMIC IngestionIssue list) alongside the walk
    output without re-loading.
    """
    loader = build_dataset_loader(study)
    dataset = loader()
    report = walk_preflight(study, questions, dataset)
    return report, dataset


# ---------------------------------------------------------------------------
# reset-progress (S9b, owner decision b)
# ---------------------------------------------------------------------------


class ResetError(ValueError):
    """The operator asked for a reset that cannot be applied; nothing was written."""


@dataclass(frozen=True)
class ResetReport:
    clinician_id: str
    previous_unlocked_t_index: int
    was_completed: bool
    to_t_index: int
    deleted_answers: int


def reset_progress(
    conn: Any,
    *,
    clinician_name: str,
    patient_id: str,
    to_t_index: int,
    timepoints: list[float],
) -> ResetReport:
    """Rewind one clinician's walk of one patient to ``to_t_index``.

    Answers strictly after the target timepoint are deleted (the re-opened
    pane pre-fills from what survives); a completed walk is re-opened; one
    ``progress.reset`` event records the intervention. Raises
    :class:`ResetError` — before any write — when the clinician is unknown,
    the pair has no walk, the index is outside the study, or the index is
    ahead of the current frontier (a reset only rewinds).
    """
    from ehr_simulator.db import answers, clinicians, events, progress

    if not 0 <= to_t_index < len(timepoints):
        raise ResetError(
            f"--to-t-index {to_t_index} outside the study (valid: 0…{len(timepoints) - 1})"
        )
    clinician_id = clinicians.lookup(conn, clinician_name)
    if clinician_id is None:
        raise ResetError(f"unknown clinician {clinician_name!r}")
    row = progress.fetch(conn, clinician_id=clinician_id, patient_id=patient_id)
    if row is None:
        raise ResetError(f"{clinician_name!r} has not started patient {patient_id!r}")
    if to_t_index > row.unlocked_t_index:
        raise ResetError(
            f"--to-t-index {to_t_index} is ahead of the current frontier "
            f"{row.unlocked_t_index}; reset only rewinds"
        )

    # Delete first, rewind second: a failure between the two leaves the walk
    # intact for a retry instead of a rewound frontier over orphaned answers
    # that would pre-fill the re-walk as already complete.
    deleted = answers.delete_after(
        conn,
        clinician_id=clinician_id,
        patient_id=patient_id,
        min_timepoint_exclusive=timepoints[to_t_index],
    )
    progress.reset(conn, clinician_id=clinician_id, patient_id=patient_id, to_t_index=to_t_index)
    events.append(
        conn,
        session_id=None,
        clinician_id=clinician_id,
        patient_id=patient_id,
        timepoint=timepoints[to_t_index],
        kind="progress.reset",
        payload={
            "from_t_index": row.unlocked_t_index,
            "to_t_index": to_t_index,
            "was_completed": row.completed_at is not None,
            "deleted_answers": deleted,
        },
    )
    return ResetReport(
        clinician_id=clinician_id,
        previous_unlocked_t_index=row.unlocked_t_index,
        was_completed=row.completed_at is not None,
        to_t_index=to_t_index,
        deleted_answers=deleted,
    )


def assert_schema_current(conn: Any) -> None:
    """Refuse to touch a DB whose schema is behind this build.

    Operator commands must not apply DDL under a running server; that is
    ``ehr-simulator migrate``'s job, run with the server stopped.
    """
    from ehr_simulator.db import MIGRATIONS

    applied = {
        row[0]
        for row in conn.execute(
            "SELECT version FROM schema_migrations"
            if _has_migrations_table(conn)
            else "SELECT 0 WHERE 0"
        )
    }
    pending = [m.version for m in MIGRATIONS if m.version not in applied]
    if pending:
        raise ResetError(
            f"database schema is behind (pending migrations {pending}); "
            "stop the server and run `ehr-simulator migrate` first"
        )


def _has_migrations_table(conn: Any) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    return row is not None
