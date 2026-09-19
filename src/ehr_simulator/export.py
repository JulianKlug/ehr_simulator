"""Analysis-ready CSV export of recorded answers (S9c).

Spec: ``specs/session-09c-csv-export.md``.

Layering::

    cli.py  →  ehr_simulator.export (this module)
             →  ehr_simulator.db.* (read DAOs)
             →  ehr_simulator.answer_codec (strict decode)
             →  ehr_simulator.timing (S10 derivation, pure)
             →  ehr_simulator.config (models, hash)

This module **never imports ``ehr_simulator.web``** — the UI layer sits
above it, and importing it from a batch tool would create a cycle.

``build_export`` takes one read-only :class:`sqlite3.Connection`, snapshots
the relevant tables inside a single explicit transaction
(``BEGIN`` … ``ROLLBACK`` — never ``COMMIT``; the connection stays
read-only), and returns an immutable :class:`ExportBundle`. No file I/O
happens until :func:`write_export` runs — and it refuses before writing
anything when the target exists and ``force`` is not set.

Every rejections raise :class:`ExportError` (a ``ValueError``) with an
operator-facing message that names the failure *shape* and coordinates
— never the free-text content of an answer.
"""

from __future__ import annotations

import contextlib
import csv
import io
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ehr_simulator import timing
from ehr_simulator.answer_codec import AnswerValidationError, decode_stored_answer
from ehr_simulator.config import Question, Questions, StudyConfig
from ehr_simulator.db import answers, arm_assignments, clinicians, progress

__all__ = [
    "METADATA_COLUMNS",
    "ExportBundle",
    "ExportError",
    "ExportFrame",
    "ExportOptions",
    "ExportReport",
    "build_export",
    "encode_multi_select",
    "guard_cell",
    "write_csv",
    "write_export",
    "write_keyfile",
]

# Static export columns, in order. Question columns follow, in
# questions.yaml order. S10 adds the three timing columns (blank when the
# pair has no paired enter/exit at that timepoint — never interpolated).
METADATA_COLUMNS: tuple[str, ...] = (
    "patient_id",
    "clinician_id",
    "t_index",
    "timepoint_minutes",
    "timepoint_started_at",
    "timepoint_ended_at",
    "elapsed_seconds",
    "arm",
    "completed_at",
    "config_hash",
)

_KEYFILE_HEADER: tuple[str, str] = ("clinician_id", "name_normalized")


class ExportError(ValueError):
    """Research export cannot be produced faithfully.

    Messages are operator-facing and never reveal free-text answer content.
    """


@dataclass(frozen=True)
class ExportOptions:
    only_complete: bool = False


@dataclass(frozen=True)
class ExportReport:
    rows: int
    columns: int
    patients: int
    clinicians: int
    complete_walks: int
    in_progress_walks: int
    config_hash: str


@dataclass(frozen=True)
class ExportFrame:
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    report: ExportReport


@dataclass(frozen=True)
class ExportBundle:
    frame: ExportFrame
    keyfile_rows: tuple[tuple[str, str], ...] | None


def encode_multi_select(options: list[str]) -> str:
    """Pipe-join options (questions.yaml order) for the CSV cell."""
    return "|".join(options)


def guard_cell(value: str) -> str:
    """Prefix ``'`` if *value* would be read as a formula by spreadsheet readers.

    Guards against the formula triggers (``= + - @ TAB CR LF`` and the
    full-width variants ``＝＋－＠``) that Excel and Google Sheets
    auto-execute at the start of a cell. A value beginning with `'` is
    unchanged, and an empty value is unchanged.
    """
    if not value:
        return value
    if _FORMULA_TRIGGERS.issuperset(value[:1]):
        return "'" + value
    return value


_FORMULA_TRIGGERS = frozenset("=+-@\t\r\n\uff1d\uff0b\uff0d\uff20")  # = + - @ ＝ ＋ － ＠


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_export(
    conn: Any,
    *,
    study: StudyConfig,
    questions: Questions,
    live_hash: str,
    options: ExportOptions,
    include_keyfile: bool = False,
) -> ExportBundle:
    """Read + validate + compute the export (no file I/O).

    All rejections raise :class:`ExportError`. All reads happen inside
    one explicit ``BEGIN`` … ``ROLLBACK`` read transaction — a WAL
    snapshot taken at the first read is what every DAO read sees, so an
    aborting caller can never observe a torn state.
    """
    by_id = {q.question_id: q for q in questions.questions}
    for column in METADATA_COLUMNS:
        if column in by_id:
            raise ExportError(
                f"question_id {column!r} collides with an exported metadata column; "
                "rename the question in questions.yaml"
            )
    if not study.timepoints_minutes:
        raise ExportError("the study config has no timepoints; nothing to export")

    if conn.in_transaction:
        raise ExportError(
            "cannot export inside an open transaction; build_export takes its own snapshot — "
            "use one connection per export"
        )

    conn.execute("BEGIN")
    try:
        answer_rows = tuple(answers.fetch_all(conn))
        progress_rows = tuple(progress.fetch_all(conn).values())
        assignment_rows = tuple(arm_assignments.fetch_all(conn))
        clinician_ids = set(clinicians.fetch_all_ids(conn))
        # S10: the enter/exit history must be read in the same snapshot as
        # the answers it will be paired with (spec §4, rejection list).
        timing_events = timing.fetch_timing_events(conn)
        return _build_under_snapshot(
            conn,
            study=study,
            questions=questions,
            by_id=by_id,
            live_hash=live_hash,
            options=options,
            include_keyfile=include_keyfile,
            answer_rows=answer_rows,
            progress_rows=progress_rows,
            assignment_rows=assignment_rows,
            clinician_ids=clinician_ids,
            timing_events=timing_events,
        )
    finally:
        conn.rollback()


def _build_under_snapshot(
    conn: Any,
    *,
    study: StudyConfig,
    questions: Questions,
    by_id: dict[str, Question],
    live_hash: str,
    options: ExportOptions,
    include_keyfile: bool,
    answer_rows: tuple[Any, ...],
    progress_rows: tuple[Any, ...],
    assignment_rows: tuple[Any, ...],
    clinician_ids: set[str],
    timing_events: tuple[timing.TimingEvent, ...],
) -> ExportBundle:
    patient_ids = list(study.patient_ids)
    pid_rank = {pid: i for i, pid in enumerate(patient_ids)}
    tp_index = {tp: i for i, tp in enumerate(study.timepoints_minutes)}
    n_tp = len(study.timepoints_minutes)

    # -- 1) Config-generation integrity (spec §8.3) ----------------------
    _refuse_hash_drift(
        live_hash,
        ("answers", answer_rows),
        ("progress", progress_rows),
        ("arm_assignments", assignment_rows),
    )

    # -- 2) Referential integrity ---------------------------------------
    for table in ("answers", "progress", "arm_assignments"):
        table_rows = {
            "answers": answer_rows,
            "progress": progress_rows,
            "arm_assignments": assignment_rows,
        }[table]
        for row in table_rows:
            if row.patient_id not in pid_rank:
                raise ExportError(
                    f"{table} row for patient {row.patient_id!r}, clinician {row.clinician_id} "
                    "references a patient the study config does not know; the DB was written "
                    "under a different study config"
                )
    for row in answer_rows:
        if row.question_id not in by_id:
            raise ExportError(
                f"answers row for patient {row.patient_id!r}, clinician {row.clinician_id}, "
                f"timepoint {row.timepoint} references unknown question {row.question_id!r}; "
                "the DB was written under a different questions.yaml"
            )
        if row.timepoint not in tp_index:
            raise ExportError(
                f"answers row for patient {row.patient_id!r}, clinician {row.clinician_id} "
                f"references timepoint {row.timepoint} the study config does not know; the DB "
                "was written under a different study config"
            )
    # S10 (spec §4): timing rows for an unknown clinician, patient, or
    # timepoint refuse the export — a foreign event would otherwise leak
    # into a derived timing field of a real pair.
    for ev in timing_events:
        if ev.clinician_id not in clinician_ids:
            raise ExportError(
                f"timepoint enter/exit event for unknown clinician {ev.clinician_id!r} "
                f"(patient {ev.patient_id!r}, timepoint {ev.timepoint}); the DB was written "
                "under a different study config"
            )
        if ev.patient_id is None or ev.patient_id not in pid_rank:
            raise ExportError(
                f"timepoint enter/exit event for patient {ev.patient_id!r} the study config "
                f"does not know (clinician {ev.clinician_id!r}); the DB was written under a "
                "different study config"
            )
        if ev.timepoint is None or ev.timepoint not in tp_index:
            raise ExportError(
                f"timepoint enter/exit event for timepoint {ev.timepoint!r} the study config "
                f"does not know (patient {ev.patient_id!r}, clinician {ev.clinician_id!r}); "
                "the DB was written under a different study config"
            )

    # -- 3) Group cells per pair; validate progress (spec §6.4) ----------
    cells: dict[tuple[str, str], dict[int, dict[str, str]]] = {}
    for row in answer_rows:
        pair = (row.clinician_id, row.patient_id)
        idx = tp_index[row.timepoint]
        cells.setdefault(pair, {}).setdefault(idx, {})[row.question_id] = row.value
    # unique (pair, t, question) is enforced by the ux_answers_cell index

    frontier: dict[tuple[str, str], int] = {}
    completed: dict[tuple[str, str], Any] = {}
    for row in progress_rows:
        pair = (row.clinician_id, row.patient_id)
        if pair in frontier:
            raise ExportError(
                f"multiple progress rows for patient {row.patient_id!r}, "
                f"clinician {row.clinician_id}; the progress table is corrupt"
            )
        if not 0 <= row.unlocked_t_index < n_tp:
            raise ExportError(
                f"progress frontier {row.unlocked_t_index} for patient {row.patient_id!r}, "
                f"clinician {row.clinician_id} is out of study range (0..{n_tp - 1})"
            )
        if row.completed_at is not None and row.unlocked_t_index != n_tp - 1:
            raise ExportError(
                "progress for "
                f"patient {row.patient_id!r}, clinician {row.clinician_id} is marked "
                f"completed at frontier {row.unlocked_t_index}, but the study's final index is "
                f"{n_tp - 1}"
            )
        frontier[pair] = row.unlocked_t_index
        if row.completed_at is not None:
            completed[pair] = row.completed_at

    pairs = set(cells) | set(frontier)
    for pair in pairs:
        if pair not in frontier:
            # Answer-only pair: its frontier is the highest live timepoint
            # it answered at (spec §6.1). It can never be complete.
            frontier[pair] = max(cells[pair])
            continue
        max_answer = max(cells.get(pair, {}), default=-1)
        if max_answer > frontier[pair]:
            raise ExportError(
                f"answers exist beyond the progress frontier for patient {pair[1]!r}, clinician "
                f"{pair[0]}: frontier is {frontier[pair]} but an answer sits at index "
                f"{max_answer}; the DB was not written by the simulator"
            )

    # -- 4) Arm integrity (spec §8.4): every pair must carry one arm -----
    arms: dict[tuple[str, str], str] = {}
    for row in assignment_rows:
        pair = (row.clinician_id, row.patient_id)
        if pair in arms and arms[pair] != row.arm:
            raise ExportError(
                f"conflicting arm assignments for patient {row.patient_id!r}, "
                f"clinician {row.clinician_id}: {arms[pair]!r} vs {row.arm!r}"
            )
        arms.setdefault(pair, row.arm)
    for pair in pairs:
        if pair not in arms:
            raise ExportError(
                f"no arm assignment for patient {pair[1]!r}, clinician {pair[0]}; "
                "every exported pair must have locked an arm at session start"
            )

    for row in answer_rows:
        pair = (row.clinician_id, row.patient_id)
        if row.arm != arms[pair]:
            raise ExportError(
                f"arm mismatch for patient {pair[1]!r}, clinician {pair[0]}: assignment is "
                f"{arms[pair]!r} but an answer row carries {row.arm!r}"
            )

    # -- 5) Strict decode of every stored cell ----------------------------
    decoded: dict[tuple[str, str, int, str], str] = {}
    for (cid, pid), per_tp in cells.items():
        for idx, per_q in per_tp.items():
            for qid, stored in per_q.items():
                try:
                    value = decode_stored_answer(by_id[qid], stored)
                except AnswerValidationError as exc:
                    raise ExportError(
                        f"invalid persisted answer for patient {pid!r}, clinician {cid}, "
                        f"timepoint {repr(float(study.timepoints_minutes[idx]))}, "
                        f"question {qid}: {exc}"
                    ) from None
                decoded[(cid, pid, idx, qid)] = value

    # -- 6) Selection + ordering (spec §6.2) ------------------------------
    selected = frozenset(completed) if options.only_complete else pairs
    question_ids = tuple(q.question_id for q in questions.questions)
    header = METADATA_COLUMNS + question_ids

    ordered = sorted(selected, key=lambda p: (pid_rank[p[1]], p[0]))

    # -- 6b) S10 timing (spec §4): derive per pair; never infer ----------
    # An invalid history (selected exit before the selected enter) raises
    # TimingError → ExportError: the pair's timing fields stay blank and
    # the export is refused rather than half-filled. Other pairs' derivations
    # are unaffected until the error propagates (no rollback needed — the
    # connection only ever saw reads).
    timings: dict[tuple[str, str], dict[float, timing.TimepointTiming]] = {}
    for pair in ordered:
        try:
            timings[pair] = timing.derive_timepoint_timings(
                timing_events, clinician_id=pair[0], patient_id=pair[1]
            )
        except timing.TimingError as exc:
            raise ExportError(f"cannot derive timepoint timing: {exc}") from exc

    rows: list[tuple[str, ...]] = []
    for cid, pid in ordered:
        front = frontier[(cid, pid)]
        done = completed.get((cid, pid))
        completed_str = done.strftime("%Y-%m-%d %H:%M:%S") if done is not None else ""
        pair_timings = timings.get((cid, pid), {})
        for idx in range(0, front + 1):
            tt = pair_timings.get(float(study.timepoints_minutes[idx]))
            elapsed_cell = (
                str(tt.elapsed_seconds) if tt is not None and tt.elapsed_seconds is not None else ""
            )
            row = [
                pid,
                cid,
                str(idx),
                repr(float(study.timepoints_minutes[idx])),
                timing.format_ts(tt.started_at) if tt is not None else "",
                timing.format_ts(tt.ended_at) if tt is not None else "",
                elapsed_cell,
                arms[(cid, pid)],
                completed_str,
                live_hash,
            ]
            row.extend(decoded.get((cid, pid, idx, qid), "") for qid in question_ids)
            rows.append(tuple(row))

    # -- 7) Report + keyfile ----------------------------------------------
    complete_walks = sum(1 for p in ordered if p in completed)
    report = ExportReport(
        rows=len(rows),
        columns=len(header),
        patients=len({pid for _, pid in ordered}),
        clinicians=len({cid for cid, _ in ordered}),
        complete_walks=complete_walks,
        in_progress_walks=len(ordered) - complete_walks,
        config_hash=live_hash,
    )
    frame = ExportFrame(header=header, rows=tuple(rows), report=report)

    keyfile_rows: tuple[tuple[str, str], ...] | None = None
    if include_keyfile:
        ids = tuple(sorted({cid for cid, _ in ordered}))
        keyfile_rows = _fetch_keyfile_rows(conn, ids)

    return ExportBundle(frame=frame, keyfile_rows=keyfile_rows)


def _refuse_hash_drift(live_hash: str, *tables: tuple[str, tuple[Any, ...]]) -> None:
    """Refuse the DB if any row carries a config_hash other than *live_hash*.

    A mixed-generation database is refused, not merged: interpretation of
    a cell depends on the questions.yaml that was live when it was
    recorded, so a single CSV row could mix two meanings.
    """
    foreign: dict[tuple[str, str], int] = {}
    for table, rows in tables:
        for row in rows:
            if row.config_hash != live_hash:
                key = (table, row.config_hash)
                foreign[key] = foreign.get(key, 0) + 1
    if not foreign:
        return
    lines = [
        "database contains records from another study configuration.",
        f"Live config: {live_hash[:4]}…",
        "Foreign records:",
    ]
    for table in ("answers", "progress", "arm_assignments"):
        for h, n in ((hh, nn) for (tt, hh), nn in foreign.items() if tt == table):
            lines.append(f"  {table}:".ljust(18) + f" {h[:4]}… ({n} rows)")
    lines.append(
        "Refusing interpreted CSV export. Re-export under the config that recorded these rows."
    )
    raise ExportError("\n".join(lines))


def _fetch_keyfile_rows(conn: Any, ids: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    if not ids:
        return ()
    rows = clinicians.fetch_by_ids(conn, ids)
    returned = {cid for cid, _ in rows}
    requested = set(ids)
    if returned != requested:
        diff = sorted((requested - returned) or (returned - requested))
        raise ExportError(
            f"clinicians lookup did not round-trip for id {diff[0]!r}; "
            "the clinicians table is inconsistent with the answers table"
        )
    if len(rows) != len(returned):
        raise ExportError(
            "clinicians lookup returned duplicate ids; the clinicians table is corrupt"
        )
    return tuple(rows)


# ---------------------------------------------------------------------------
# Staged write + install
# ---------------------------------------------------------------------------


def write_export(
    bundle: ExportBundle,
    *,
    out: Path,
    keyfile: Path | None = None,
    force: bool = False,
) -> None:
    """Stage every requested artifact before installing either final path.

    Normal validation/refusal cannot create or replace a final output.
    Without ``force`` installation is no-clobber. With ``force``, a fully
    staged file atomically replaces its existing final path.
    """
    out = Path(out)
    keyfile = Path(keyfile) if keyfile is not None else None

    try:
        if keyfile is not None and out.resolve() == keyfile.resolve():
            raise ExportError(
                "--keyfile and --out resolve to the same path; the answers CSV and the keyfile "
                "are different artifacts"
            )

        if keyfile is not None:
            if bundle.keyfile_rows is None:
                raise ExportError(
                    "--keyfile was passed but build_export was called without include_keyfile=True"
                )
            _validate_keyfile_rows(bundle.keyfile_rows)

        targets = _targets(out, keyfile)
        for target in targets:
            if target.is_dir():
                raise ExportError(f"refusing to replace a pre-existing directory: {target}")

        finals = [target for target in targets if target.exists()]
        if finals and not force:
            listed = ", ".join(str(target) for target in finals)
            raise ExportError(
                f"refusing to overwrite existing output: {listed}; re-run with --force"
            )

        # Parent creation is preflight. No final output path is touched yet.
        for target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)

        rendered: list[tuple[Path, str, str, bool]] = [
            (out, _render_csv(bundle.frame), "csv", False),
        ]
        if keyfile is not None:
            rendered.append(
                (
                    keyfile,
                    _render_keyfile(bundle.keyfile_rows),
                    "keyfile",
                    True,
                )
            )

        staged: list[tuple[Path, Path, str]] = []
        try:
            # Stage and validate every requested artifact first.
            for final, data, what, require_0600 in rendered:
                staged_path = _stage_content(
                    data,
                    final,
                    what,
                    require_0600=require_0600,
                )
                staged.append((staged_path, final, what))

            # Only after all staging succeeds may finals be installed.
            for staged_path, final, what in staged:
                _install_staged(staged_path, final, what, force=force)
        finally:
            for staged_path, _final, _what in staged:
                _cleanup_stage(staged_path)

    except ExportError:
        raise
    except OSError as exc:
        raise ExportError(f"failed to write export: {exc}") from exc


def _targets(out: Path, keyfile: Path | None) -> list[Path]:
    targets = [out]
    if keyfile is not None:
        targets.append(keyfile)
    return targets


def write_csv(frame: ExportFrame, path: Path) -> None:
    """Render and no-clobber-install one answers CSV."""
    _write_one(
        _render_csv(frame),
        Path(path),
        "csv",
        require_0600=False,
    )


def write_keyfile(rows: tuple[tuple[str, str], ...], path: Path) -> None:
    """Render and no-clobber-install one POSIX mode-0600 keyfile."""
    _validate_keyfile_rows(rows)
    _write_one(
        _render_keyfile(rows),
        Path(path),
        "keyfile",
        require_0600=True,
    )


def _render_csv(frame: ExportFrame) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([guard_cell(cell) for cell in frame.header])
    for row in frame.rows:
        writer.writerow([guard_cell(cell) for cell in row])
    return buf.getvalue()


def _render_keyfile(rows: tuple[tuple[str, str], ...]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([guard_cell(cell) for cell in _KEYFILE_HEADER])
    for clinician_id, name_normalized in rows:
        writer.writerow(
            [
                guard_cell(clinician_id),
                guard_cell(name_normalized),
            ]
        )
    return buf.getvalue()


def _validate_keyfile_rows(rows: tuple[tuple[str, str], ...]) -> None:
    if os.name != "posix":
        raise ExportError(
            f"cannot write keyfile on {os.name}: a mode-0600 guarantee for a name-to-id "
            "keyfile is not available on this platform; export without --keyfile"
        )
    if len(rows) != len({clinician_id for clinician_id, _ in rows}):
        raise ExportError(
            "duplicate clinician_id in keyfile rows; a keyfile maps each id exactly once"
        )


def _write_one(
    content: str,
    final: Path,
    what: str,
    *,
    require_0600: bool,
) -> None:
    try:
        if final.is_dir():
            raise ExportError(f"refusing to replace a pre-existing directory: {final}")
        if final.exists():
            raise ExportError(
                f"refusing to overwrite existing {what}: {final}; re-run with --force"
            )

        final.parent.mkdir(parents=True, exist_ok=True)
        staged = _stage_content(
            content,
            final,
            what,
            require_0600=require_0600,
        )
        try:
            _install_staged(staged, final, what, force=False)
        finally:
            _cleanup_stage(staged)

    except ExportError:
        raise
    except OSError as exc:
        raise ExportError(f"failed to write {what}: {exc}") from exc


def _stage_content(
    content: str,
    final: Path,
    what: str,
    *,
    require_0600: bool,
) -> Path:
    """Write a completed sibling temp file without touching the final path."""
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{final.name}.",
        suffix=".tmp",
        dir=str(final.parent),
    )
    tmp = Path(tmp_name)

    try:
        if require_0600:
            created_mode = stat.S_IMODE(os.fstat(fd).st_mode)
            if created_mode != 0o600:
                raise ExportError(
                    f"{what} staging file has mode {created_mode:#o}, not 0600; "
                    "refusing to write identifying data"
                )

        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        if require_0600:
            staged_mode = stat.S_IMODE(tmp.stat().st_mode)
            if staged_mode != 0o600:
                raise ExportError(
                    f"{what} staging file has mode {staged_mode:#o}, not 0600; "
                    "refusing to write identifying data"
                )

        return tmp

    except BaseException:
        if fd != -1:
            os.close(fd)
        _cleanup_stage(tmp)
        raise


def _install_staged(
    staged: Path,
    final: Path,
    what: str,
    *,
    force: bool,
) -> None:
    """Install one fully staged artifact.

    ``os.link`` gives no-clobber semantics. ``os.replace`` preserves the
    old destination until the replacement is fully staged.
    """
    if force:
        os.replace(staged, final)
        return

    try:
        os.link(staged, final)
    except FileExistsError as exc:
        raise ExportError(
            f"refusing to overwrite existing {what}: {final}; re-run with --force"
        ) from exc


def _cleanup_stage(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
