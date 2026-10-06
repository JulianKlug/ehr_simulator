"""Read-only research outputs.

``export-answers`` (S9c), ``export-phase2`` (S11n), ``divergence-view`` (S10).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import typer

from ehr_simulator.cli._common import (
    DB_PATH_HELP,
    EXPORT_STAMP_FORMAT,
    LOG_DIR,
    PSEUDONYM_SECRET_HELP,
    MissingDb,
    app_typer,
    existing_db_path,
    study_db,
)
from ehr_simulator.config import ConfigError


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
        help=DB_PATH_HELP,
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
    pseudonym_secret: Path = typer.Option(
        ...,
        "--pseudonym-secret",
        help=PSEUDONYM_SECRET_HELP,
    ),
    only_complete: bool = typer.Option(False, "--only-complete"),
    force: bool = typer.Option(
        False, "--force", help="Replace an existing output path (both --out and --keyfile)."
    ),
) -> None:
    """Export the study's recorded answers to a guarded, analysis-ready CSV."""
    from datetime import UTC, datetime

    from ehr_simulator import case_lifecycle, export, pseudonym
    from ehr_simulator.cli_support import OperatorError
    from ehr_simulator.config import (
        compute_config_hash_from_models,
        load_questions,
        load_study_config,
    )
    from ehr_simulator.db.connection import AccessMode
    from ehr_simulator.db.exceptions import ConfigurationProvenanceError, StudyIdentityError
    from ehr_simulator.logging import get_logger, setup_logging

    setup_logging(LOG_DIR)

    try:
        study = load_study_config(study_path)
        questions = load_questions(questions_path)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    bundle: export.ExportBundle
    try:
        live_hash = compute_config_hash_from_models(study, questions)
        target_db = existing_db_path(study, db_path, MissingDb.DATABASE)
        if out is None:
            stamp = datetime.now(UTC).strftime(EXPORT_STAMP_FORMAT)
            out = target_db.parent / "exports" / f"answers_{stamp}.csv"
        pseudonym.require_outside_outputs(
            pseudonym_secret, files=[out] if keyfile is None else [out, keyfile]
        )
        secret = pseudonym.load_or_create_secret(pseudonym_secret)

        with study_db(study, target_db, AccessMode.READ_ONLY) as conn:
            # S11e: cases no contact will ever time out still read as open.
            overdue = case_lifecycle.overdue_cases(conn, datetime.now(UTC))
            bundle = export.build_export(
                conn,
                study=study,
                questions=questions,
                live_hash=live_hash,
                options=export.ExportOptions(only_complete=only_complete),
                pseudonym_secret=secret,
                include_keyfile=keyfile is not None,
            )
            export.write_export(
                bundle,
                out=out,
                keyfile=keyfile,
                force=force,
            )
    except (
        ConfigError,
        export.ExportError,
        pseudonym.PseudonymSecretError,
        OperatorError,
        StudyIdentityError,
        ConfigurationProvenanceError,
        OSError,
        sqlite3.Error,
    ) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if overdue:
        typer.echo(
            f"Warning: {len(overdue)} open case(s) are past their grace and export as "
            "open; run expire-cases first.",
            err=True,
        )

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
# export-phase2 (S11n)
# ---------------------------------------------------------------------------


@app_typer.command("export-phase2")
def export_phase2(
    study_path: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="Path to study_config.yaml (identifies the study)."
    ),
    db_path: Path | None = typer.Option(
        None,
        "--db-path",
        help=DB_PATH_HELP,
    ),
    out_dir: Path | None = typer.Option(
        None,
        "--out-dir",
        help="Bundle directory; defaults to <db dir>/exports/phase2_<study_id>_<UTC>/.",
    ),
    keyfile: Path | None = typer.Option(
        None,
        "--keyfile",
        help="Optional POSIX 0600 id→name keyfile, outside --out-dir, for the bundle's clinicians.",
    ),
    pseudonym_secret: Path = typer.Option(
        ...,
        "--pseudonym-secret",
        help=PSEUDONYM_SECRET_HELP + " Must lie outside --out-dir.",
    ),
    include_practice: bool = typer.Option(
        False, "--include-practice", help="Add practice_timepoints.csv and practice_answers.csv."
    ),
    force: bool = typer.Option(
        False, "--force", help="Replace an existing --out-dir (and --keyfile) after a full build."
    ),
) -> None:
    """Export the linked Phase 2 research bundle (every configuration version)."""
    from datetime import UTC, datetime

    from ehr_simulator import case_lifecycle, export_bundle, export_phase2, pseudonym
    from ehr_simulator.cli_support import OperatorError
    from ehr_simulator.config import load_study_config
    from ehr_simulator.db.connection import AccessMode
    from ehr_simulator.db.exceptions import StudyIdentityError
    from ehr_simulator.logging import get_logger, setup_logging

    setup_logging(LOG_DIR)
    try:
        study = load_study_config(study_path)
        target_db = existing_db_path(study, db_path, MissingDb.DATABASE)
        if out_dir is None:
            stamp = datetime.now(UTC).strftime(EXPORT_STAMP_FORMAT)
            out_dir = target_db.parent / "exports" / f"phase2_{study.study_id}_{stamp}"
        if out_dir.exists() and not force:
            raise OperatorError(f"{out_dir} already exists; pass --force to replace it")
        pseudonym.require_outside_outputs(
            pseudonym_secret, directories=[out_dir], files=[] if keyfile is None else [keyfile]
        )
        secret = pseudonym.load_or_create_secret(pseudonym_secret)

        with study_db(study, target_db, AccessMode.READ_ONLY) as conn:
            overdue = case_lifecycle.overdue_cases(conn, datetime.now(UTC))
            bundle = export_phase2.build_phase2_bundle(
                conn,
                study_id=study.study_id,
                pseudonym_secret=secret,
                practice_export=(
                    export_phase2.PracticeExport.INCLUDE
                    if include_practice
                    else export_phase2.PracticeExport.EXCLUDE
                ),
                keyfile=(
                    export_phase2.KeyfileRequest.REQUESTED
                    if keyfile is not None
                    else export_phase2.KeyfileRequest.NONE
                ),
            )

        export_bundle.write_bundle(
            bundle,
            out_dir,
            overwrite=export_bundle.Overwrite.REPLACE if force else export_bundle.Overwrite.REFUSE,
            keyfile=keyfile,
        )
    except (
        ConfigError,
        OperatorError,
        StudyIdentityError,
        export_phase2.Phase2ExportError,
        export_bundle.BundleWriteError,
        pseudonym.PseudonymSecretError,
        OSError,
        sqlite3.Error,
    ) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if overdue:
        typer.echo(
            f"Warning: {len(overdue)} open case(s) are past their grace and export as "
            "open; run expire-cases first.",
            err=True,
        )
    rows = {t.name: len(t.rows) for t in bundle.tables}
    get_logger().info("export.phase2.written", event_kind="export.phase2.written", **rows)
    typer.echo(
        f"Wrote Phase 2 bundle ({len(bundle.tables)} files, "
        f"{len(bundle.config_versions)} configuration version(s)) to {out_dir}"
    )
    if keyfile is not None:
        n = len(bundle.keyfile_rows or ())
        typer.echo(f"Wrote keyfile ({n} clinician(s), mode 600) to {keyfile}")


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
        help=DB_PATH_HELP,
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
    from ehr_simulator.cli_support import OperatorError, build_dataset_loader
    from ehr_simulator.config import (
        compute_config_hash_from_models,
        load_questions,
        load_study_config,
    )
    from ehr_simulator.db.connection import AccessMode
    from ehr_simulator.db.exceptions import StudyIdentityError
    from ehr_simulator.ingestion.exceptions import AdapterError
    from ehr_simulator.logging import get_logger, setup_logging

    setup_logging(LOG_DIR)

    try:
        study = load_study_config(study_path)
        questions = load_questions(questions_path)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    live_hash = compute_config_hash_from_models(study, questions)
    target_db = existing_db_path(study, db_path, MissingDb.DATABASE)

    try:
        with study_db(study, target_db, AccessMode.READ_ONLY) as conn:
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
        if out is None:
            out = target_db.parent / f"divergence_{patient}.svg"
        fig.save(out, verbose=False)
    except (
        AdapterError,
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
