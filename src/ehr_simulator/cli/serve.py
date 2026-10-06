"""``serve``: boot uvicorn against the FastAPI app."""

from __future__ import annotations

from pathlib import Path

import typer
import uvicorn

from ehr_simulator.cli._common import LOG_DIR, app_typer
from ehr_simulator.config import ConfigError


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
            log_dir=LOG_DIR,
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
            log_dir=LOG_DIR,
            db_path=db_path,
            backup_dir=backup_dir,
        )
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    uvicorn.run(app_instance, host=host, port=port, reload=reload)
