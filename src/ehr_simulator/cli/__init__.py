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
  ``STUDY_CONFIG QUESTIONS`` are positional; ``--pseudonym-secret`` is
  required; ``--db-path``/``--out``/``--keyfile``/``--only-complete``/
  ``--force`` round it out. Exit 0 on
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
- ``expire-cases`` (S11e) — mark every open case past its pinned grace
  ``incomplete``; ``--dry-run`` lists them without writing.
- ``case-status`` (S11e) — read-only per-clinician lifecycle counts and
  remaining limits, keyed by ``clinician_id``.


Module layout::

    cli/
      __init__     imports every command module (registers it on app_typer)
      _common      app_typer · main · LOG_DIR · study_db gate
      │              load study → resolve DB → exists → connect
      │              → assert_schema_current → require study identity
      ├─ serve        serve
      ├─ db_ops       migrate · backup
      ├─ validation   validate-config · validate-adapter · preflight · preview
      ├─ operator     reset-progress · abandon-case · expire-cases · case-status
      ├─ exports      export-answers · export-phase2 · divergence-view
      └─ config_cmds  activate-config
    cli_support    services behind the commands (no typer)

The ``main(argv: list[str] | None = None) -> None`` signature is preserved
from the S2 argparse skeleton so ``test_cli.py``'s monkeypatch idiom carries
over for the ``serve`` command.
"""

from __future__ import annotations

from ehr_simulator.cli._common import app_typer, main

# Import order = registration order = ``--help`` command order.
# isort: off
from ehr_simulator.cli import serve
from ehr_simulator.cli import db_ops
from ehr_simulator.cli import validation
from ehr_simulator.cli import operator
from ehr_simulator.cli import exports
from ehr_simulator.cli import config_cmds

# isort: on

__all__ = [
    "app_typer",
    "config_cmds",
    "db_ops",
    "exports",
    "main",
    "operator",
    "serve",
    "validation",
]
