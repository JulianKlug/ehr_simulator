"""Operator recovery (S9b, S11e).

``reset-progress``, ``abandon-case``, ``expire-cases``, ``case-status``.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ehr_simulator.cli._common import DB_PATH_HELP, LOG_DIR, _open_study_db, app_typer


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
        help=DB_PATH_HELP,
    ),
) -> None:
    """Rewind a clinician's walk of one patient (recovery for a mis-click on Next)."""
    from ehr_simulator.cli_support import OperatorError, reset_progress
    from ehr_simulator.db import AccessMode
    from ehr_simulator.db.exceptions import StudyIdentityError
    from ehr_simulator.logging import setup_logging

    setup_logging(LOG_DIR)
    with _open_study_db(study_path, db_path, AccessMode.READ_WRITE) as (study, conn):
        try:
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

    state = " (was complete)" if report.was_completed else ""
    typer.echo(
        f"Reset {patient} for clinician {report.clinician_id}: "
        f"frontier {report.previous_unlocked_t_index}{state} → {report.to_t_index}, "
        f"{report.deleted_answers} answer(s) deleted."
    )


@app_typer.command("abandon-case")
def abandon_case_cmd(
    study_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    clinician: str = typer.Option(..., "--clinician", help="Clinician name as typed at login."),
    patient: str = typer.Option(..., "--patient", help="Patient ID of the open case."),
    db_path: Path | None = typer.Option(
        None,
        "--db-path",
        help=DB_PATH_HELP,
    ),
) -> None:
    """Mark an open case incomplete (operator_abandoned); never deletes it."""
    from datetime import UTC, datetime

    from ehr_simulator.cli_support import OperatorError, abandon_case
    from ehr_simulator.db import AccessMode
    from ehr_simulator.logging import setup_logging

    setup_logging(LOG_DIR)
    with _open_study_db(study_path, db_path, AccessMode.READ_WRITE) as (_study, conn):
        try:
            report = abandon_case(
                conn, clinician_name=clinician, patient_id=patient, now=datetime.now(UTC)
            )
        except OperatorError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    typer.echo(
        f"Case {patient} for clinician {report.clinician_id} is now incomplete "
        "(operator_abandoned)."
    )
    if report.replacement_patient_id is not None:
        typer.echo(f"Replacement planned: {report.replacement_patient_id}.")
    if report.planning_error is not None:
        typer.echo(f"Warning: replacement not planned: {report.planning_error}", err=True)


@app_typer.command("expire-cases")
def expire_cases_cmd(
    study_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    db_path: Path | None = typer.Option(
        None,
        "--db-path",
        help=DB_PATH_HELP,
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="List overdue cases; write nothing."),
) -> None:
    """Mark every open case past its grace incomplete, as its next contact would."""
    from datetime import UTC, datetime

    from ehr_simulator.cli_support import ExpiryMode, expire_cases
    from ehr_simulator.db import AccessMode
    from ehr_simulator.db.case_lifecycle import to_db_timestamp
    from ehr_simulator.db.exceptions import ConfigurationProvenanceError
    from ehr_simulator.logging import setup_logging

    setup_logging(LOG_DIR)
    mode = ExpiryMode.DRY_RUN if dry_run else ExpiryMode.APPLY
    access = AccessMode.READ_ONLY if dry_run else AccessMode.READ_WRITE
    with _open_study_db(study_path, db_path, access) as (_study, conn):
        try:
            expired = expire_cases(conn, now=datetime.now(UTC), mode=mode)
        except ConfigurationProvenanceError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    verb = "Would expire" if dry_run else "Expired"
    typer.echo(f"{verb} {len(expired)} case(s).")
    for case in expired:
        line = (
            f"  {case.clinician_id} {case.patient_id} {case.reason} "
            f"(deadline {to_db_timestamp(case.deadline)})"
        )
        if case.replacement_patient_id is not None:
            line += f"; replacement planned: {case.replacement_patient_id}"
        typer.echo(line)
        if case.planning_error is not None:
            typer.echo(f"Warning: replacement not planned: {case.planning_error}", err=True)


@app_typer.command("case-status")
def case_status_cmd(
    study_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    db_path: Path | None = typer.Option(
        None,
        "--db-path",
        help=DB_PATH_HELP,
    ),
) -> None:
    """Read-only lifecycle counts per clinician against the study's limits."""
    from ehr_simulator.cli_support import case_status, format_case_status
    from ehr_simulator.db import AccessMode

    with _open_study_db(study_path, db_path, AccessMode.READ_ONLY) as (study, conn):
        report = case_status(conn, study)

    typer.echo(format_case_status(report))
