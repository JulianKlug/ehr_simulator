"""``activate-config`` (S11b): register and activate a configuration version."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import typer

from ehr_simulator.cli._common import DB_PATH_HELP, LOG_DIR, app_typer
from ehr_simulator.config import ConfigError


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
        help=DB_PATH_HELP,
    ),
) -> None:
    """Register the given config as a new configuration version and make it active."""
    from ehr_simulator.cli_support import OperatorError, activate_for_cli
    from ehr_simulator.config import load_questions, load_study_config, validate_study_questions
    from ehr_simulator.db import resolve_db_path
    from ehr_simulator.db.exceptions import ConfigurationActivationError, StudyIdentityError
    from ehr_simulator.logging import setup_logging

    setup_logging(LOG_DIR)
    try:
        study = load_study_config(study_path)
        questions = load_questions(questions_path)
        validate_study_questions(study, questions)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    resolved_db = resolve_db_path(study, cli_override=db_path)
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
