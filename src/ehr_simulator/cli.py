"""Command-line entry point for ``ehr-simulator``.

Ten commands after S11b:

- ``serve`` — boot uvicorn against the FastAPI app. ``--config STUDY``
  + ``--questions Q`` wires a study-driven loader; without ``--config`` the
  synthetic default holds (back-compat with S2). ``--db-path`` +
  ``--backup-dir`` (S6) plumb persistence + shutdown-backup destinations.
- ``validate-config`` — Pydantic-validate study + questions YAML; exit 1
  with the offending field path on failure.
- ``validate-adapter`` — resolve the study config's dataset and try to
  load it. Surfaces ingestion issues via stdout.
- ``preflight`` — headless walk of every ``(patient_id, timepoint)``;
  catches missing patients and empty-data timepoints before a clinician
  sees a broken UI mid-session.
- ``preview`` — render a single patient's per-timepoint summary as text;
  ``--html-out`` additionally dumps the rendered HTMX panel HTML for
  design review and bug repro.
- ``migrate`` (S6) — apply unapplied DB migrations + ``PRAGMA
  wal_checkpoint(TRUNCATE)`` so the bare ``.db`` file is a complete
  snapshot. Idempotent.
- ``backup`` (S6) — snapshot the SQLite DB to a backup directory.
- ``reset-progress`` (S9b) — operator recovery for a mis-advanced walk:
  rewind one clinician's frontier on one patient, drop the answers past
  it, record a ``progress.reset`` event.
- ``export-answers`` (S9c) — read-only, strictly-validated, guarded CSV
  export of the recorded answers plus an optional POSIX 0600 keyfile.
  ``STUDY_CONFIG QUESTIONS`` are positional; ``--db-path``/``--out``/
  ``--keyfile``/``--only-complete``/``--force`` round it out. Exit 0 on
  success (including a 0-row export), 1 with ``Error: <reason>`` on any
  rejection; nothing is ever written before every validation passes.
- ``activate-config`` (S11b) — register the given study + questions YAML
  as a new configuration version (``--version``/``--description``
  required, ``--reason`` optional) and make it the study's active
  configuration. Binds the database's study identity first (refusing a
  non-empty unbound legacy database), then applies one atomic commit.
  Exit 0 on success (including a same-registered no-op), 1 on any
  refusal.
- ``abandon-case`` (S11e) — mark one open Phase 2 case ``incomplete`` with
  reason ``operator_abandoned``; terminal or unknown cases exit 1 unwritten.
- ``case-status`` (S11e) — read-only per-clinician lifecycle counts and
  remaining limits, keyed by ``clinician_id``.

The ``main(argv: list[str] | None = None) -> None`` signature is preserved
from the S2 argparse skeleton so ``test_cli.py``'s monkeypatch idiom carries
over for the ``serve`` command.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import typer
import uvicorn

from ehr_simulator.config import ConfigError

app_typer: typer.Typer = typer.Typer(
    name="ehr-simulator",
    no_args_is_help=True,
    add_completion=False,
)


def main(argv: list[str] | None = None) -> None:
    """Console entry point (S10 §8: command failure must reach the OS).

    With ``standalone_mode=True`` typer/click translate ``Exit`` into
    ``SystemExit(1)`` (refusal) or ``SystemExit(2)`` (usage); success
    paths return ``None`` so direct ``cli.main([...])`` test ergonomics
    survive. A refusal surfaces as ``SystemExit`` with the exact code,
    which the installed console script propagates as the process status.
    """
    try:
        app_typer(args=argv, standalone_mode=True)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        if code == 0:
            return None
        raise SystemExit(code) from exc


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@app_typer.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    reload: bool = typer.Option(False, "--reload"),
    config: Path | None = typer.Option(None, "--config", help="Path to study_config.yaml."),
    questions: Path | None = typer.Option(
        None, "--questions", help="Path to questions.yaml (required with --config)."
    ),
    db_path: Path | None = typer.Option(
        None,
        "--db-path",
        help=(
            "Path to the SQLite DB. Defaults to data/study_<study_id>.db in study mode, "
            "data/ehr_simulator.db otherwise. Bypasses the traversal guard (explicit operator "
            "decision), but never bypasses the study-identity check."
        ),
    ),
    backup_dir: Path | None = typer.Option(
        None,
        "--backup-dir",
        help="Directory for shutdown-time SQLite backups. Defaults to <db parent>/backups.",
    ),
) -> None:
    """Run the FastAPI server via uvicorn."""
    if config is None and questions is None:
        if db_path is None and backup_dir is None:
            uvicorn.run(
                "ehr_simulator.web.app:app",
                host=host,
                port=port,
                reload=reload,
            )
            return
        from ehr_simulator.web.app import create_app

        app_instance = create_app(
            log_dir=Path("logs"),
            db_path=db_path,
            backup_dir=backup_dir,
        )
        if reload:
            typer.echo(
                "Warning: --reload disabled when --db-path/--backup-dir is set "
                "(reload requires the import-string entry point).",
                err=True,
            )
            reload = False
        uvicorn.run(app_instance, host=host, port=port, reload=reload)
        return

    if config is None or questions is None:
        typer.echo(
            "Error: --config and --questions must be passed together.",
            err=True,
        )
        raise typer.Exit(code=2)

    if reload:
        typer.echo(
            "Warning: --reload disabled when --config is set "
            "(reload requires the import-string entry point).",
            err=True,
        )
        reload = False

    from ehr_simulator.web.app import app_from_study_config

    try:
        app_instance = app_from_study_config(
            config,
            questions,
            log_dir=Path("logs"),
            db_path=db_path,
            backup_dir=backup_dir,
        )
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    uvicorn.run(app_instance, host=host, port=port, reload=reload)


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------


@app_typer.command()
def migrate(
    db_path: Path = typer.Option(
        Path("data/ehr_simulator.db"),
        "--db-path",
        help="Path to the SQLite DB to migrate.",
    ),
) -> None:
    """Apply all unapplied DB migrations + checkpoint the WAL.

    The post-migration ``PRAGMA wal_checkpoint(TRUNCATE)`` ensures a
    researcher who ``cp``'s the bare ``.db`` file afterwards doesn't lose
    un-checkpointed writes (review-fix R14).
    """
    from ehr_simulator.db import apply_migrations, connect
    from ehr_simulator.logging import setup_logging

    setup_logging(Path("logs"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        versions = apply_migrations(conn)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    if versions:
        typer.echo(f"Applied migrations: {versions}")
    else:
        typer.echo("No migrations to apply.")


@app_typer.command()
def backup(
    db_path: Path = typer.Option(
        Path("data/ehr_simulator.db"),
        "--db-path",
        help="Path to the SQLite DB to back up.",
    ),
    backup_dir: Path = typer.Option(
        Path("data/backups"),
        "--backup-dir",
        help="Directory the backup snapshot is written into. Auto-created.",
    ),
) -> None:
    """Snapshot the SQLite DB to a timestamped file in --backup-dir."""
    from ehr_simulator.db.backup import create_backup
    from ehr_simulator.logging import setup_logging

    setup_logging(Path("logs"))
    if not db_path.exists():
        typer.echo(f"Error: db_path does not exist: {db_path}", err=True)
        raise typer.Exit(code=1)
    dest = create_backup(db_path, backup_dir)
    typer.echo(f"Backup written to: {dest}")


# ---------------------------------------------------------------------------
# validate-config
# ---------------------------------------------------------------------------


@app_typer.command("validate-config")
def validate_config(
    study_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    questions_path: Path = typer.Argument(..., exists=True, dir_okay=False),
) -> None:
    """Validate study_config.yaml + questions.yaml shape."""
    from ehr_simulator.config import load_questions, load_study_config

    try:
        study = load_study_config(study_path)
        questions_obj = load_questions(questions_path)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        f"OK: {study_path} ({len(study.patient_ids)} patients, "
        f"{len(study.timepoints)} timepoints), "
        f"{questions_path} ({len(questions_obj.questions)} questions, schema_version=1)"
    )
    if study.randomisation is not None and study.case_lifecycle is None:
        typer.echo(
            "Warning: randomisation without case_lifecycle — no reconnection timeout, "
            "no voluntary pause and no clinician stopping limits apply.",
            err=True,
        )


# ---------------------------------------------------------------------------
# validate-adapter
# ---------------------------------------------------------------------------


@app_typer.command("validate-adapter")
def validate_adapter(
    study_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    strict: bool = typer.Option(False, "--strict", help="Fail on first AdapterError."),
) -> None:
    """Resolve a study_config.yaml's dataset and try to load it."""
    from ehr_simulator.cli_support import build_dataset_loader
    from ehr_simulator.config import load_study_config
    from ehr_simulator.ingestion.exceptions import AdapterError

    try:
        study = load_study_config(study_path)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    try:
        loader = build_dataset_loader(study)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if strict and study.dataset != "synthetic":
        loader = _strict_loader(study)

    try:
        dataset = loader()
    except AdapterError as exc:
        typer.echo(f"AdapterError: {exc}", err=True)
        for issue in exc.issues:
            typer.echo(f"  {issue.dataset}: {issue.reason}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Dataset:    {study.dataset}")
    if study.csv_path is not None:
        typer.echo(f"csv_path:   {study.csv_path}")
    if study.params_dir is not None:
        typer.echo(f"params_dir: {study.params_dir}")
    typer.echo(f"SCALAR_TS:  {len(dataset.scalar_ts)} rows")
    typer.echo(f"ADMISSION:  {len(dataset.admission)} rows")
    typer.echo(f"IMAGING:    {len(dataset.imaging)} rows")
    typer.echo(f"AI_OUTPUT:  {len(dataset.ai_output)} rows")
    issues = getattr(dataset, "issues", [])
    if issues:
        typer.echo(f"Issues:     {len(issues)}")
        for issue in issues:
            typer.echo(f"  {issue.dataset}: {issue.reason}")
    else:
        typer.echo("Issues:     0")


def _strict_loader(study):  # type: ignore[no-untyped-def]
    """Strict-mode equivalent of :func:`cli_support.build_dataset_loader`.

    Used only by ``validate-adapter --strict``. ``build_dataset_loader``
    is the lenient/runtime path (issues collected, never raises); this is
    the failing-fast path the CLI exposes for CI gates.
    """
    if study.dataset == "geneva":
        from ehr_simulator.ingestion.geneva import load_geneva

        def _go():  # type: ignore[no-untyped-def]
            return load_geneva(study.csv_path, study.params_dir, strict=True)

        return _go
    if study.dataset == "mimic":
        from ehr_simulator.ingestion.mimic import load_mimic

        def _go():  # type: ignore[no-untyped-def]
            return load_mimic(study.csv_path, study.params_dir, strict=True)

        return _go
    raise ConfigError(f"--strict not supported for dataset={study.dataset!r}")


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


@app_typer.command()
def preflight(
    study_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    questions_path: Path = typer.Argument(..., exists=True, dir_okay=False),
) -> None:
    """Walk every (patient_id, timepoint) headlessly to surface issues."""
    from ehr_simulator.cli_support import format_preflight_text, walk_preflight_report
    from ehr_simulator.config import load_questions, load_study_config

    try:
        study = load_study_config(study_path)
        questions = load_questions(questions_path)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    try:
        report, _dataset = walk_preflight_report(study, questions)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(format_preflight_text(report))
    if report.has_fail:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# preview
# ---------------------------------------------------------------------------


@app_typer.command()
def preview(
    study_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    patient: str = typer.Option(..., "--patient", help="Patient ID to render."),
    questions_path: Path | None = typer.Option(
        None,
        "--questions",
        help="Optional questions.yaml; renders the questions pane with --html-out.",
    ),
    html_out: Path | None = typer.Option(
        None,
        "--html-out",
        help=(
            "Directory to dump per-timepoint HTML files. References /static URLs "
            "valid only on a live server."
        ),
    ),
) -> None:
    """Render a single patient's per-timepoint summary."""
    from ehr_simulator.cli_support import (
        build_dataset_loader,
        format_preview_text,
        render_html_for_preview,
        render_preview,
    )
    from ehr_simulator.config import load_study_config

    try:
        study = load_study_config(study_path)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    try:
        loader = build_dataset_loader(study)
        dataset = loader()
        report = render_preview(study, patient, dataset)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(format_preview_text(report))

    if html_out is not None:
        if questions_path is None:
            typer.echo(
                "Error: --html-out requires --questions (the app factory needs both files).",
                err=True,
            )
            raise typer.Exit(code=2)
        try:
            written = render_html_for_preview(
                study_path,
                questions_path,
                patient,
                log_dir=Path("logs"),
                out_dir=html_out,
            )
        except ConfigError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"Wrote {len(written)} HTML files under {html_out}")


# ---------------------------------------------------------------------------
# reset-progress
# ---------------------------------------------------------------------------


@app_typer.command("reset-progress")
def reset_progress_cmd(
    study_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    clinician: str = typer.Option(..., "--clinician", help="Clinician name as typed at login."),
    patient: str = typer.Option(..., "--patient", help="Patient ID whose walk to rewind."),
    to_t_index: int = typer.Option(
        0, "--to-t-index", help="Timepoint index to re-open (answers after it are deleted)."
    ),
    db_path: Path | None = typer.Option(
        None,
        "--db-path",
        help="SQLite DB; defaults to the study's db_path / data/study_<study_id>.db.",
    ),
) -> None:
    """Rewind a clinician's walk of one patient (recovery for a mis-click on Next)."""
    from ehr_simulator.cli_support import OperatorError, assert_schema_current, reset_progress
    from ehr_simulator.config import load_study_config
    from ehr_simulator.db import connect, resolve_db_path
    from ehr_simulator.db.exceptions import StudyIdentityError
    from ehr_simulator.db.study_identity import require as require_study_identity
    from ehr_simulator.logging import setup_logging

    setup_logging(Path("logs"))
    try:
        study = load_study_config(study_path)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    resolved_db = db_path if db_path is not None else resolve_db_path(study)
    if not resolved_db.exists():
        typer.echo(f"Error: db_path does not exist: {resolved_db}", err=True)
        raise typer.Exit(code=1)

    conn = connect(resolved_db)
    try:
        assert_schema_current(conn)
        require_study_identity(conn, study.study_id)
        report = reset_progress(
            conn,
            clinician_name=clinician,
            patient_id=patient,
            to_t_index=to_t_index,
            timepoints=list(study.timepoints_minutes),
        )
    except (OperatorError, StudyIdentityError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    state = " (was complete)" if report.was_completed else ""
    typer.echo(
        f"Reset {patient} for clinician {report.clinician_id}: "
        f"frontier {report.previous_unlocked_t_index}{state} → {report.to_t_index}, "
        f"{report.deleted_answers} answer(s) deleted."
    )


# ---------------------------------------------------------------------------
# abandon-case / case-status (S11e)
# ---------------------------------------------------------------------------


def _open_study_db(study_path: Path, db_path: Path | None, *, read_only: bool):
    """Load the study, open its DB and pass the schema + identity gates.

    Returns ``(study, conn)``; any refusal is ``typer.Exit(1)``.
    """
    from ehr_simulator.cli_support import OperatorError, assert_schema_current
    from ehr_simulator.config import load_study_config
    from ehr_simulator.db import AccessMode, connect, resolve_db_path
    from ehr_simulator.db.exceptions import StudyIdentityError
    from ehr_simulator.db.study_identity import require as require_study_identity

    try:
        study = load_study_config(study_path)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    resolved_db = db_path if db_path is not None else resolve_db_path(study)
    if not resolved_db.exists():
        typer.echo(f"Error: db_path does not exist: {resolved_db}", err=True)
        raise typer.Exit(code=1)

    access = AccessMode.READ_ONLY if read_only else AccessMode.READ_WRITE
    conn = connect(resolved_db, access=access)
    try:
        assert_schema_current(conn)
        require_study_identity(conn, study.study_id)
    except (OperatorError, StudyIdentityError) as exc:
        conn.close()
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    return study, conn


@app_typer.command("abandon-case")
def abandon_case_cmd(
    study_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    clinician: str = typer.Option(..., "--clinician", help="Clinician name as typed at login."),
    patient: str = typer.Option(..., "--patient", help="Patient ID of the open case."),
    db_path: Path | None = typer.Option(
        None,
        "--db-path",
        help="SQLite DB; defaults to the study's db_path / data/study_<study_id>.db.",
    ),
) -> None:
    """Mark an open case incomplete (operator_abandoned); never deletes it."""
    from datetime import UTC, datetime

    from ehr_simulator.cli_support import OperatorError, abandon_case
    from ehr_simulator.logging import setup_logging

    setup_logging(Path("logs"))
    _study, conn = _open_study_db(study_path, db_path, read_only=False)
    try:
        clinician_id = abandon_case(
            conn, clinician_name=clinician, patient_id=patient, now=datetime.now(UTC)
        )
    except OperatorError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    typer.echo(
        f"Case {patient} for clinician {clinician_id} is now incomplete (operator_abandoned)."
    )


@app_typer.command("case-status")
def case_status_cmd(
    study_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    db_path: Path | None = typer.Option(
        None,
        "--db-path",
        help="SQLite DB; defaults to the study's db_path / data/study_<study_id>.db.",
    ),
) -> None:
    """Read-only lifecycle counts per clinician against the study's limits."""
    from ehr_simulator.cli_support import case_status, format_case_status

    study, conn = _open_study_db(study_path, db_path, read_only=True)
    try:
        report = case_status(conn, study)
    finally:
        conn.close()

    typer.echo(format_case_status(report))


# ---------------------------------------------------------------------------
# export-answers (S9c)
# ---------------------------------------------------------------------------


@app_typer.command("export-answers")
def export_answers(
    study_path: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="Path to study_config.yaml."
    ),
    questions_path: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="Path to questions.yaml."
    ),
    db_path: Path | None = typer.Option(
        None,
        "--db-path",
        help="SQLite DB; defaults to the study's db_path / data/study_<study_id>.db.",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Answers CSV destination; defaults to a UTC-timestamped file in <db dir>/exports/.",
    ),
    keyfile: Path | None = typer.Option(
        None,
        "--keyfile",
        help="Optional POSIX 0600 id→name keyfile covering exactly the clinicians in this export.",
    ),
    only_complete: bool = typer.Option(False, "--only-complete"),
    force: bool = typer.Option(
        False, "--force", help="Replace an existing output path (both --out and --keyfile)."
    ),
) -> None:
    """Export the study's recorded answers to a guarded, analysis-ready CSV."""
    from datetime import UTC, datetime

    from ehr_simulator import export
    from ehr_simulator.cli_support import OperatorError, assert_schema_current
    from ehr_simulator.config import (
        compute_config_hash_from_models,
        load_questions,
        load_study_config,
    )
    from ehr_simulator.db import connect, resolve_db_path
    from ehr_simulator.db.connection import AccessMode
    from ehr_simulator.db.exceptions import StudyIdentityError
    from ehr_simulator.db.study_identity import require as require_study_identity
    from ehr_simulator.logging import get_logger, setup_logging

    setup_logging(Path("logs"))

    try:
        study = load_study_config(study_path)
        questions = load_questions(questions_path)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    bundle: export.ExportBundle
    try:
        live_hash = compute_config_hash_from_models(study, questions)
        target_db = resolve_db_path(study, cli_override=db_path)
        if not target_db.exists():
            raise export.ExportError(f"database not found: {target_db}")

        conn = connect(target_db, access=AccessMode.READ_ONLY)
        try:
            assert_schema_current(conn)
            require_study_identity(conn, study.study_id)
            bundle = export.build_export(
                conn,
                study=study,
                questions=questions,
                live_hash=live_hash,
                options=export.ExportOptions(only_complete=only_complete),
                include_keyfile=keyfile is not None,
            )
            if out is None:
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                out = target_db.parent / "exports" / f"answers_{stamp}.csv"
            export.write_export(
                bundle,
                out=out,
                keyfile=keyfile,
                force=force,
            )
        finally:
            conn.close()
    except (
        ConfigError,
        export.ExportError,
        OperatorError,
        StudyIdentityError,
        OSError,
        sqlite3.Error,
    ) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    report = bundle.frame.report
    get_logger().info(
        "export.written",
        event_kind="export.written",
        rows=report.rows,
        columns=report.columns,
        patients=report.patients,
        clinicians=report.clinicians,
        complete_walks=report.complete_walks,
        in_progress_walks=report.in_progress_walks,
    )
    walk_word = "walk" if report.complete_walks + report.in_progress_walks == 1 else "walks"
    clin_word = "clinician" if report.clinicians == 1 else "clinicians"
    patient_word = "patient" if report.patients == 1 else "patients"
    typer.echo(
        f"Wrote {report.rows} rows × {report.columns} columns "
        f"for {report.patients} {patient_word}, {report.clinicians} {clin_word} "
        f"({report.complete_walks} complete {walk_word}, {report.in_progress_walks} in progress) "
        f"to {out}"
    )
    if keyfile is not None:
        n = len(bundle.keyfile_rows or ())
        typer.echo(f"Wrote keyfile ({n} {clin_word}, mode 600) to {keyfile}")


# ---------------------------------------------------------------------------
# divergence-view (S10)
# ---------------------------------------------------------------------------


@app_typer.command("divergence-view")
def divergence_view(
    study_path: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="Path to study_config.yaml."
    ),
    questions_path: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="Path to questions.yaml."
    ),
    db_path: Path | None = typer.Option(
        None,
        "--db-path",
        help="SQLite DB; defaults to the study's db_path / data/study_<study_id>.db.",
    ),
    patient: str = typer.Option(
        ..., "--patient", help="One configured patient id; renders one figure."
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="SVG destination; defaults to <db dir>/divergence_<patient>.svg.",
    ),
) -> None:
    """Render the rough per-patient divergence figure (SVG) from the study DB."""
    from ehr_simulator import divergence
    from ehr_simulator.cli_support import OperatorError, assert_schema_current, build_dataset_loader
    from ehr_simulator.config import (
        compute_config_hash_from_models,
        load_questions,
        load_study_config,
    )
    from ehr_simulator.db import connect, resolve_db_path
    from ehr_simulator.db.connection import AccessMode
    from ehr_simulator.db.exceptions import StudyIdentityError
    from ehr_simulator.db.study_identity import require as require_study_identity
    from ehr_simulator.logging import get_logger, setup_logging

    setup_logging(Path("logs"))

    try:
        study = load_study_config(study_path)
        questions = load_questions(questions_path)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    live_hash = compute_config_hash_from_models(study, questions)
    target_db = resolve_db_path(study, cli_override=db_path)
    if not target_db.exists():
        typer.echo(f"Error: database not found: {target_db}", err=True)
        raise typer.Exit(code=1)

    try:
        conn = connect(target_db, access=AccessMode.READ_ONLY)
        try:
            assert_schema_current(conn)
            require_study_identity(conn, study.study_id)
            # S11a: the study dataset is loaded only **after** identity
            # verification succeeds — no study data is read before the
            # database identity has been checked.
            dataset = build_dataset_loader(study)()
            fig = divergence.build_divergence_figure(
                conn,
                study=study,
                questions=questions,
                live_hash=live_hash,
                patient_id=patient,
                dataset=dataset,
            )
        finally:
            conn.close()
        if out is None:
            out = target_db.parent / f"divergence_{patient}.svg"
        fig.save(out)
    except (
        ConfigError,
        divergence.DivergenceError,
        OperatorError,
        StudyIdentityError,
        OSError,
        sqlite3.Error,
    ) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    get_logger().info(
        "divergence.written", event_kind="divergence.written", patient=patient, svg=str(out)
    )
    typer.echo(
        f"Wrote divergence figure for patient {patient} to {out} "
        f"(descriptive only; arms: study config + recorded answers)"
    )


# ---------------------------------------------------------------------------
# activate-config (S11b)
# ---------------------------------------------------------------------------


@app_typer.command("activate-config")
def activate_config_cmd(
    study_path: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="Path to study_config.yaml."
    ),
    questions_path: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="Path to questions.yaml."
    ),
    version: str = typer.Option(
        ..., "--version", help="Configuration version label (e.g. v1, baseline, 20260924)."
    ),
    description: str = typer.Option(
        ..., "--description", help="What this configuration changes or establishes (max 500 chars)."
    ),
    reason: str | None = typer.Option(
        None, "--reason", help="Why the change was made now (optional, max 1000 chars)."
    ),
    db_path: Path | None = typer.Option(
        None,
        "--db-path",
        help="SQLite DB; defaults to the study's db_path / data/study_<study_id>.db.",
    ),
) -> None:
    """Register the given config as a new configuration version and make it active."""
    from ehr_simulator.cli_support import OperatorError, activate_for_cli
    from ehr_simulator.config import load_questions, load_study_config
    from ehr_simulator.db import resolve_db_path
    from ehr_simulator.db.exceptions import ConfigurationActivationError, StudyIdentityError
    from ehr_simulator.logging import setup_logging

    setup_logging(Path("logs"))
    try:
        study = load_study_config(study_path)
        questions = load_questions(questions_path)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    resolved_db = db_path if db_path is not None else resolve_db_path(study)
    try:
        report = activate_for_cli(
            study=study,
            questions=questions,
            db_path=resolved_db,
            version=version,
            description=description,
            reason=reason,
        )
    except (
        OperatorError,
        ConfigurationActivationError,
        StudyIdentityError,
        sqlite3.Error,
    ) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if report.was_noop:
        typer.echo(
            f"Configuration {report.config_version!r} is already registered with "
            "identical hash and metadata.\n"
            f"No change was made. Active configuration remains {report.active_version!r} "
            f"for study {study.study_id!r}."
        )
        return
    typer.echo(
        f"Activated configuration {report.config_version!r} "
        f"(config_hash {report.config_hash[:12]}…, "
        f"{report.change_description!r}) as the active configuration for "
        f"study {study.study_id!r}."
    )


if __name__ == "__main__":  # pragma: no cover - manual smoke
    sys.exit(main())
