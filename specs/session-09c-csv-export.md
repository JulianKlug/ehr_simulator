# Session 09c — CSV export (`export-answers`)

**Goal:** a researcher can turn the pilot's SQLite file into one analysis-ready CSV with a single command, without exporting the clinician login-name mapping and without a spreadsheet executing anything a clinician typed.

S9c adds:

```text
ehr-simulator export-answers STUDY_CONFIG QUESTIONS
    [--db-path P]
    [--out F]
    [--keyfile K]
    [--only-complete]
    [--force]
```

The answers CSV has one row per `(patient_id, clinician_id, timepoint)`, one column per `question_id` in `questions.yaml` order, plus `arm`, `t_index`, `config_hash`, and the walk's `completed_at`.

Multi-select cells are pipe-delimited. Every CSV cell passes a formula-injection guard at the file boundary.

The answers file carries only the 16-hex `clinician_id`; `clinicians.name_normalized` is never placed in that file. An optional `--keyfile` writes the mapping for clinicians actually present in the export to a separate mode-0600 file.

The export is an **interpreted export of the live study configuration**. It therefore refuses any relevant database record belonging to another `config_hash`; there is no mixed-config override. It also strictly validates persisted answers and arm provenance before writing anything.

The database is opened read-only, and all research-state reads occur inside one explicit SQLite read transaction so the command can run against a live WAL-mode server while seeing one consistent snapshot.

**Out of scope (later sessions):**

* events export + dwell-time derivation — S10 reads `events` directly;
* the divergence figure — S10;
* randomized arms — S11; the `arm` column already round-trips;
* `arm_source` in the CSV — revisit with S11 if analysis needs assignment provenance explicitly;
* export from the web UI — an operator command is enough on a single laptop;
* scheduled/automatic export at shutdown — the S6 backup already snapshots the DB; CSV is a derived view;
* Excel `.xlsx` output;
* raw recovery export for mixed config generations;
* a one-row-per-pair wide pivot — one `pandas.pivot` away from this file.

> **Spec history:** drafted 2026-09-16; revised 2026-09-18 after engineering review. The revised version removes mixed-config interpretation, adds strict persisted-answer validation, fixes the SQLite snapshot contract, promotes arm mismatches to integrity errors, and makes output installation staged.

---

## 1. Context

S9a fixed the `answers.value` storage contract:

* categorical — option verbatim;
* multi-select — JSON array in `questions.yaml` option order;
* likert / probability — canonical `str(int)`;
* free-text — stripped text, ≤ 4000 chars.

S9b froze answers behind the frontier and added `progress.completed_at`. It also left two S9c requirements:

1. export must know about `progress`;
2. export must detect `config_hash` drift.

`plan.md` requires CSV export with one row per patient / clinician / timepoint and one column per question. `CLAUDE.md` repeats the one-column-per-question requirement.

The older ROADMAP S9c description instead proposed one row per clinician/patient pair with `{question_id}_t{timepoint}` columns. The two shapes are incompatible. §15 keeps the tidy row-per-timepoint shape and updates the ROADMAP pointer.

Four failure modes drive the revised design:

1. **A spreadsheet executes clinician-entered text.**
   A free-text answer beginning with `=`, `+`, `-`, `@`, or another spreadsheet formula trigger can be interpreted as a formula when the CSV is opened.

2. **The login-name mapping leaves the laptop unintentionally.**
   `clinicians.name_normalized` is identifying metadata. The answers CSV never reads or exports it. It is queried only when `--keyfile` is explicitly supplied.

3. **An interpreted CSV silently combines incompatible study generations.**
   `answers.config_hash` belongs to each individual answer row, and an answer can be independently upserted. A single `(clinician, patient, timepoint)` export row can therefore contain answers written under different generations. One row-level `config_hash` cannot faithfully represent such a row.

   **[review-fix R1]** S9c therefore refuses any non-live `config_hash`. A future recovery/raw-export command may export the underlying long-form rows without interpreting them.

4. **Malformed persisted data is converted into plausible analysis data.**
   The existing UI deserializer is intentionally forgiving for browser pre-fill. That is inappropriate for a research export.

   **[review-fix R2]** Export uses a strict persisted-value decoder and fails loudly if the DB violates the S9a storage contract.

Design principle carried from S9b:

> **The service layer owns every research-data decision.**

`export.py` validates and builds the export. `cli.py` parses flags, prints results and maps operator errors to exit codes. DAO additions remain read-only.

---

## 2. Deliverables

| #  | Path                                                     | Purpose                                                                                                                                                                                                    |                                                                                                     |
| -- | -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| 1  | `pyproject.toml`                                         | No new runtime or dev dependencies. `csv` is stdlib; pandas remains test/analysis-only.                                                                                                                    |                                                                                                     |
| 2  | `src/ehr_simulator/answer_codec.py`                      | NEW. Move the answer serialization contract out of `web/answer_capture.py`; retain the existing lenient `deserialize_answer` for UI pre-fill and add strict `decode_stored_answer` for research consumers. |                                                                                                     |
| 3  | `src/ehr_simulator/config/questions.py`                  | MODIFIED. Multi-select options may not contain `                                                                                                                                                           | `, the export delimiter. Accepted models are unchanged, so existing valid configs hash identically. |
| 4  | `src/ehr_simulator/db/connection.py`                     | MODIFIED. Add `AccessMode.READ_WRITE` / `READ_ONLY`; read-only opens an existing DB via SQLite `mode=ro` and never applies write-affecting boot PRAGMAs.                                                   |                                                                                                     |
| 5  | `src/ehr_simulator/db/answers.py`                        | MODIFIED. Add deterministic `fetch_all()` returning typed `AnswerRow`s.                                                                                                                                    |                                                                                                     |
| 6  | `src/ehr_simulator/db/progress.py`                       | MODIFIED. Add deterministic `fetch_all()` keyed by `(clinician_id, patient_id)`.                                                                                                                           |                                                                                                     |
| 7  | `src/ehr_simulator/db/arm_assignments.py`                | MODIFIED. Add typed `fetch_all()` including `arm`, `arm_source`, and `config_hash`.                                                                                                                        |                                                                                                     |
| 8  | `src/ehr_simulator/db/clinicians.py`                     | MODIFIED. Add `fetch_by_ids()` for the optional keyfile; no name lookup occurs without `--keyfile`.                                                                                                        |                                                                                                     |
| 9  | `src/ehr_simulator/export.py`                            | NEW. Export service: snapshot read, integrity validation, frame construction, CSV guard, staged output installation.                                                                                       |                                                                                                     |
| 10 | `src/ehr_simulator/cli_support.py`                       | MODIFIED. Generalize operator-command errors so schema-current checks can be reused without export raising a reset-specific exception.                                                                     |                                                                                                     |
| 11 | `src/ehr_simulator/cli.py`                               | MODIFIED. Add `export-answers`; module docstring becomes nine commands after S9c.                                                                                                                          |                                                                                                     |
| 12 | `.gitignore`                                             | MODIFIED. Ignore `exports/` and `*.keyfile.csv`.                                                                                                                                                           |                                                                                                     |
| 13 | `.github/workflows/ci.yml`                               | MODIFIED. DB smoke gains header-only export + keyfile mode check.                                                                                                                                          |                                                                                                     |
| 14 | `tests/test_answer_codec.py`                             | NEW. Strict persisted-value decoder tests.                                                                                                                                                                 |                                                                                                     |
| 15 | `tests/test_export.py`                                   | NEW. Export service, integrity, snapshot, guard and output tests.                                                                                                                                          |                                                                                                     |
| 16 | `tests/test_cli.py`                                      | EXTENDED. CLI happy path and refusal behavior.                                                                                                                                                             |                                                                                                     |
| 17 | `tests/test_config.py`                                   | EXTENDED. Pipe in a multi-select option is rejected; categorical pipe remains valid.                                                                                                                       |                                                                                                     |
| 18 | `tests/test_db.py`                                       | EXTENDED. New DAO reads + read-only connection.                                                                                                                                                            |                                                                                                     |
| 19 | `tests/test_answer_capture.py`                           | Test logic unchanged; existing tests lock compatibility of the moved codec.                                                                                                                                |                                                                                                     |
| 20 | `tests/fixtures/study/questions_broken_pipe_option.yaml` | NEW. Multi-select option containing `                                                                                                                                                                      | `.                                                                                                  |
| 21 | `TODOS.md`                                               | MODIFIED. Close the two S9c TODOs; add deferred raw mixed-config export / `arm_source` decisions.                                                                                                          |                                                                                                     |
| 22 | `specs/ROADMAP.md`                                       | MODIFIED. Point S9c at the tidy shape and note that interpreted export refuses config drift.                                                                                                               |                                                                                                     |

`README.md` and `CLAUDE.md` current-state wording remains release-documentation work after implementation.

---

## 3. Repo layout after Session 9c

```text
src/ehr_simulator/
├── answer_codec.py            NEW   shared answer storage codec
├── export.py                  NEW   export preparation + integrity + file writing
├── cli.py                     MOD   export-answers
├── cli_support.py             MOD   OperatorError
├── config/
│   └── questions.py           MOD   multi-select options: no '|'
├── db/
│   ├── connection.py          MOD   AccessMode + read-only open
│   ├── answers.py             MOD   fetch_all + AnswerRow
│   ├── progress.py            MOD   fetch_all
│   ├── arm_assignments.py     MOD   fetch_all + typed row
│   └── clinicians.py          MOD   fetch_by_ids for keyfile
└── web/
    └── answer_capture.py      MOD   imports codec functions; web behavior unchanged

tests/
├── test_answer_codec.py       NEW
├── test_export.py             NEW
├── test_cli.py                MOD
├── test_config.py             MOD
├── test_db.py                 MOD
└── fixtures/study/
    └── questions_broken_pipe_option.yaml
```

Layering:

```text
cli.py ──▶ export.py ──▶ db/answers
                │       db/progress
                │       db/arm_assignments
                │       db/clinicians   [only with --keyfile]
                │
                └────▶ answer_codec.decode_stored_answer
```

`export.py` never imports `ehr_simulator.web`.

---

## 4. Data flow

```text
study.yaml + questions.yaml
        │
        ├── load + validate
        └── compute_config_hash_from_models
                     │
                     ▼
                  live_hash

DB opened READ_ONLY
        │
        ▼
     BEGIN                         explicit read transaction
        │
        ├── answers.fetch_all
        ├── progress.fetch_all
        ├── arm_assignments.fetch_all
        │
        ├── validate config hashes
        ├── validate pair / frontier / arm invariants
        ├── strict-decode every persisted answer
        ├── build ExportFrame
        │
        └── clinicians.fetch_by_ids       only when --keyfile requested
        │
     ROLLBACK                              closes read snapshot
        │
        ▼
fully validated ExportBundle
        │
        ├── stage answers CSV
        ├── stage mode-0600 keyfile       optional
        └── install final outputs
```

**[review-fix R3]** `with conn:` is not treated as the snapshot mechanism. `build_export` issues an explicit `BEGIN` before the first research-state `SELECT` and ends the read transaction in `finally`.

The snapshot therefore covers:

* answers;
* progress;
* arm assignments;
* optional clinician-name lookup.

### Row universe

For every `(clinician_id, patient_id)` pair appearing in `answers` or `progress`, emit one row for each live-study `t_index` from `0` through that pair's frontier.

Frontier:

* if a `progress` row exists: `progress.unlocked_t_index`;
* otherwise: the highest live `t_index` represented by that pair's answer rows.

A future, not-yet-unlocked timepoint has **no CSV row**.

An unlocked timepoint with an unanswered optional question has a CSV row with an empty question cell.

Those states are intentionally distinct.

---

## 5. CLI contract

### 5.1 `ehr-simulator export-answers STUDY_CONFIG QUESTIONS [options]`

| Argument / flag   | Default                                                  | Meaning                                                                                            |
| ----------------- | -------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `STUDY_CONFIG`    | required                                                 | Same study YAML used by `serve`; supplies patient order, timepoints, DB path and live config hash. |
| `QUESTIONS`       | required                                                 | Same questions YAML used by `serve`; defines question columns, response types and live hash.       |
| `--db-path P`     | `resolve_db_path(study)`                                 | Override DB path. DB must already exist.                                                           |
| `--out F`         | `<db parent>/exports/answers_<UTC YYYYmmddTHHMMSSZ>.csv` | Answers CSV. Parent created if needed. Existing path refused unless `--force`.                     |
| `--keyfile K`     | unset                                                    | Write `clinician_id,name_normalized` for clinicians present in this export. Must be mode 0600.     |
| `--only-complete` | off                                                      | Export only pairs with a progress row whose `completed_at IS NOT NULL`.                            |
| `--force`         | off                                                      | Replace existing requested output paths after all validation succeeds.                             |

There is deliberately **no `--allow-mixed-config` flag**.

An interpreted tidy export cannot faithfully combine answer cells whose question definitions or provenance may belong to different study generations.

### Exit codes

**0** — export written successfully, including a header-only answers CSV when there are no exportable pairs.

**1** — operator/config/data-integrity/filesystem refusal.

Examples:

* invalid study/questions config;
* DB missing;
* schema migrations pending;
* question id collides with an export metadata column;
* any relevant `answers.config_hash != live_hash`;
* any relevant `progress.config_hash != live_hash`;
* any relevant `arm_assignments.config_hash != live_hash`;
* answer references an unknown question;
* answer references a timepoint outside the live study;
* pair references a patient outside the live study;
* persisted answer violates the S9a storage contract;
* missing arm assignment for an exported pair;
* answer's denormalized `arm` disagrees with locked assignment;
* invalid progress frontier;
* answer exists beyond the progress frontier;
* `completed_at` exists before the final frontier;
* output and keyfile resolve to the same path;
* requested final path exists without `--force`;
* secure keyfile permissions cannot be provided;
* file staging / installation fails.

CLI refusal format:

```text
Error: <reason>
```

No normal validation or data-integrity refusal creates or replaces a final output path.

Unexpected OS-level failures during staging/install are reported as exit 1; temporary files are removed best-effort.

### stdout

Example:

```text
Wrote 42 rows × 13 columns for 3 patients, 2 clinicians (2 complete walks, 1 in progress) to data/exports/answers_20260918T081500Z.csv
```

With a keyfile:

```text
Wrote keyfile (2 clinicians, mode 600) to data/clinician.keyfile.csv
```

Logging:

* `setup_logging(Path("logs"))`;
* one `export.written` INFO event with counts;
* integrity refusals may log structured context but raw answer values and clinician names are not logged.

---

### 5.2 Live server

The export connection uses:

```python
connect(db_path, access=AccessMode.READ_ONLY)
```

Read-only connection behavior:

```python
class AccessMode(StrEnum):
    READ_WRITE = "read-write"
    READ_ONLY = "read-only"
```

`READ_ONLY`:

* requires an existing file;
* opens SQLite with URI `mode=ro`;
* sets the row factory;
* may enable `PRAGMA foreign_keys=ON`;
* does not attempt `PRAGMA journal_mode=WAL`;
* does not alter synchronous mode;
* may additionally set `PRAGMA query_only=ON` as defense in depth.

The application server remains the writer.

**[review-fix R3]** The exporter explicitly starts one read transaction:

```python
conn.execute("BEGIN")
try:
    ...
finally:
    conn.rollback()
```

The first SELECT establishes the read snapshot; subsequent DAO reads remain inside it.

The contract is therefore:

> safe to run while `serve` is up; export sees one committed snapshot and never observes uncommitted server writes.

The snapshot test deliberately commits a second connection between exporter fetches and proves the later exporter fetch still sees the original snapshot.

---

## 6. Export shape

### 6.1 Columns, in order

```text
patient_id,
clinician_id,
t_index,
timepoint_minutes,
arm,
completed_at,
config_hash,
<q1>,
<q2>,
…,
<qN>
```

`<qi>` is the `question_id` verbatim and follows `questions.yaml` order.

Fixed metadata columns:

```text
patient_id
clinician_id
t_index
timepoint_minutes
arm
completed_at
config_hash
```

If a question uses one of those ids, `build_export` refuses before DB output preparation.

This remains an export-layer validation rather than making the generic questions model aware of every downstream format.

`question_id` already matches `^[a-z0-9_]+$`.

### Metadata semantics

**`patient_id`**
Live-study patient id as stored.

**`clinician_id`**
16-hex pseudonym as stored. The answers file never contains `name_normalized`.

**`t_index`**
Integer index in `study.timepoints_minutes`.

There are no rows with an empty `t_index`: foreign/unknown timepoints are integrity errors, not partially interpreted rows.

**`timepoint_minutes`**
Formatted from the live study's float value using `repr(float(...))`, e.g.:

```text
60.0
180.0
```

**`arm`**
The locked `arm_assignments.arm`.

Before emitting a pair, every answer row for that pair must carry the same `answers.arm`.

A disagreement is an `ExportError`; the exporter does not choose a winner.

**[review-fix R4]**

**`completed_at`**
For a completed pair, serialize the `datetime` returned by SQLite back to the canonical SQLite second-resolution representation:

```text
YYYY-MM-DD HH:MM:SS
```

Repeat it on every row for that pair.

For an in-progress pair, use the empty string.

A pair with no progress row is necessarily considered incomplete.

**`config_hash`**
Always `live_hash`.

This is valid because S9c refuses the export if any relevant answer, progress or arm-assignment record has another hash.

The column remains useful provenance once CSVs are detached from the source DB.

---

### 6.2 Pair and row ordering

Order is deterministic:

1. `study.patient_ids` order;
2. `clinician_id` ascending;
3. `t_index` ascending.

A pair referencing a patient not in the live study is refused rather than sorted into a foreign-data tail.

Two exports from the same DB snapshot and same config are byte-identical except when the caller uses different output filenames; file contents are identical.

### `--only-complete`

Without the flag:

```python
pairs = progress_pairs | answer_pairs
```

With the flag:

```python
pairs = {
    pair
    for pair in pairs
    if (p := progress_rows.get(pair)) is not None
    and p.completed_at is not None
}
```

**[review-fix R5]** An answer-only pair therefore does not raise `KeyError`; it is simply excluded from `--only-complete`.

---

### 6.3 Strict answer decoding

The web UI retains:

```python
deserialize_answer(...)
```

for forgiving browser pre-fill.

Research export uses:

```python
decode_stored_answer(...)
```

which validates the persisted representation.

#### categorical

Stored:

```text
Yes
```

Requirements:

* exactly one configured option;
* value must appear in `question.options`.

CSV:

```text
Yes
```

#### multi-select

Stored:

```json
["Imaging","Labs"]
```

Requirements:

* syntactically valid JSON;
* top-level value is a list;
* every member is a string;
* every member is a configured option;
* no duplicates;
* order exactly matches canonical `questions.yaml` option order;
* persisted list is non-empty.

CSV:

```text
Imaging|Labs
```

A malformed JSON value does **not** become an empty CSV cell.

#### likert

Stored:

```text
3
```

Requirements:

* canonical integer string;
* integer is within configured `[scale_min, scale_max]`;
* non-canonical persisted forms such as `+3`, `03`, `3.0` are rejected.

CSV:

```text
3
```

#### probability-0-100

Stored:

```text
75
```

Requirements:

* canonical integer string;
* range 0–100.

CSV:

```text
75
```

#### free-text

Stored and exported verbatim after validating the storage contract:

* length ≤ `FREE_TEXT_MAX_CHARS`;
* value is already in the canonical stripped form produced by S9a.

Commas, quotes and embedded newlines are handled by `csv.writer`.

A strict-decoding failure is reported with identifiers, not the raw value:

```text
Error: invalid persisted answer for patient synth_001, clinician a1b2…, timepoint 60.0, question confidence: expected canonical integer 1..5
```

Raw free text is never echoed into the error or log.

---

### 6.4 Progress integrity

Before rows are emitted:

* `0 <= unlocked_t_index < len(study.timepoints_minutes)`;
* every answer timepoint maps exactly to one live timepoint;
* if progress exists, no answer may lie beyond `unlocked_t_index`;
* if `completed_at is not None`, the frontier must be the final study `t_index`.

Violations are refused rather than repaired.

For an answer-only pair with no progress row:

```text
frontier = max(t_index of its answers)
```

For a progress-only pair:

```text
frontier = progress.unlocked_t_index
```

The latter produces blank question cells for unlocked timepoints with no recorded answers.

---

### 6.5 Encoding and CSV dialect

```python
open(path, "w", encoding="utf-8", newline="")
csv.writer(
    f,
    lineterminator="\n",
    quoting=csv.QUOTE_MINIMAL,
)
```

No BOM.

Header row first.

A regression round-trip includes:

* `é`;
* en dash;
* comma;
* quote;
* embedded newline;
* leading apostrophe;
* every formula-trigger prefix.

The round-trip comparison uses the exact guarded CSV representation. It does **not** attempt to heuristically “undo” guard prefixes.

---

## 7. CSV / formula-injection guard

Formula guarding is an output-boundary concern.

```python
_FORMULA_TRIGGERS = frozenset(
    (
        "=",
        "+",
        "-",
        "@",
        "\t",
        "\r",
        "\n",
        "＝",
        "＋",
        "－",
        "＠",
    )
)
_GUARD_PREFIX = "'"


def guard_cell(value: str) -> str:
    if value and value[0] in _FORMULA_TRIGGERS:
        return _GUARD_PREFIX + value
    return value
```

**[review-fix R6]** LF and full-width variants are included in addition to the original six triggers.

`guard_cell` is applied exactly once to **every** cell passed to the CSV writer, including metadata and headers.

Consequences:

```text
=1+1       -> '=1+1
-3 points  -> '-3 points
@SUM(...)  -> '@SUM(...)
＝1+1      -> '＝1+1
```

An original value beginning with `'` is unchanged.

For example:

```text
'=1+1
```

remains:

```text
'=1+1
```

There is deliberately no generic “unguard” operation: an original apostrophe followed by a trigger is indistinguishable from an injected guard prefix without access to the original frame.

Tests therefore compare:

```python
parsed_csv_cell == guard_cell(original_frame_cell)
```

rather than stripping prefixes heuristically.

`csv.writer` owns delimiter / quote / newline escaping; handwritten CSV concatenation is forbidden.

Security statement:

> The export neutralizes recognized formula-leading characters when the generated CSV is initially opened in common spreadsheet software. No claim is made that this protection survives arbitrary spreadsheet editing, re-saving, locale conversion or import/export through another application.

---

## 8. Service layer (`export.py`)

### 8.1 API

```python
class ExportError(ValueError):
    """Research export cannot be produced faithfully."""


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


def build_export(
    conn,
    *,
    study: StudyConfig,
    questions: Questions,
    live_hash: str,
    options: ExportOptions,
    include_keyfile: bool = False,
) -> ExportBundle:
    ...


def write_export(
    bundle: ExportBundle,
    *,
    out: Path,
    keyfile: Path | None,
    force: bool,
) -> None:
    ...


def write_csv(frame: ExportFrame, path: Path) -> None:
    ...


def write_keyfile(rows: tuple[tuple[str, str], ...], path: Path) -> None:
    ...


def encode_multi_select(options: list[str]) -> str:
    return "|".join(options)


def guard_cell(value: str) -> str:
    ...
```

`ExportFrame` contains **un-guarded** semantic cell values.

Guarding occurs only in `write_csv`.

---

### 8.2 Read snapshot algorithm

```text
1. validate metadata/question-column collisions

2. assert connection is not already inside an application transaction

3. conn.execute("BEGIN")

4. try:
       answers = answers.fetch_all(conn)
       progress = progress.fetch_all(conn)
       assignments = arm_assignments.fetch_all(conn)

       validate config hashes
       validate referenced patients/questions/timepoints
       validate progress/frontier invariants
       validate arm assignment presence + consistency
       strict-decode all stored answers

       choose pairs
       apply --only-complete
       build rows in deterministic order
       build report

       if include_keyfile:
           exported_ids = clinician ids actually represented in frame
           keyfile_rows = clinicians.fetch_by_ids(conn, exported_ids)
           verify every exported clinician id resolved exactly once
       else:
           keyfile_rows = None

       return immutable ExportBundle
   finally:
       conn.rollback()
```

No final output file is touched until this function returns successfully.

**[review-fix R3]** The test for snapshot consistency does not merely inspect `conn.in_transaction`; it proves snapshot behavior using a second connection that commits between two exporter reads.

---

### 8.3 Config-generation integrity

Collect config hashes from:

```text
answers.config_hash
progress.config_hash
arm_assignments.config_hash
```

Any value other than `live_hash` causes refusal.

Example:

```text
Error: database contains records from another study configuration.
Live config: 3f9a…
Foreign records:
  answers:        8c21… (17 rows)
  progress:       8c21… (2 rows)
  arm_assignments: 8c21… (2 rows)
Refusing interpreted CSV export.
```

No override flag is advertised.

Rationale:

* answer hash is per answer, not per export row;
* question response type/options may have changed;
* an old numeric timepoint may have a different `t_index`;
* a question id may have changed semantic meaning while retaining the same text id;
* pretending those rows fit the live model is worse than refusing.

A future raw recovery export may expose:

```text
clinician_id,
patient_id,
timepoint,
question_id,
value,
arm,
config_hash,
ts_recorded
```

without interpreting values through the live question model.

That feature is explicitly outside S9c.

---

### 8.4 Arm integrity

For every exported pair:

1. a locked `arm_assignments` row must exist;
2. its `config_hash` must equal `live_hash`;
3. every `answers.arm` for that pair must equal the assignment's `arm`.

If not:

```text
Error: arm mismatch for patient synth_001, clinician a1b2…: assignment=no_ai, answer row=ai
```

No row is exported.

**[review-fix R4]** A mismatch is not downgraded to a warning and no source is silently selected as “winner.”

`arm_source` is fetched because it is part of the assignment record and may be useful for diagnostics/future S11 work, but it is not exported in S9c.

---

### 8.5 Output staging and installation

**[review-fix R7]**

`write_export` owns final-path safety.

Preflight before staging:

* `out.resolve()` and `keyfile.resolve()` must differ;
* both parents are creatable;
* existing finals are rejected unless `force=True`;
* if keyfile requested, platform/filesystem must support the required private permission semantics.

Then:

1. write answers to a sibling temporary file;
2. if requested, create keyfile temp with private permissions from creation time;
3. verify keyfile has no group/other permission bits;
4. flush/close both;
5. install final paths;
6. remove temporary paths in `finally`.

The keyfile is not created world-readable and then chmodded afterwards.

On POSIX, create the keyfile temp with an API equivalent to:

```python
os.open(
    temp_path,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
    0o600,
)
```

and verify:

```python
stat.S_IMODE(path.stat().st_mode) == 0o600
```

On a platform where the application cannot provide the promised keyfile protection, `--keyfile` refuses instead of silently weakening it.

Without `--force`, installation uses a no-clobber operation so a race creating the destination after preflight does not overwrite it.

With `--force`, a completed staged file replaces the final path.

The two-file operation is not described as crash-atomic: two filesystem paths cannot be committed as one portable atomic transaction. Normal validation happens before either final path is installed; unexpected filesystem failure during final installation is surfaced explicitly.

---

## 9. Test inventory

The acceptance bar is behavior-based rather than a hard-coded total pytest collection count.

### `tests/test_answer_codec.py`

1. categorical stored value must be a configured option;
2. valid multi-select JSON decodes in configured order;
3. malformed multi-select JSON raises;
4. non-list multi-select JSON raises;
5. unknown / duplicate / non-string multi-select members raise;
6. non-canonical multi-select order raises;
7. empty persisted multi-select raises;
8. likert rejects non-canonical integers and out-of-range values;
9. probability rejects non-canonical integers and values outside 0–100;
10. free-text storage-contract violation raises;
11. existing lenient `deserialize_answer` behavior remains unchanged for UI pre-fill.

### `tests/test_export.py`

12. fixed columns precede questions in YAML order;
13. row per pair per unlocked timepoint;
14. answer-only pair uses highest answered live `t_index`;
15. progress-only pair emits unlocked rows with empty answers;
16. `--only-complete` omits answer-only pair rather than raising;
17. unanswered optional question is `""`;
18. completed timestamp repeated on every pair row;
19. completed timestamp formatting is exact;
20. deterministic patient / clinician / t-index order;
21. timepoint formatting uses `repr(float)`;
22. identical input produces byte-identical CSV;
23. multi-select pipe encoding preserves option order;
24. question id colliding with fixed export column raises;
25. pipe-containing multi-select option cannot reach exporter;
26. unknown question id in DB raises;
27. unknown timepoint in DB raises;
28. unknown patient in DB raises;
29. invalid progress frontier raises;
30. answer beyond progress frontier raises;
31. `completed_at` before final frontier raises;
32. missing arm assignment raises;
33. answer/assignment arm mismatch raises;
34. answer hash drift raises;
35. progress hash drift raises;
36. arm-assignment hash drift raises;
37. multiple hashes in one timepoint are refused rather than collapsed;
38. malformed persisted answer raises with identifiers but not raw value;
39. `guard_cell` regression over `= + - @ TAB CR LF` and full-width variants;
40. guard leaves empty string unchanged;
41. guard leaves leading apostrophe unchanged, including `'=1+1`;
42. trigger in position 2 is unchanged;
43. guard applied to metadata and question values;
44. UTF-8 / comma / quote / newline round-trip;
45. pandas read-back with `dtype=str, keep_default_na=False` equals the **guarded** frame;
46. no BOM;
47. empty DB produces header-only frame;
48. clinicians DAO is never called when keyfile not requested;
49. keyfile includes only clinician ids represented in the export;
50. explicit read transaction gives a consistent WAL snapshot across DAO reads;
51. export module imports no `ehr_simulator.web`.

### `tests/test_cli.py`

52. happy path writes file and prints counts;
53. default output path is under `<db parent>/exports`;
54. existing output refused without `--force`;
55. `--force` replaces a completed destination;
56. missing DB / stale schema / integrity failures exit 1 and create no final output;
57. `--keyfile` writes the mapping with mode 0600;
58. answers file and keyfile cannot be the same resolved path;
59. keyfile unsupported-permission platform refuses;
60. live-server/WAL test succeeds while another connection has an uncommitted write and does not see that write.

For test #60, do **not** call `answers.upsert()` to create the pending write, because that DAO commits internally.

**[review-fix R8]**

Instead:

```python
writer.execute("BEGIN IMMEDIATE")
writer.execute("INSERT ...")
# no commit
run export from second connection
```

Then assert:

* export succeeds;
* uncommitted row is absent;
* writer remains usable.

### `tests/test_config.py`

61. `|` inside a multi-select option is rejected;
62. `|` inside a categorical option remains accepted.

### `tests/test_db.py`

63. `answers.fetch_all` returns deterministic typed rows;
64. `progress.fetch_all` keyed by pair;
65. `arm_assignments.fetch_all` returns arm, source and hash;
66. `clinicians.fetch_by_ids` returns only requested ids in deterministic order;
67. read-only connection rejects INSERT;
68. read-only connection does not create a missing DB.

Existing browser/e2e tests remain green.

---

## 10. CI changes

After the existing DB smoke migration/backup work:

```yaml
uv run ehr-simulator export-answers \
  configs/example_config.yaml configs/example_questions.yaml \
  --db-path /tmp/ci_db_smoke/test.db \
  --out /tmp/ci_db_smoke/answers.csv \
  --keyfile /tmp/ci_db_smoke/clinicians.keyfile.csv

test "$(head -1 /tmp/ci_db_smoke/answers.csv)" = \
  "patient_id,clinician_id,t_index,timepoint_minutes,arm,completed_at,config_hash,deterioration_6h,survives_hospital,good_outcome_3mo,dead_6mo,confidence,contributing_factors,free_notes"

test "$(stat -c %a /tmp/ci_db_smoke/clinicians.keyfile.csv)" = "600"
```

The empty migrated DB legitimately produces:

* answers CSV: header only;
* keyfile CSV: header only;
* exit 0.

The literal answers header is intentional: changing example question order or export metadata ordering should fail CI until the contract is deliberately updated.

---

## 11. Commit discipline

Target: four commits.

### 1. `Lift answer codec and add read-only export reads`

Includes:

* `answer_codec.py`;
* strict stored decoder;
* read-only `AccessMode`;
* DAO read functions;
* config pipe validator;
* codec / DB tests.

Existing answer-capture behavior remains green.

### 2. `Add strict CSV export service`

Includes:

* `export.py`;
* snapshot transaction;
* config-generation refusal;
* arm/data-integrity checks;
* CSV guard;
* staged output;
* service tests.

### 3. `Add export-answers CLI and CI smoke`

Includes:

* CLI command;
* output-path handling;
* keyfile option;
* `.gitignore`;
* CLI tests;
* CI smoke.

### 4. `Close S9c TODOs and roadmap pointers`

Includes:

* `TODOS.md`;
* `ROADMAP.md`;
* final spec/release pointers.

Each commit:

```text
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

must be green before landing.

---

## 12. Acceptance criteria

### Functional export

Running:

```text
uv run ehr-simulator export-answers \
  configs/example_config.yaml \
  configs/example_questions.yaml \
  --db-path data/ehr_simulator.db
```

against an S9b walk produces the §6 header and deterministic tidy rows.

### Formula guard

After clinician free-text answers beginning with:

```text
=1+1
-3 points
@SUM(1,1)
```

the generated CSV contains guarded textual values and opening the freshly generated file in the pilot's spreadsheet application does not evaluate them as formulas.

Automated tests cover the complete trigger set including LF and full-width prefixes.

The acceptance criterion applies to the generated file, not an Excel-resaved derivative.

### Identifying-name separation

The answers exporter never queries `clinicians.name_normalized` unless `--keyfile` is requested.

The answers CSV contains no mapping column and no value sourced from `clinicians.name_normalized`.

This is deliberately narrower than claiming the CSV contains “no names”: clinician-entered free text may itself contain identifying text.

**[review-fix R9]**

### Keyfile

With `--keyfile`:

* output mode is exactly 0600 on supported systems;
* rows contain only clinician ids represented in this export;
* no unrelated clinician login mappings are included.

### Config drift

Changing a study/question config and then attempting export against rows from the previous generation exits 1.

This applies when drift appears in any of:

* answers;
* progress;
* arm assignments.

There is no `--allow-mixed-config` success path.

### Persisted corruption

Manually corrupting an answer value produces exit 1 with an identifying cell location and no final CSV.

Malformed persisted data is never silently represented as blank.

### Arm integrity

Changing an answer's denormalized `arm` so it disagrees with the locked assignment causes exit 1.

### Live server

Export can run while `serve` is active.

A writer transaction left uncommitted does not block or leak into the read-only export snapshot under WAL.

A committed change made by another connection after the export snapshot has begun is not partially observed by later exporter DAO reads.

### Quality

```text
uv run pytest
uv run pytest -m e2e
uv run ruff check .
uv run ruff format --check .
```

all pass.

No exact total pytest collection number is an acceptance criterion; behavior is.

---

## 13. Conventions

### Service decides, CLI reports

`cli.py` does not decide:

* which rows belong in the export;
* whether drift is acceptable;
* whether stored answers are valid;
* which arm wins;
* how incomplete walks are represented.

Those belong to `export.py`.

### Validate before file output

Research data is:

1. read;
2. integrity-checked;
3. decoded;
4. shaped;
5. reported;

before final output installation begins.

### Explicit snapshot

Never use:

```python
with conn:
```

as shorthand for “all SELECTs share a snapshot.”

The snapshot begins with explicit SQL `BEGIN`.

### Strict at research boundaries, lenient at UI boundaries

Browser pre-fill may retain the existing forgiving decoder.

Export and future analysis consumers use strict persisted-value decoding.

### Guard at the file boundary

`ExportFrame` is semantic data.

`guard_cell` belongs only in CSV serialization so in-process analysis code never sees export-escape artifacts.

### No pandas in the write path

No DataFrame construction or implicit type inference is used to create the export.

Pandas is allowed in tests and downstream analysis when called with explicit string-preserving options.

### Read-only means read-only

Export does not migrate, checkpoint, repair or normalize the source DB.

A database requiring repair/migration is refused.

### Timestamps are serialized explicitly

`completed_at` is received as a Python `datetime` under `PARSE_DECLTYPES` and serialized explicitly to the documented CSV format.

Do not rely on `str()` accidentally matching SQLite formatting.

**[review-fix R10]**

### `ts_recorded` remains unexported

`AnswerRow` may include `ts_recorded` for S10/future work, but S9c does not add per-question timestamp columns.

---

## 14. Deferred decisions / TODOs

### S10 — events export

Dwell time, blocked-advance counts and edit counts remain in `events`.

S10 may query the DB directly or introduce `export-events`.

### Raw mixed-generation recovery export

If real pilot operations require recovery of a DB spanning config generations, implement a separate long-form/raw export that does not decode values through the live question model.

Do not add an override to the tidy interpreted exporter.

### Wide per-pair pivot

Downstream:

```python
pd.read_csv(...).pivot(...)
```

can create `{question_id}_t{timepoint}` analysis columns.

No second S9c export format.

### S11 — `arm_source`

Revisit whether the analysis CSV should include:

```text
arm_source
```

once assignments can come from both phase-1 stub and phase-2 randomization.

S9c validates/fetches assignment provenance but exports `arm` only.

### Per-question `ts_recorded`

Add `<question>_ts` only if an analysis requires within-timepoint answer timing and events cannot provide it.

### Filters

Potential future flags:

```text
--patients
--clinician-id
```

Wait for a concrete use case.

Do not add filtering by clinician login name to the answers export path.

### Excel-specific output

Bare UTF-8 remains the default.

An opt-in Excel-oriented format/BOM may be added only if collaborators encounter an actual interoperability problem.

### Windows keyfile ACLs

Exact secure Windows ACL behavior is outside the pilot scope.

S9c refuses `--keyfile` where its 0600-equivalent promise cannot be implemented and verified.

---

## 15. Decisions S9c does lock

### Tidy export shape

One row per:

```text
patient_id × clinician_id × timepoint
```

with one column per question.

Reasons:

1. matches `plan.md`;
2. matches `CLAUDE.md`;
3. stays narrow as the number of timepoints grows;
4. supports S10 grouping directly;
5. wide-per-pair form is a downstream pivot.

The ROADMAP is updated to point here.

### Interpreted export is live-generation only

This session does **not** attempt best-effort interpretation of historical question schemas.

Foreign generation = refusal.

### `clinician_id` is the exported pseudonym

The existing id is deterministic and unsalted.

S9c does not claim that this makes re-identification cryptographically impossible; changing the pseudonym scheme is a D9/IRB-policy decision.

The export's concrete guarantee is:

> `clinicians.name_normalized` is not read unless the keyfile is explicitly requested, and is never written into the answers CSV.

### Empty DB succeeds

A valid migrated DB with no exportable walks produces a header-only CSV and exit 0.

“What has been recorded?” may legitimately have the answer “nothing.”

### No clinician-name flag on the answers CSV

There is deliberately no:

```text
--include-clinician-name
```

The mapping remains a separate file.

### Integrity errors are not repaired during export

The exporter never silently:

* chooses one config generation;
* chooses one arm source;
* drops unknown question rows;
* converts malformed values to blank;
* clips an invalid frontier;
* moves answers to another timepoint.

Repair, if ever required, is a separate explicit operator workflow.

---

## 16. Existing components reused

* S9a answer storage contract;
* existing web `deserialize_answer` behavior for pre-fill;
* `progress.completed_at`;
* `progress.unlocked_t_index`;
* `arm_assignments`;
* `compute_config_hash_from_models`;
* `resolve_db_path`;
* schema-current assertion;
* logging setup;
* existing walked-DB fixture pattern.

The codec extraction changes dependency direction, not browser semantics.

---

## 17. Verification — end to end

### 1. Automated suite

```text
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

All green.

### 2. Empty export

```text
uv run ehr-simulator migrate --db-path /tmp/s9c/test.db

uv run ehr-simulator export-answers \
  configs/example_config.yaml \
  configs/example_questions.yaml \
  --db-path /tmp/s9c/test.db \
  --out /tmp/s9c/empty.csv
```

Expected:

* exit 0;
* stdout says `0 rows`;
* file contains exactly the header.

### 3. Create live study data

Start:

```text
uv run ehr-simulator serve \
  --config configs/example_config.yaml \
  --questions configs/example_questions.yaml \
  --db-path /tmp/s9c/test.db
```

Log in as a test clinician.

Walk:

* `synth_001` to completion;
* enter `=1+1` in free notes at one timepoint;
* enter `-3 points` at another;
* walk `synth_002` only part-way.

### 4. Export while server remains running

```text
uv run ehr-simulator export-answers \
  configs/example_config.yaml \
  configs/example_questions.yaml \
  --db-path /tmp/s9c/test.db \
  --out /tmp/s9c/walk.csv \
  --keyfile /tmp/s9c/k.keyfile.csv
```

Expected:

* exit 0;
* counts match unlocked rows;
* one complete and one in-progress walk;
* keyfile mode = `600`;
* keyfile contains the one exported clinician mapping;
* answers CSV contains no value sourced from `name_normalized`.

### 5. Spreadsheet verification

Open the freshly generated `walk.csv`.

Verify free-text cells display textual guarded values and no formula is evaluated.

This is an initial-open check, not a promise about save/re-open behavior after spreadsheet transformation.

### 6. Live writer verification

While the server remains open:

* continue recording answers;
* confirm no `database is locked` failure caused by export.

Automated test separately proves snapshot isolation using two SQLite connections.

### 7. Config drift verification

Change a prompt or other hash-relevant config value.

Attempt export against the existing DB.

Expected:

```text
exit 1
Error: database contains records from another study configuration...
```

No final CSV.

There is no override flag.

### 8. Corrupt answer verification

In a disposable test DB, manually replace a multi-select answer with invalid JSON.

Export.

Expected:

* exit 1;
* error identifies patient / clinician / timepoint / question;
* raw stored value is not printed;
* no final CSV.

### 9. Arm mismatch verification

In a disposable DB, manually change one `answers.arm`.

Export.

Expected:

* exit 1;
* arm mismatch named;
* no final CSV.

### 10. Browser regression

```text
uv run pytest -m e2e
```

Existing browser tests remain green.

---

## 18. Review history

| Review-fix | Original design                                                                                    | Revised design                                                                                              |
| ---------- | -------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| R1         | `--allow-mixed-config` attempted to merge foreign answer generations into tidy rows.               | Interpreted export refuses every foreign answer/progress/assignment hash; raw recovery export deferred.     |
| R2         | Export reused UI `deserialize_answer`, which maps malformed multi-select storage to `[]`.          | Add strict persisted-value decoder; corruption fails export.                                                |
| R3         | `with conn:` described as one SQLite read snapshot.                                                | Explicit `BEGIN` / `ROLLBACK`; behavioral two-connection snapshot regression.                               |
| R4         | Arm assignment won on mismatch and export continued with a warning.                                | Mismatch is a data-integrity refusal.                                                                       |
| R5         | `--only-complete` indexed `progress[pair]` even for answer-only pairs.                             | Missing-progress pairs are simply incomplete and omitted.                                                   |
| R6         | Formula triggers covered `=+-@`, TAB and CR only.                                                  | Add LF and full-width variants; clarify mitigation limits.                                                  |
| R7         | CSV/keyfile writes had an underspecified “nothing written on refusal” guarantee.                   | Validate first, stage files, create keyfile private from first byte, install only after success.            |
| R8         | Live-write test proposed an uncommitted `answers.upsert`, but that DAO commits internally.         | Test uses explicit SQL in an uncommitted writer transaction.                                                |
| R9         | Privacy wording implied exported content could contain no clinician names at all.                  | Guarantee is specifically that the login-name mapping is not read/exported; free text remains user content. |
| R10        | `completed_at` was described as exported “as stored,” though SQLite conversion returns `datetime`. | Explicit canonical timestamp serialization.                                                                 |

---


