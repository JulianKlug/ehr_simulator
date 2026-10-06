"""``validate-config``, ``validate-adapter``, ``preflight``, ``preview``."""

from __future__ import annotations

from pathlib import Path

import typer

from ehr_simulator.cli._common import LOG_DIR, app_typer
from ehr_simulator.config import ConfigError


@app_typer.command("validate-config")
def validate_config(
    study_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    questions_path: Path = typer.Argument(..., exists=True, dir_okay=False),
) -> None:
    """Validate study_config.yaml + questions.yaml shape."""
    from ehr_simulator.config import load_questions, load_study_config, validate_study_questions

    try:
        study = load_study_config(study_path)
        questions_obj = load_questions(questions_path)
        validate_study_questions(study, questions_obj)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        f"OK: {study_path} ({len(study.patient_ids)} patients, "
        f"{len(study.timepoints)} timepoints), "
        f"{questions_path} ({len(questions_obj.questions)} questions, "
        f"schema_version={questions_obj.schema_version})"
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
    _echo_ai_provenance(dataset)
    issues = getattr(dataset, "issues", [])
    if issues:
        typer.echo(f"Issues:     {len(issues)}")
        for issue in issues:
            typer.echo(f"  {issue.dataset}: {issue.reason}")
    else:
        typer.echo("Issues:     0")


def _echo_ai_provenance(dataset) -> None:  # type: ignore[no-untyped-def]
    """S7: the loaded digests an operator copies into ``ai_intervention``; never payloads."""
    from ehr_simulator.ingestion.provenance import ai_provenance_of

    provenance = ai_provenance_of(dataset)
    if provenance is None:
        return
    typer.echo(f"AI patients: {dataset.ai_output['patient_id'].nunique()}")
    typer.echo("AI provenance:")
    typer.echo(f"  prediction_artifact_sha256:  {provenance.prediction_sha256}")
    typer.echo(f"  explanation_artifact_sha256: {provenance.explanation_sha256 or 'none'}")
    typer.echo(f"  model_system_version:        {provenance.model_system_version}")


def _strict_loader(study):  # type: ignore[no-untyped-def]
    """Strict-mode equivalent of :func:`cli_support.build_dataset_loader`.

    Used only by ``validate-adapter --strict``. ``build_dataset_loader``
    is the lenient/runtime path (issues collected, never raises); this is
    the failing-fast path the CLI exposes for CI gates.
    """
    if study.dataset == "geneva":
        from ehr_simulator.cli_support import geneva_ai_source
        from ehr_simulator.ingestion.geneva import load_geneva

        def _go():  # type: ignore[no-untyped-def]
            return load_geneva(
                study.csv_path, study.params_dir, strict=True, ai_source=geneva_ai_source(study)
            )

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
                log_dir=LOG_DIR,
                out_dir=html_out,
            )
        except ConfigError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"Wrote {len(written)} HTML files under {html_out}")
