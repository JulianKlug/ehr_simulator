# Session 09c — CSV export (`export-answers`)

**Goal:** a researcher can turn the pilot's SQLite file into one analysis-ready CSV with a single command, without leaking clinician names and without a spreadsheet executing anything a clinician typed. S9c adds `ehr-simulator export-answers STUDY_CONFIG QUESTIONS [--db-path P] [--out F] [--keyfile K] [--only-complete] [--allow-mixed-config] [--force]`: one row per `(patient_id, clinician_id, timepoint)`, one column per `question_id` in `questions.yaml` order, plus `arm`, `t_index`, `config_hash` and the walk's `completed_at`. Multi-select cells are pipe-delimited. Every cell passes a **CSV-injection guard**. The file carries the 16-hex `clinician_id` only; the `clinician_id → name` mapping is written to a separate, opt-in, mode-0600 keyfile that lives under the (gitignored) DB directory. The export **refuses** a DB whose answers span more than one `config_hash` unless told otherwise, and it opens the DB read-only so it can run against a live server.

**Out of scope (later sessions):** events export + dwell-time derivation (S10 reads `events` directly); the divergence figure (S10); randomized arms (S11 — the `arm` column already exists and will round-trip, ROADMAP S11 regression); export from the web UI (an operator command is enough on a single laptop); scheduled/automatic export at shutdown (the S6 backup already snapshots the DB; CSV is a derived view); Excel `.xlsx` output; a "wide" one-row-per-pair pivot (one `pandas.pivot` away from this file — see §15 and the review report).

> **Spec history:** drafted 2026-09-16; routed through `/plan-eng-review` before implementation (see `## GSTACK REVIEW REPORT`).

---

## 1. Context

S9a fixed the `answers.value` string contract (categorical verbatim; multi-select as a JSON array in `questions.yaml` option order; likert / probability as `str(int)`; free-text stripped, ≤ 4000 chars) precisely so that *"the wide-pivot export does not have to guess"* (S9a §1, §6). S9b froze answers behind the frontier and added `progress.completed_at`, and filed two TODOs against this session: *export must know about `progress`* and *export inherits the `config_hash` drift check* (TODOS.md, S9a R23). Both are in scope here.

`plan.md:29` is the user's requirement: *"should easily export to csv file with columns: patient_id, clinician_name, timepoint, question_1, question_2 …"*. `CLAUDE.md` restates it as *"must export to CSV with one column per question"*. The ROADMAP's S9c block instead describes a per-pair pivot with `{question_id}_t{timepoint}` columns. The two shapes are not compatible; §5 picks the `plan.md` one and §15 records why. The ROADMAP's other four bullets (pipe-delimited multi-select, UTF-8 + header, injection guard with a `[REGRESSION]` test, pseudonymization keyfile) are kept verbatim.

Three failure modes drive the design:

1. **A spreadsheet executes a clinician's free text.** `=HYPERLINK(...)`, `-2+3`, `@SUM` typed into `free_notes` becomes a formula the moment the CSV is opened in Excel/LibreOffice. The guard (§7) is a pure function applied to every cell, including ids, and locked by a regression test.
2. **Names leave the laptop.** `clinicians.name_normalized` is the only identifying column in the DB. The export never reads it unless `--keyfile` is given, and then writes it to its own file, never into the answers CSV. This is the D9 pseudonym policy (TODOS.md) made concrete before the first pilot export exists.
3. **Two studies in one file.** A mid-pilot `questions.yaml` edit changes `config_hash`; answers recorded under the old hash have different columns, prompts or option sets. A silent mix is an analysis bug discovered six weeks later. The export refuses by default (§8.3).

Design principle carried from S9b: the **service layer owns every decision** (`export.py`); `cli.py` parses flags, prints one line and maps exceptions to exit codes; the DAOs gain pure read-all functions and nothing else.

---

## 2. Deliverables

| # | Path | Purpose |
|---|---|---|
| 1 | `pyproject.toml` | No new runtime deps (`csv` + `pandas` already present). No new dev deps. |
| 2 | `src/ehr_simulator/answer_codec.py` | NEW. Lifts `serialize_answer`, `deserialize_answer`, `AnswerValidationError`, `FREE_TEXT_MAX_CHARS`, `PROBABILITY_MIN/MAX` and the `_SERIALIZERS` table out of `web/answer_capture.py` **unchanged**, so a non-web consumer (this session's export, S10's divergence query) can decode `answers.value` without importing the web layer. `answer_capture.py` re-imports the names, so every S9a/S9b test and import path keeps working. Pure move — `git diff --color-moved` shows no edited lines. |
| 3 | `src/ehr_simulator/config/questions.py` | MODIFIED (one validator). Multi-select `options` must not contain `|` (the export delimiter) — `ValueError("multi-select options must not contain '|' (CSV export delimiter)")`. A validator only; the dumped model is unchanged, so **`config_hash` does not shift** and no `schema_version` bump. |
| 4 | `src/ehr_simulator/db/connection.py` | MODIFIED. `connect(db_path, *, apply_pragmas=True, access=AccessMode.READ_WRITE)`; `class AccessMode(StrEnum): READ_WRITE, READ_ONLY`. `READ_ONLY` opens `sqlite3.connect(f"file:{path}?mode=ro", uri=True)` and skips the `journal_mode` PRAGMA (a read-only connection cannot change it; the other two are harmless and still applied). A missing file under `READ_ONLY` raises `FileNotFoundError` instead of silently creating an empty DB. Default unchanged → every existing call site is untouched. |
| 5 | `src/ehr_simulator/db/answers.py` | MODIFIED. `fetch_all(conn) -> list[AnswerRow]`, `@dataclass(frozen=True) AnswerRow(clinician_id, patient_id, timepoint: float, question_id, value, arm, config_hash, ts_recorded)`. `ORDER BY patient_id, clinician_id, timepoint, question_id` so callers never depend on insertion order. |
| 6 | `src/ehr_simulator/db/progress.py` | MODIFIED. `fetch_all(conn) -> dict[tuple[str, str], Progress]` keyed by `(clinician_id, patient_id)`. |
| 7 | `src/ehr_simulator/db/arm_assignments.py` | MODIFIED. `fetch_all(conn) -> dict[tuple[str, str], str]` → `arm`. |
| 8 | `src/ehr_simulator/db/clinicians.py` | MODIFIED. `fetch_all(conn) -> list[tuple[str, str]]` → `(clinician_id, name_normalized)` ordered by `clinician_id`. **Only** the keyfile path calls it. |
| 9 | `src/ehr_simulator/export.py` | NEW. The service: `build_export(...) -> ExportFrame`, `write_csv(frame, out)`, `write_keyfile(rows, path)`, `guard_cell(value) -> str`, `encode_multi_select(value) -> str`, `ExportError`, `ExportReport`. §8. |
| 10 | `src/ehr_simulator/cli_support.py` | MODIFIED. `ResetError` generalized: `class OperatorError(ValueError)` with `ResetError = OperatorError` kept as an alias so `assert_schema_current` can be shared by `reset-progress` and `export-answers` (it currently raises `ResetError`, which reads wrong from the export). No behavior change. |
| 11 | `src/ehr_simulator/cli.py` | MODIFIED. NEW command `export-answers`. §5. Module docstring: "Nine commands after S9c". |
| 12 | `.gitignore` | MODIFIED. `exports/` and `*.keyfile.csv` — belt-and-braces; the defaults already sit under the ignored `data/`, but an operator who passes `--out ./answers.csv` should still not be able to commit a keyfile by accident. |
| 13 | `.github/workflows/ci.yml` | MODIFIED. `DB smoke` gains an `export-answers` run against the freshly migrated (empty) DB and asserts the header line byte-for-byte; a second run with `--keyfile` asserts the keyfile's mode is `600`. §10. |
| 14 | `tests/test_export.py` | NEW (~24 functions). §9. |
| 15 | `tests/test_cli.py` | EXTENDED (+6). §9. |
| 16 | `tests/test_config.py` | EXTENDED (+1 param case: `|` in a multi-select option is rejected; `|` in a *categorical* option is still accepted). |
| 17 | `tests/test_db.py` | EXTENDED (+4: the four `fetch_all`s + read-only `connect`). |
| 18 | `tests/test_answer_capture.py` | 0 changed lines — the import-path compatibility of deliverable #2 is what this asserts. |
| 19 | `tests/fixtures/study/questions_broken_pipe_option.yaml` | NEW (one multi-select option containing `\|`). |
| 20 | `TODOS.md` | MODIFIED. Strike the two S9c TODOs (closed). Add §14 items. |
| 21 | `specs/ROADMAP.md` | MODIFIED (S9c block). Shape pointer to §5 / §15, one line. S11's "`arm` round-trips through CSV" bullet gets the column name. |

`README.md` and `CLAUDE.md` "Current state" are owned by `/document-release` post-ship (README's *"CSV export … not in this build yet"* and the *"they live in SQLite only for now"* bullet become false).

---

## 3. Repo layout after Session 9c (diff vs end-of-S9b)

```
src/ehr_simulator/
├── answer_codec.py            NEW   value-string contract (moved out of web/)
├── export.py                  NEW   build_export / write_csv / write_keyfile / guard_cell
├── cli.py                     MOD   export-answers
├── cli_support.py             MOD   OperatorError (ResetError alias kept)
├── config/questions.py        MOD   multi-select options: no '|'
├── db/
│   ├── connection.py          MOD   AccessMode, read-only open
│   ├── answers.py             MOD   fetch_all + AnswerRow
│   ├── progress.py            MOD   fetch_all
│   ├── arm_assignments.py     MOD   fetch_all
│   └── clinicians.py          MOD   fetch_all (keyfile only)
└── web/answer_capture.py      MOD   re-imports from answer_codec (no logic change)

tests/
├── test_export.py             NEW
├── test_cli.py                MOD
├── test_config.py             MOD
├── test_db.py                 MOD
└── fixtures/study/questions_broken_pipe_option.yaml   NEW
```

Layering (new code only):

```
 cli.py ──▶ export.py ──▶ db/answers, db/progress, db/arm_assignments, db/clinicians ──▶ sqlite3
                │
                └─▶ answer_codec.deserialize_answer   (same layer as config/; no web import)
```

`export.py` never imports `ehr_simulator.web`. That is the reason for deliverable #2.

---

## 4. Data flow

```
 study.yaml + questions.yaml ──▶ load + compute_config_hash_from_models ──▶ live_hash, timepoints_minutes, question order
                                                                                   │
 DB (read-only) ──▶ answers.fetch_all ──┐                                          ▼
                    progress.fetch_all ─┤──▶ build_export(...) ──▶ ExportFrame(header, rows, report)
                    arm_assignments.fetch_all ┘         │                  │
                                                        │                  ├──▶ write_csv(out)      guard_cell on EVERY cell
                                                        │                  └──▶ ExportReport → one stdout line
                                                        └── refuses: mixed config_hash (unless --allow-mixed-config)
 clinicians.fetch_all ──▶ write_keyfile(K)   only when --keyfile given; mode 0600
```

Row universe (§6): for every `(clinician_id, patient_id)` pair that appears in `answers` **or** `progress`, one row per study `t_index` from `0` to the pair's frontier (`progress.unlocked_t_index`, or the highest answered `t_index` when no progress row exists). Cells with no answer are empty strings, never `NaN`/`None` text — an unanswered optional question and a not-yet-reached timepoint look identical in the file, and both are distinguishable from the walk state via `completed_at` + `t_index`.

---

## 5. CLI contract

### 5.1 `ehr-simulator export-answers STUDY_CONFIG QUESTIONS [options]`

| Flag | Default | Meaning |
|---|---|---|
| `STUDY_CONFIG` (arg) | required | Same YAML `serve --config` takes. Needed for `patient_ids` order, `timepoints_minutes` (→ `t_index`), `db_path` and the config hash. |
| `QUESTIONS` (arg) | required | Same YAML `serve --questions` takes. Defines the question columns and their order; needed for the hash and for multi-select decoding. |
| `--db-path P` | `resolve_db_path(study)` | Same precedence chain as every other command. Must exist (exit 1 otherwise — the export never creates a DB). |
| `--out F` | `<db parent>/exports/answers_<UTC YYYYmmddTHHMMSSZ>.csv` | Parent created if missing. An existing `F` is refused (exit 1) unless `--force`. |
| `--keyfile K` | *unset* | When given, write `clinician_id,name_normalized` to `K` with mode `0600`. Existing `K` refused unless `--force`. Without the flag, `clinicians` is never read. |
| `--only-complete` | off | Drop every pair whose `progress.completed_at IS NULL`. |
| `--allow-mixed-config` | off | Export even when rows carry a `config_hash` other than the live one (§8.3). |
| `--force` | off | Overwrite `--out` / `--keyfile`. |

Exit codes: **0** written (also for a header-only file when the DB has no answers — a valid, empty dataset; stdout says `0 rows`); **1** for every refusal, printed to stderr as `Error: <reason>` **before any file is created**. Refusals: config error; DB missing; schema behind (`assert_schema_current`, same message as `reset-progress`); mixed config without the flag; `--out`/`--keyfile` exists without `--force`; `--keyfile` on a filesystem that cannot honour `chmod 600` (Windows) → exit 1 with an explicit message rather than a world-readable keyfile.

stdout on success, one line:

```
Wrote 42 rows × 13 columns for 3 patients, 2 clinicians (2 complete walks, 1 in progress) to data/exports/answers_20260916T101500Z.csv
```

plus `Wrote keyfile (2 clinicians, mode 600) to data/clinician.keyfile.csv` when asked. Logging: `setup_logging(Path("logs"))` like every operator command; one `export.written` INFO event with the counts and `export.config_hash.drift` WARNING when `--allow-mixed-config` let a mix through.

### 5.2 Live server

The DB is opened with `AccessMode.READ_ONLY`. Under WAL a reader never blocks the server's writer and sees a consistent snapshot for the duration of its transaction. `build_export` performs its three `fetch_all`s inside one `BEGIN … COMMIT` so `answers` and `progress` come from the same snapshot (a frontier that moved between the two reads would otherwise produce a row with an answer past its own frontier). Documented in the CLI help: *safe to run while `serve` is up*.

---

## 6. Export shape

### 6.1 Columns, in order

```
patient_id, clinician_id, t_index, timepoint_minutes, arm, completed_at, config_hash, <q1>, <q2>, …, <qN>
```

- `<qi>` = `question_id`, verbatim, in `questions.yaml` order. `question_id` matches `^[a-z0-9_]+$`, so no header cell can ever collide with the seven fixed names — **as long as no question is called `patient_id`, `clinician_id`, `t_index`, `timepoint_minutes`, `arm`, `completed_at` or `config_hash`**. `build_export` raises `ExportError` on such a collision (test #7); adding a `questions.py` validator instead would shift `config_hash` for nothing.
- `patient_id`, `clinician_id` — as stored. `clinician_id` is the 16-hex pseudonym; there is no name column.
- `t_index` — integer position of `timepoint_minutes` in `study.timepoints_minutes`. Empty when the row's timepoint is not in the live study (only reachable under `--allow-mixed-config`).
- `timepoint_minutes` — `answers.timepoint`, formatted with `repr(float)` → `60.0`, never `60` or `6e1` (test #12 asserts byte equality).
- `arm` — from `arm_assignments` for the pair. If any `answers.arm` for the pair disagrees, WARNING `export.arm.mismatch` and the `arm_assignments` value wins (it is the locked assignment; `answers.arm` is a denormalized copy).
- `completed_at` — `progress.completed_at` as stored (`YYYY-MM-DD HH:MM:SS`), repeated on every row of the pair; empty while the walk is in progress. This is the "mark incomplete walks" TODO; `--only-complete` is the "exclude" half.
- `config_hash` — per row, from `answers` (the hash the answer was recorded under). Rows synthesised for un-answered timepoints carry the pair's `progress.config_hash`.

### 6.2 Rows

Order: `study.patient_ids` order, then `clinician_id` ascending, then `t_index` ascending. Pairs whose `patient_id` is not in the live study (only under `--allow-mixed-config`) sort after the study's patients, by `patient_id`. Deterministic: two exports of the same DB are byte-identical (test #13).

Per response type, the cell is `deserialize_answer(question, value)` rendered as:

| response_type | stored `value` | CSV cell |
|---|---|---|
| categorical | `Yes` | `Yes` |
| multi-select | `["Imaging","Labs"]` | `Imaging\|Labs` (option order preserved; empty list → empty cell — unreachable in practice, S9a deletes the row) |
| likert | `3` | `3` |
| probability-0-100 | `75` | `75` |
| free-text | text | text, verbatim (newlines and commas quoted by `csv`) |

Then `guard_cell` (§7). A `question_id` present in `answers` but absent from the live `questions.yaml` is part of the mixed-config case: refused by default; under `--allow-mixed-config` such ids become extra columns appended **after** the known questions, sorted, decoded as free-text (no question to decode against) — never silently dropped (test #17).

### 6.3 Encoding + dialect

`open(out, "w", encoding="utf-8", newline="")` + `csv.writer(f, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)`. No BOM: `pandas.read_csv` and R's `readr` handle bare UTF-8; a BOM would make the first header cell `﻿patient_id` in some readers. Header row first. Test #14 writes `é`, `–`, a newline, a comma and a `"` through a free-text answer and reads them back.

---

## 7. CSV-injection guard

```python
_FORMULA_TRIGGERS = frozenset("=+-@\t\r")
_GUARD_PREFIX = "'"

def guard_cell(value: str) -> str:
    """Neutralise spreadsheet formula injection.

    A cell whose first character is one of ``= + - @ TAB CR`` is prefixed
    with a single quote: Excel / LibreOffice / Sheets then render it as
    text.  Applied to EVERY cell, ids and header included — a question_id
    cannot start with these (regex), a patient_id or free-text can.
    """
    if value and value[0] in _FORMULA_TRIGGERS:
        return _GUARD_PREFIX + value
    return value
```

Consequences, stated so the analyst is not surprised: a free-text answer `-2 points` exports as `'-2 points`; a Geneva `patient_id` never starts with a trigger (digits + `_`) so ids are unaffected on both real datasets; likert/probability cells are non-negative ints and unaffected; multi-select cells start with an option's first character — an option starting with `-` or `+` (e.g. `+ve troponin`) **would** be guarded, so the export applies the guard to the joined cell, once. The guard is one-way; `read_back(path)` (test helper, not shipped) strips one leading `'` from cells that start with `''`? — **no**: an original value that starts with `'` is *not* guarded (`'` is not a trigger) so there is no ambiguity to resolve; the reverse map is "strip one leading `'` iff the next char is a trigger". Documented in the module docstring; test #4 (**[REGRESSION]**, ROADMAP) is parametrised over all six triggers, the empty string, a value that begins with `'`, and a value with a trigger in position 2.

---

## 8. Service layer (`export.py`)

### 8.1 API

```python
class ExportError(ValueError): ...            # refusal; nothing written

@dataclass(frozen=True)
class ExportOptions:
    only_complete: bool = False               # (enum-less: read straight from Typer flags)
    allow_mixed_config: bool = False

@dataclass(frozen=True)
class ExportReport:
    rows: int
    columns: int
    patients: int
    clinicians: int
    complete_walks: int
    in_progress_walks: int
    config_hashes: tuple[str, ...]           # distinct, sorted; len > 1 only under --allow-mixed-config

@dataclass(frozen=True)
class ExportFrame:
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]         # already decoded, NOT yet guarded
    report: ExportReport

def build_export(conn, *, study: StudyConfig, questions: Questions, live_hash: str,
                 options: ExportOptions) -> ExportFrame
def write_csv(frame: ExportFrame, out: Path) -> None          # guard_cell applied here, header included
def write_keyfile(conn, path: Path) -> int                    # returns row count; chmod 0600 before writing rows
def encode_multi_select(options: list[str]) -> str            # "|".join
def guard_cell(value: str) -> str
```

`ExportOptions` uses two booleans on a **dataclass**, not positional function parameters — the CLAUDE.md rule targets call-site readability (`f(conn, True, False)`); a keyword-constructed frozen options object is the enum-free way to get the same readability without four enum types for four flags. If the review prefers enums, `WalkFilter.{ALL, COMPLETE_ONLY}` and `ConfigMix.{REFUSE, ALLOW}` are the names.

### 8.2 `build_export` algorithm

```
1. header  = FIXED_COLUMNS + tuple(q.question_id for q in questions)        (collision check → ExportError)
2. with conn: (one snapshot)
       answers  = answers.fetch_all(conn)
       progress = progress.fetch_all(conn)
       arms     = arm_assignments.fetch_all(conn)
3. hashes = {a.config_hash for a in answers} | {p.config_hash for p in progress.values()}
   if hashes - {live_hash} and not allow_mixed_config → ExportError listing the foreign hashes + counts
4. pairs = keys(progress) ∪ {(a.clinician_id, a.patient_id) for a in answers}
   if only_complete: pairs = {p for p in pairs if progress[p].completed_at is not None}
5. for each pair (study order): frontier = progress.unlocked_t_index or max answered t_index
       for t_index in 0..frontier: emit row; fill cells from answers[(pair, timepoints[t_index])]
   answers whose timepoint ∉ timepoints_minutes (mixed only) → extra rows with t_index=""
6. report counts; return ExportFrame
```

Everything above is pure Python over lists; `pandas` is **not** used to build the frame (it would coerce `"3"` → `3`, `"60.0"` → `60`, and empty → `NaN` — exactly the surprises §6 rules out). `pandas.read_csv(..., dtype=str, keep_default_na=False)` is the sanctioned reader for the round-trip test.

### 8.3 Mixed config policy

Default **refuse**. Message names the live hash, each foreign hash and its row count, and the flag that overrides:

```
Error: answers span 2 config generations; live config is 3f9a…; found 17 rows under 8c21… .
       Re-export with --allow-mixed-config to include them (t_index / question columns may be empty for foreign rows).
```

Under the flag: rows kept, `config_hash` column tells them apart, WARNING event, and the stdout line ends with `(2 config generations — see config_hash column)`. This is the S9a R23 TODO closed with "refuse **or** column": both, default refuse.

---

## 9. Test inventory (ROADMAP bar ≥6; **35 new functions**)

Baseline 393 collected → ~428. Counted by function, e2e unaffected (no browser surface).

### `tests/test_export.py` (24)

Fixtures: `study_synthetic.yaml` + `questions.yaml` from `tests/fixtures/study/`; a `walked_db(tmp_path, *, pairs=…)` builder reusing `test_cli.py::_walked_db`'s pattern (lift it into `conftest.py` as `seed_walked_db`).

1. `test_header_order_fixed_then_questions_in_yaml_order`
2. `test_one_row_per_pair_per_timepoint_up_to_frontier` — pair unlocked to t=1 → rows for t_index 0 and 1 only.
3. `test_pair_with_answers_but_no_progress_row_uses_max_answered_t_index`
4. `test_guard_cell_regression` **[REGRESSION]** — parametrised over `= + - @ \t \r`, `""`, `"'x"`, `"a=b"`.
5. `test_guard_applied_to_every_cell_including_ids_and_header` — a `patient_id` starting with `=` in a synthetic-shaped DB is guarded; header untouched because no fixed name or `question_id` can start with a trigger (asserted literally).
6. `test_multi_select_pipe_encoding_preserves_option_order`
7. `test_question_id_colliding_with_fixed_column_raises` — a `Questions` model built in-test with `question_id: arm`.
8. `test_empty_cells_for_unanswered_optional_question` — `free_notes` blank → `""`, not `None`.
9. `test_completed_at_repeated_on_every_row_and_empty_in_progress`
10. `test_only_complete_drops_in_progress_pairs`
11. `test_arm_from_arm_assignments_wins_and_mismatch_warns` — `answers.arm="ai"` vs assignment `no_ai` → WARNING captured, cell `no_ai`.
12. `test_timepoint_minutes_formatted_as_repr_float` — `60.0`, `180.0`.
13. `test_export_is_deterministic_byte_identical_twice`
14. `test_utf8_round_trip_free_text_with_newline_comma_quote_and_accents`
15. `test_round_trip_pandas_read_csv_dtype_str_matches_frame` — read with `dtype=str, keep_default_na=False`; after stripping guards, equals `frame.rows`.
16. `test_mixed_config_refused_by_default_and_message_names_hashes`
17. `test_mixed_config_allowed_appends_unknown_question_columns_sorted_after_known`
18. `test_mixed_config_allowed_foreign_timepoint_gets_empty_t_index`
19. `test_write_csv_creates_parent_and_no_bom`
20. `test_write_keyfile_mode_0600_and_columns` — `stat(path).st_mode & 0o777 == 0o600`; skipped on Windows (`sys.platform == "win32"`).
21. `test_keyfile_not_written_when_not_requested_and_clinicians_never_read` — monkeypatch `clinicians.fetch_all` to raise.
22. `test_empty_db_writes_header_only_and_reports_zero_rows`
23. `test_build_export_reads_within_one_transaction` — assert `conn.in_transaction` is `True` inside a monkeypatched `progress.fetch_all` and `False` after.
24. `test_no_web_import` — `import ehr_simulator.export` in a fresh subprocess; `"ehr_simulator.web" not in sys.modules`. Cheap architectural lock for §3.

### `tests/test_cli.py` (+6)

25. `test_cli_export_answers_happy_path_writes_file_and_prints_counts`
26. `test_cli_export_answers_default_out_path_under_db_parent_exports` — regex on the timestamped name.
27. `test_cli_export_answers_refuses_existing_out_without_force_then_overwrites_with_force`
28. `test_cli_export_answers_errors` — parametrised: missing DB; stale schema (`pending migrations`); mixed config; `--keyfile` exists. Each: exit 1, `Error:` on stderr, **no file created**.
29. `test_cli_export_answers_keyfile_flag_writes_keyfile`
30. `test_cli_export_answers_runs_against_open_write_connection` — hold a `connect()` (WAL) with an uncommitted `answers.upsert` in the test process, run the export, assert it succeeds and does not see the uncommitted row.

### `tests/test_config.py` (+1 case)

31. `|` in a multi-select option → `ConfigError` naming the option; `|` in a categorical option → accepted (fixture #19 + an in-test model).

### `tests/test_db.py` (+4)

32. `test_answers_fetch_all_ordered`
33. `test_progress_and_arm_fetch_all_keyed_by_pair`
34. `test_clinicians_fetch_all_ordered_by_id`
35. `test_connect_read_only_refuses_write_and_missing_file` — `sqlite3.OperationalError` on `INSERT`; `FileNotFoundError` on a missing path (the default mode would have created it).

### `tests/test_answer_capture.py` (0 changed)

The unchanged file passing is the assertion that deliverable #2 was a pure move.

---

## 10. CI changes (`.github/workflows/ci.yml`)

`DB smoke` step, after the existing `backup` lines:

```yaml
uv run ehr-simulator export-answers \
  configs/example_config.yaml configs/example_questions.yaml \
  --db-path /tmp/ci_db_smoke/test.db --out /tmp/ci_db_smoke/answers.csv \
  --keyfile /tmp/ci_db_smoke/clinicians.keyfile.csv
test "$(head -1 /tmp/ci_db_smoke/answers.csv)" = \
  "patient_id,clinician_id,t_index,timepoint_minutes,arm,completed_at,config_hash,deterioration_6h,survives_hospital,good_outcome_3mo,dead_6mo,confidence,contributing_factors,free_notes"
test "$(stat -c %a /tmp/ci_db_smoke/clinicians.keyfile.csv)" = "600"
```

The header literal is intentional: it fails the day someone reorders `configs/example_questions.yaml` or the fixed columns without touching this spec.

---

## 11. Commit discipline (target 4 commits, ~1 day)

1. **`Lift answer codec, add read-only connect and fetch_all DAOs`** — deliverables #2, #3, #4–#8, #10, #16, #17, #18. Pure moves + additive reads. Suite green at 393 + 5.
2. **`Add export service with injection guard`** — #9, #14, #19. `export.py` + `test_export.py`.
3. **`Add export-answers CLI and CI smoke`** — #11, #12, #13, #15.
4. **`Close S9c TODOs and roadmap pointers`** — #20, #21.

Each commit: `uv run ruff check . && uv run ruff format --check . && uv run pytest` green before landing. Commit messages follow the seven rules; no attribution trailer.

---

## 12. Acceptance criteria

- `uv run ehr-simulator export-answers configs/example_config.yaml configs/example_questions.yaml --db-path data/ehr_simulator.db` on a DB produced by the S9b e2e walk writes a file whose header is the §10 literal and whose rows open in LibreOffice with **no** cell rendered as a formula after a `free_notes` answer of `=1+1` and `-3` (§17 step 5).
- The answers CSV contains no `name_normalized` value anywhere (`grep -c` of every clinician name = 0), with and without `--keyfile`.
- `--keyfile` produces a `600` file with exactly the clinicians in the DB.
- A DB with answers under two hashes exits 1 by default with the §8.3 message and exports with the flag, `config_hash` column distinguishing the rows.
- Export runs while `serve` is up (§17 step 6) and the server keeps accepting answers during it.
- `uv run pytest` ≥ 428 collected, all default-suite green on 3.11 and 3.12; `ruff check`/`format --check` clean; CI `DB smoke` header + mode assertions pass.
- `git diff --color-moved` on commit 1 shows `answer_capture.py` → `answer_codec.py` as pure moves.

---

## 13. Conventions

- **Service decides, CLI reports.** `cli.py` contains no branch on export content; every refusal is an `ExportError` / `OperatorError` raised by the service before any file is opened for writing. Order in `write_csv`: validate → build frame → **then** `open(out, "x")` (or `"w"` under `--force`) — `"x"` makes the no-overwrite rule an OS guarantee, not a `Path.exists()` race.
- **Guard at the boundary.** `guard_cell` is applied in `write_csv`, once, to every cell. `ExportFrame.rows` are un-guarded so tests compare against stored values, and so S10 can consume `build_export` in-process without CSV artefacts.
- **No pandas in the write path** (§8.2). Reading back with `dtype=str, keep_default_na=False` is the only sanctioned pandas use, in tests.
- **Read-only means read-only.** `AccessMode.READ_ONLY` is passed from `cli.py`; test #35 locks that an `INSERT` fails on such a connection, and test #21 locks that `clinicians` is not even queried without `--keyfile`.
- **No new access-modifier changes.** Everything lifted in #2 keeps its name and its leading-underscore status; `answer_capture.py` re-exports what it already exported.
- **Timestamps as stored.** `completed_at` and (in the keyfile) nothing else; the export does not reformat SQLite's `CURRENT_TIMESTAMP` strings. `ts_recorded` is fetched (it is in `AnswerRow` for S10) but **not** exported — one column per question is the contract; per-question timestamps would double the width. §15.

---

## 14. Open decisions deferred to later sessions / TODOs to file

- **S10: events export.** Dwell time, `advance.blocked` counts and `answer.upsert` edit counts live in `events`; S10 reads the DB directly or adds `export-events`. `AnswerRow.ts_recorded` is already fetched for it.
- **S10/S12: wide per-pair pivot.** `pd.read_csv(...).pivot(index=["patient_id","clinician_id"], columns="t_index", values=[...])` reproduces the ROADMAP's original S9c shape from this file; ship it as a helper in the S12 analysis notebook, not as a second export format.
- **S11: `arm` column round-trip regression** — test #11 already asserts the column; S11's spec adds the `ai` value case and the `arm_source` question (export it? today: no).
- **Per-question `ts_recorded` columns** (`<q>_ts`) — only if an analysis needs within-timepoint ordering that `events` cannot give.
- **`--patients P1,P2` / `--clinician NAME` filters** — trivial to add to `ExportOptions`; wait for a request.
- **BOM opt-in (`--excel`)** for double-click-open on Windows — §6.3 chose bare UTF-8; revive if a collaborator hits mojibake.
- **Windows keyfile permissions** — §5.1 refuses; an ACL-based equivalent is out of scope for a Linux/macOS pilot.

---

## 15. What Session 9c does NOT lock

- **The export shape vs the ROADMAP's pivot.** `plan.md:29` (and CLAUDE.md) specify one row per `(patient_id, clinician_name, timepoint)` with one column per question; the ROADMAP's S9c block (written before S9a fixed the value contract) specifies one row per pair with `{question_id}_t{timepoint}` columns. S9c ships the `plan.md` shape because: (a) it is the owner's stated requirement and the one CLAUDE.md repeats; (b) it is tidy — a study with 3 timepoints × 7 questions is 14 columns, not 28, and a Geneva study with 24 timepoints is 31 columns, not 175; (c) S10's divergence view groups by `(patient, t_index, arm)`, which is a `groupby` on this shape and a `melt` on the other; (d) the pivot is one pandas call away (§14). **This is a premise-level decision for the owner** — see the review report.
- **`clinician_id` as the pseudonym.** It is `sha256(name_normalized)[:16]`, deterministic and *not* salted — anyone with a name list can re-identify by hashing (S6 design). The keyfile is therefore a convenience, not the only reverse path. A salted or random pseudonym is a D9/IRB policy question (Phase-2 gate), not an export-format one; the export column will not change shape if the id scheme does.
- **Header-only export on an empty DB is exit 0.** A refusal would make the CI smoke need seeded data; an empty dataset is a legitimate answer to "what has been recorded". Reversible in one `if`.
- **`'` as the guard prefix.** OWASP's recommendation; the alternative (a leading space) is invisible in a spreadsheet and gets trimmed by many readers.
- **No `clinician_name` column, ever, in the answers file — not even behind a flag.** The two-file design is the whole point; a flag would be the first thing an analyst in a hurry reaches for.

---

## 16. What already exists (carried into S9c)

- `answers.value` string contract (S9a §6) and `deserialize_answer` — moved, not rewritten.
- `progress.completed_at` + `unlocked_t_index` (S9b) — the row universe and the `completed_at` column.
- `arm_assignments` (S6, `phase1_stub` → `no_ai`) — the `arm` column; S11 fills in `ai`.
- `compute_config_hash_from_models` (S5) — the live hash the refusal compares against.
- `resolve_db_path` (S6), `assert_schema_current` (S9b), `setup_logging` — reused verbatim by the new command.
- `test_cli.py::_walked_db` — lifted to `conftest.py::seed_walked_db` and reused by `test_export.py`.

---

## 17. Verification (end-to-end)

1. `uv sync && uv run pytest` — ≥ 428 collected, default suite green.
2. `uv run ehr-simulator migrate --db-path /tmp/s9c/test.db` then `uv run ehr-simulator export-answers configs/example_config.yaml configs/example_questions.yaml --db-path /tmp/s9c/test.db --out /tmp/s9c/empty.csv` → exit 0, `0 rows`, header equals §10 literal.
3. `uv run ehr-simulator serve --config configs/example_config.yaml --questions configs/example_questions.yaml --db-path /tmp/s9c/test.db`; log in as `Dr. Export`; walk `synth_001` to completion answering `free_notes` with `=1+1` at t=0 and `-3 points` at t=1; walk `synth_002` to t=1 only.
4. With the server **still running**: `export-answers … --db-path /tmp/s9c/test.db --out /tmp/s9c/walk.csv --keyfile /tmp/s9c/k.csv` → exit 0; stdout reports 5 rows (3 + 2), 1 complete walk, 1 in progress; `stat -c %a /tmp/s9c/k.csv` = `600`; `grep -c "dr. export" /tmp/s9c/walk.csv` = 0; `grep -c "dr. export" /tmp/s9c/k.csv` = 1.
5. Open `walk.csv` in LibreOffice Calc: the two `free_notes` cells display `'=1+1` and `'-3 points` as text; no cell evaluates.
6. Back in the browser, answer a question at `synth_002` t=1 during step 4's run (or immediately after) → 200; the server never logged `database is locked`.
7. Edit `configs/example_questions.yaml` (change a prompt), restart the server, answer one more question; re-run export → exit 1 with the §8.3 message; add `--allow-mixed-config` → exit 0, `config_hash` column shows two values.
8. `uv run pytest -m e2e` — 8 Playwright tests still green (no UI change, sanity).

---

## 18. Review history

Filled by `/plan-eng-review`. Each accepted fix is annotated `[review-fix R<N>]` inline in the spec body; this table cross-references them.

| Review-fix | Original design | Resolved design |
|---|---|---|
| — | — | — |

---

## Spec destination

`specs/session-09c-csv-export.md` (matches the `session-NN-name.md` convention). The pre-review draft is overwritten in place by the review pass.
