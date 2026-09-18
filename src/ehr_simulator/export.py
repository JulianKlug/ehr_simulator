"""Analysis-ready CSV export of recorded answers (S9c).

Spec: ``specs/session-09c-csv-export.md``.

Layering::

    cli.py  →  ehr_simulator.export (this module)
             →  ehr_simulator.db.* (read DAOs)
             →  ehr_simulator.answer_codec (strict decode)
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

import csv
import io
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
# questions.yaml order.
METADATA_COLUMNS: tuple[str, ...] = (
    "patient_id",
    "clinician_id",
    "t_index",
    "timepoint_minutes",
    "arm",
    "completed_at",
    "config_hash",
)

_KEYFILE_HEADER: tuple[str, str] = ("clinician_id", "name_normalized")


class ExportError(ValueError):
    """The export cannot be produced; nothing was written and nothing was logged.

    The message is operator-facing (it goes to stderr) and names what is
    wrong without revealing free-text answer content.
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
        progress_rows = tuple(progress.fetch_all(conn))
        assignment_rows = tuple(arm_assignments.fetch_all(conn))
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
            if (row.clinician_id, row.patient_id) == pair and row.arm != arms[pair]:
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
    rows: list[tuple[str, ...]] = []
    for cid, pid in ordered:
        front = frontier[(cid, pid)]
        done = completed.get((cid, pid))
        completed_str = done.strftime("%Y-%m-%d %H:%M:%S") if done is not None else ""
        for idx in range(0, front + 1):
            row = [
                pid,
                cid,
                str(idx),
                repr(float(study.timepoints_minutes[idx])),
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
    """Install the export at *out* (and the keyfile at *keyfile*, if any).

    Preflight (before any directory is created and any staging begins):

    - ``out`` and ``keyfile`` must not resolve to the same path;
    - a target that exists is refused unless *force*;
    - a target that is a **directory** is always refused (replacement of
      a directory is not supported, even with *force*).

    With *force*, existing final files are unlinked *first*, then the
    writers install via hard link (no-clobber): two concurrent exports
    cannot both claim the same name.
    """
    out = Path(out)
    keyfile = Path(keyfile) if keyfile is not None else None
    if keyfile is not None and out.resolve() == keyfile.resolve():
        raise ExportError(
            "--keyfile and --out resolve to the same path; the answers CSV and the keyfile "
            "are different artifacts"
        )
    for target in _targets(out, keyfile):
        if target.is_dir():
            raise ExportError(f"refusing to replace a pre-existing directory: {target}")
    finals = [t for t in _targets(out, keyfile) if t.exists()]
    if finals and not force:
        listed = ", ".join(str(t) for t in finals)
        raise ExportError(f"refusing to overwrite existing output: {listed}; re-run with --force")
    if force:
        for t in finals:
            t.unlink()
    for target in _targets(out, keyfile):
        target.parent.mkdir(parents=True, exist_ok=True)
    if keyfile is not None and bundle.keyfile_rows is None:
        raise ExportError(
            "--keyfile was passed but build_export was called without include_keyfile=True"
        )
    write_csv(bundle.frame, out)
    if keyfile is not None:
        write_keyfile(bundle.keyfile_rows, keyfile)


def _targets(out: Path, keyfile: Path | None) -> list[Path]:
    targets = [out]
    if keyfile is not None:
        targets.append(keyfile)
    return targets


def write_csv(frame: ExportFrame, path: Path) -> None:
    """Render *frame* as the answers CSV and no-clobber-install it at *path*."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([guard_cell(c) for c in frame.header])
    for row in frame.rows:
        writer.writerow([guard_cell(c) for c in row])
    _stage_and_install(buf.getvalue(), Path(path), "csv")


def write_keyfile(rows: tuple[tuple[str, str], ...], path: Path) -> None:
    """Write the id→name *rows* (0600, POSIX) and no-clobber-install at *path*.

    An empty keyfile is legitimate when the export contains no pairs
    (the header is still written).
    """
    if os.name != "posix":
        raise ExportError(
            f"cannot write keyfile on {os.name}: a mode-0600 guarantee for a name-to-id "
            "keyfile is not available on this platform; export without --keyfile"
        )
    if len(rows) != len({c for c, _ in rows}):
        raise ExportError(
            "duplicate clinician_id in keyfile rows; a keyfile maps each id exactly once"
        )
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([guard_cell(h) for h in _KEYFILE_HEADER])
    for cid, name in rows:
        writer.writerow([guard_cell(cid), guard_cell(name)])
    _stage_and_install(buf.getvalue(), Path(path), "keyfile")


def _stage_and_install(content: str, final: Path, what: str) -> None:
    """Stage *content* mode-0600 in *final*'s directory, hard-link to *final*.

    The hard link is the install step: it fails with ``FileExistsError``
    if *final* already exists (no-clobber, even under *force* after the
    preflight unlink — two racing installs can't both claim the name).
    The staged temp file is always cleaned up.
    """
    final.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(final.parent)) as tmpdir:
        tmp = Path(tmpdir) / ".stage"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp, final)
            except FileExistsError as exc:
                raise ExportError(
                    f"refusing to overwrite existing {what}: {final}; re-run with --force"
                ) from exc
        finally:
            if tmp.exists():
                tmp.unlink()
    final.chmod(0o600)
    installed = stat.S_IMODE(final.stat().st_mode)
    if installed != 0o600:
        raise ExportError(
            f"{what} was installed with mode {installed:#o}, not 0600; refusing to deliver it"
        )
    return None
