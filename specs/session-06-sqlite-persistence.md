# Session 06 — SQLite persistence + full schema + clinician login

**Goal:** stand up the durable response store + the full data model from the design doc + a minimal `/login` flow so the simulator can attribute writes to a clinician end-to-end. S6 ships seven SQLite tables (`clinicians`, `sessions`, `arm_assignments`, `answers`, `events`, `ingestion_issues`, `schema_migrations`) under a ~40-line Python migrations runner, WAL + `synchronous=NORMAL` PRAGMAs at boot, named indexes per the roadmap, the `phase1_stub` arm assigner (always `no_ai`, scaffold for S11), a `clinicians.lookup_or_create` flow gated on a case-folded + trimmed name, a backup hook fired from both a `ehr-simulator backup` CLI command and the FastAPI lifespan shutdown handler **(only when the session actually wrote to the DB — see §6.4)**, and a `/login` GET/POST + cookie that binds `clinician_id` into the existing structlog context var so every request from S6 onward carries clinician identity in JSONL logs and SQLite rows alike. Answer capture, `/advance` gating, and CSV export all stay in S9a/b/c per the roadmap split.

**Out of scope (later sessions):** answer capture POST endpoint + auto-save-on-blur (S9a); `/advance` endpoint + question gating (S9b); `export-answers` CLI command + CSV-injection guard + pseudonymization keyfile (S9c); randomized arm assignment (S11 — S6 ships only the `phase1_stub` constant assigner); divergence view (S10); polished v1.0 release artifacts (S12); Geneva AI predictions adapter (S7); real-data UI on Geneva (S8); session `ended_at` lifecycle (deferred to S9b alongside `/advance`); auth beyond name-typed-into-form (no password, no SSO — local-only per design doc); DICOM / FHIR / mobile (TODOS.md); `events.kind` enumerated `Literal` type (deferred to S9a when more producers exist); backup rotation policy (v1.0 prep).

> **Spec history:** revised 2026-05-10 by `/plan-eng-review` to fix 13 architectural / code-quality / coverage issues surfaced in review (sentinel-row FK break, migration atomicity, `compute_config_hash` signature, `db_path` validator gap, ingestion_issues per-boot growth, loader-closure attribute, conftest db isolation, backup-on-every-shutdown noise, AdapterError-only catch, HTMX 303 swap-failure, cookie-tamper validation cost, backup-vs-graceful-timeout, `db_path` traversal guard, post-migrate WAL checkpoint). Each fix is annotated `[review-fix R<N>]` inline; full table at §18.

---

## 1. Context

S5 shipped the configuration layer: Pydantic-validated `study_config.yaml` + `questions.yaml`, a Typer CLI (`serve`, `validate-config`, `validate-adapter`, `preflight`, `preview`), CSP middleware, and `compute_config_hash(study_path, questions_path) -> str` (64-char SHA256 hex over the canonicalized parsed model — already the contract S6 imports for the `config_hash` columns; S6 adds a `compute_config_hash_from_models(study, questions)` sibling so the lifespan that already holds the parsed models doesn't re-parse YAML — see §2 row 6 + [review-fix R3]). The simulator can boot against synthetic, Geneva, or MIMIC, but every clinician interaction is in-memory only: navigating timepoints emits structlog events to JSONL but no row lands in any persistent store. There is no clinician identity capture; the `clinician_id` ContextVar exists in `logging.py` but stays None forever.

S6 closes that gap. Without persistence, the next two sessions have nothing to write to: S9a's `/answer` upsert and S9b's `/advance` gating both key on `(clinician_id, patient_id, timepoint, question_id)` rows in `answers`. Without `clinician_id` capture, the design-doc primary endpoints — "did AI assistance change clinician X's answer to question Y for patient Z at timepoint T" — have no joinable identifier on either side. Without the unique-constraint upsert behavior locked here, a network-retry double-submit in S9a would silently produce two rows for the same `(clinician, patient, timepoint, question_id)` cell and silently corrupt the analysis. Without the backup hook, every Phase 1 pilot session that ships before S11 violates the D10 commitment.

S6 also lands two upstream-deferred items. The `/login` flow was punted from S5 (S5 spec §14: *"`clinician_name` login flow — deferred to S6 (alongside the `clinicians` table)"*). The backup cadence was reassigned from S5 → S6 by /plan-eng-review on S5 (TODOS.md: *"Wire backup cadence into S6 SQLite boot. D10 is design-doc-load-bearing from Phase 1 ship onward; the single Phase-2 policy gate cannot retroactively cover Phase-1 sessions."*). Both close in this session.

The `serve` synthetic-only path stays runnable without a study config; the no-config app gets a default DB at `./data/ehr_simulator.db` and the `/login` flow still works (clinician identity is per-deployment, not per-study). The roadmap pins S8 as the session that validates real-data UI experience under perf load; S6 lands the persistence plumbing so S8/S9a/b/c are mechanical.

---

## 2. Deliverables

| #  | Path | Purpose |
|----|---|---|
| 1  | `pyproject.toml` | No new deps. Python 3.11+ stdlib `sqlite3` covers everything S6 needs (per design-doc local-only constraint). The `src/ehr_simulator/db/` directory is force-included by hatch automatically (matches `ingestion/data/` pattern); no `force-include` entries needed because no data files ship with `db/`. |
| 2  | `src/ehr_simulator/db/__init__.py` | NEW. Re-exports the public surface: `connect`, `apply_migrations`, `MIGRATIONS`, `DbError`, `clinicians`, `sessions`, `arm_assignments`, `answers`, `events`, `ingestion_issues`, `backup`. |
| 3  | `src/ehr_simulator/db/connection.py` | NEW. `connect(db_path: Path, *, apply_pragmas: bool = True) -> sqlite3.Connection` — opens a connection with `detect_types=PARSE_DECLTYPES`, sets `row_factory=sqlite3.Row`, and applies `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON;` when `apply_pragmas=True`. Also `resolve_db_path(study: StudyConfig | None, *, cli_override: Path | None = None) -> Path` (precedence: CLI flag → `EHR_SIM_DB_PATH` env → `study.db_path` → default `Path("data/ehr_simulator.db")`). All non-CLI sources additionally pass through `_db_path_traversal_guard` (rejects `..` segments and absolute paths outside the CWD subtree — see §6.3 + [review-fix R13]). |
| 4  | `src/ehr_simulator/db/migrations.py` | NEW. `Migration` namedtuple `(version: int, name: str, up_sql: str)` + module-level `MIGRATIONS: tuple[Migration, ...]` constant. `apply_migrations(conn) -> list[int]` runs every unapplied migration, records each in `schema_migrations(version, applied_at)`, returns the list of versions applied. **Idempotent:** re-running with no pending migrations is a no-op + logs `event_kind="db.migrate.noop"`. **Recoverable on partial-apply:** the DDL uses `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` so a mid-DDL crash on retry re-applies cleanly without "table already exists" errors (see §5 + [review-fix R2]). S6 ships exactly one migration: `001_initial`. |
| 5  | `src/ehr_simulator/db/exceptions.py` | NEW. `class DbError(Exception)` raised by DAO functions on integrity violations the caller is expected to handle (e.g., FK violation when inserting an `events` row whose `session_id` references a non-existent session). `sqlite3.IntegrityError` is wrapped at the DAO boundary; `sqlite3.OperationalError` is not (boot-time errors propagate verbatim). |
| 6  | `src/ehr_simulator/db/clinicians.py` | NEW. `lookup_or_create(conn, raw_name: str, *, known_clinicians: set[str] \| None = None) -> str` — case-folds + trims `raw_name`, INSERT-OR-IGNOREs into `clinicians(clinician_id, name_normalized, first_seen_at)`, returns the canonical `clinician_id`. `clinician_id` is the SHA256-hex-truncated-to-16 of `name_normalized` (deterministic, pseudonymizable, fits in a cookie). Two round-trips on existing clinician (INSERT-OR-IGNORE returns no row, fallback `SELECT` resolves) — fine at the pilot scale of one POST per session. When `known_clinicians` is provided (the lifespan-scoped cache, see [review-fix R11]), the function adds the new id to the set after a successful insert. Empty / whitespace-only `raw_name` raises `ValueError("name must be non-empty after trimming")`. |
| 7  | `src/ehr_simulator/db/sessions.py` | NEW. `start_or_resume(conn, clinician_id, patient_id, *, arm: str, config_hash: str) -> str` — looks up the latest session for `(clinician_id, patient_id)`; if `ended_at IS NULL`, returns it; otherwise inserts a new one. Returns `session_id` (UUID4 hex). S6 never sets `ended_at` — that's S9b's `/advance`-final-timepoint path. |
| 8  | `src/ehr_simulator/db/arm_assignments.py` | NEW. `assign_or_lookup(conn, clinician_id, patient_id, *, config_hash: str) -> tuple[str, str]` returning `(arm, arm_source)`. **S6 ships the `phase1_stub` assigner only:** always returns `("no_ai", "phase1_stub")`. S11 swaps the body to a deterministic randomized assigner; the call signature stays. The `arm_assignments` row is INSERT-OR-IGNORE'd — first call for a `(clinician, patient)` pair locks the assignment forever. **REGRESSION (test #11):** switching the stub to randomized in S11 must NOT rewrite existing rows. The S11 seed strategy (which fills `arm_assignments.seed` for `phase2_randomized` rows) is deferred — S6 leaves seed NULL and adds a TODO. |
| 9  | `src/ehr_simulator/db/answers.py` | NEW. `upsert(conn, *, clinician_id, patient_id, timepoint, question_id, value, arm, config_hash) -> None` — `INSERT INTO answers (...) VALUES (...) ON CONFLICT(clinician_id, patient_id, timepoint, question_id) DO UPDATE SET value=excluded.value, arm=excluded.arm, config_hash=excluded.config_hash, ts_recorded=CURRENT_TIMESTAMP`. The unique-constraint name is `ux_answers_cell` (used by index DDL). `value` stored as TEXT (callers are responsible for serialization — S9a will JSON-encode multi-select). S6 ships the upsert + tests; no route writes to the table yet. **Increments `app.state.write_counter` on every successful upsert** (used by the shutdown-backup gate, see §6.4). |
| 10 | `src/ehr_simulator/db/events.py` | NEW. `append(conn, *, session_id, clinician_id, patient_id, timepoint, kind, payload, client_ts=None, client_seq=None) -> int` — inserts one row, returns `event_id` (autoincrement). `payload` is dict-like and JSON-encoded (`json.dumps(payload, sort_keys=True, separators=(",", ":"))`). `server_ts` is set to `CURRENT_TIMESTAMP` by the DDL DEFAULT. **`session_id` is nullable** (per [review-fix R1]) — clinician-level events (`clinician.login`, `clinician.logout`) pass `session_id=None`; patient-scoped events from S9a/b will pass a real session_id. S6 fires exactly one event from a real route: `kind="clinician.login"` with `session_id=None` on `/login` POST success (smoke for the events table). **Increments `app.state.write_counter` on every successful append** (used by the shutdown-backup gate, see §6.4). |
| 11 | `src/ehr_simulator/db/ingestion_issues.py` | NEW. `record_batch(conn, dataset: str, boot_id: str, issues: Iterable[IngestionIssue]) -> int` — inserts one row per `IngestionIssue` **inside one transaction via `conn.executemany`** (P1 perf fix), tags every row with the lifespan-scoped `boot_id` so re-recording across boots doesn't conflate issues from different runs (see [review-fix R5]), returns the count. Called from the lifespan **after** `app.state.dataset = dataset_loader()` succeeds, reading `getattr(app.state.dataset, "issues", [])` — synthetic has no `.issues` attribute and falls through cleanly (see [review-fix R6]). Imports `IngestionIssue` from `ehr_simulator.ingestion.exceptions`. |
| 12 | `src/ehr_simulator/db/backup.py` | NEW. `create_backup(db_path: Path, backup_dir: Path) -> Path` — uses SQLite's online backup API (`sqlite3.Connection.backup()`, blocking, safe under WAL) to copy `db_path` to `backup_dir / f"ehr_simulator_{utc_iso}.db"`. Returns the backup file path. Logs `event_kind="db.backup.ok"` with `bytes_copied` and `dest`. Called from (a) the `ehr-simulator backup` CLI command (always) and (b) the FastAPI lifespan shutdown branch **only when `app.state.write_counter > 0`** — dev iteration with no writes never clutters `data/backups/` (see §6.4 + [review-fix R8]). **No rotation / pruning in S6** — researchers manage retention manually; documented in §13 as a v1.0 follow-up. **Backup time vs uvicorn graceful timeout:** at pilot scale (≤30 patients, ≤10 MB DB) the online-backup completes well inside uvicorn's 5s default; researchers running on larger DBs should set `--graceful-timeout 60` ([review-fix R12]). Documented in §13. |
| 13 | `src/ehr_simulator/db/cookies.py` | NEW. `read_clinician_id(request) -> str \| None` — reads the `ehrsim_clinician_id` cookie (unsigned: S6 ships an unsigned cookie because the local-only threat model doesn't justify the dep. Cookie is plain hex `clinician_id`. Tampering means a clinician could write under another's identity, which is not a threat in the controlled-pilot setting). `set_clinician_cookie(response, clinician_id) -> None` and `clear_clinician_cookie(response) -> None` for `/login` and `/logout` respectively. The cookie is `HttpOnly; SameSite=Strict; Path=/` — no `Secure` flag because deployments are localhost only. |
| 14 | `src/ehr_simulator/web/app.py` | MODIFIED. Lifespan grows three steps after the dataset-load branch: (a) `app.state.db = connect(db_path)`, (b) `apply_migrations(app.state.db)`, (c) `record_batch(app.state.db, dataset_name, boot_id, app.state.dataset.issues)` (when `dataset` exposes `.issues`). Lifespan also catches **all** dataset-load exceptions, not only `AdapterError` (see [review-fix R9]) — non-`AdapterError` exceptions log `event_kind="app.boot.failed"` with `error=repr(exc)` and re-raise as `SystemExit(1)` so uvicorn exits cleanly. `app.state.boot_id = uuid4().hex`, `app.state.write_counter = 0`, `app.state.known_clinicians = set(SELECT clinician_id FROM clinicians)`. Shutdown branch grows one conditional step: `if app.state.write_counter > 0: create_backup(db_path, backup_dir)` ([review-fix R8]). `create_app` gains `db_path: Path \| None = None` and `backup_dir: Path \| None = None` kwargs. `app_from_study_config` resolves `db_path` via `db.connection.resolve_db_path(study)` and passes it through. |
| 15 | `src/ehr_simulator/web/routes.py` | MODIFIED. Three new routes — `GET /login` (renders `login.html`), `POST /login` (form submit → `lookup_or_create` (with the `app.state.known_clinicians` cache passed through) → set cookie → write one `events.append(kind="clinician.login", session_id=None, ...)` → 303 redirect to `/`), `POST /logout` (clear cookie → 303 redirect to `/login`). Existing `/` and `/patient/...` routes grow a one-line clinician-id resolution preamble via the helper `_require_clinician(request) -> tuple[str \| None, Response \| None]` — returns `(clinician_id, None)` on success, `(None, redirect_response)` on failure. The redirect is **HTMX-aware** (see [review-fix R10]): when `request.headers.get("hx-request") == "true"`, it returns `Response(status_code=200, headers={"HX-Redirect": "/login"})` so HTMX swaps the full page; otherwise it returns `RedirectResponse("/login", status_code=303)`. The cache lookup on `clinician_id` (against `app.state.known_clinicians`, populated at lifespan boot, mutated by `lookup_or_create`) is what makes the cookie-tamper guard zero-DB-cost ([review-fix R11]). |
| 16 | `src/ehr_simulator/web/templates/login.html` | NEW. Minimal HTML form: one text input (`name="clinician_name"`, `required`, `autofocus`), one submit button. Form POSTs to `/login`. Inherits the existing base template's chrome (header + body wrapper). No HTMX — full page submit. Renders the `error` variable (when set by the empty-name 400 path) as a `<p class="error-flash">`. |
| 17 | `src/ehr_simulator/web/templates/_chrome.html` | MODIFIED. Header gets a "Logged in as <clinician_name> · Logout" stripe in the top-right when the clinician cookie is present (a server-side render condition; no JS). The existing chrome-A/B and `[`/`]` shortcuts unchanged. `_chrome_dense.html` and `_chrome_epic.html` both render the stripe — wired via a shared `_login_stripe.html` partial included by both. |
| 18 | `src/ehr_simulator/config/study.py` | MODIFIED. `StudyConfig` gains an optional `db_path: Path \| None = None` field. The existing `_resolve_relative_paths` `model_validator(mode="before")` is amended to include `"db_path"` in its loop, mirroring `csv_path` / `params_dir` ([review-fix R4]). A new `_db_path_safe` `field_validator` (after-resolution) **rejects `..` segments** in the resolved path ([review-fix R13]). **NOT routed through `schema_version` bump** — it's an additive optional field with a default; old `study_config.yaml` files (no `db_path`) continue to validate cleanly. The S5 §3 spec claim ("future field additions surface as breaking changes routed through `schema_version` bump") was specifically about *unknown* fields rejected by `extra="forbid"`, not about *adding* new optional fields with defaults to the model. Tested by tests #33 + #33b. |
| 19 | `src/ehr_simulator/config/loader.py` | MODIFIED. New sibling `compute_config_hash_from_models(study: StudyConfig, questions: Questions) -> str` — same canonical-payload hashing as the existing `compute_config_hash(study_path, questions_path)`, but accepts already-parsed models so the lifespan + `app_from_study_config` (which already hold the parsed models in hand) don't re-parse YAML on every restart. The path version delegates to the model version (see [review-fix R3]). |
| 20 | `src/ehr_simulator/cli.py` | MODIFIED. `serve` gains `--db-path PATH` and `--backup-dir PATH` options (both optional; resolved via `resolve_db_path` precedence chain — the CLI flag bypasses the traversal guard since it's an explicit operator decision). NEW command: `ehr-simulator backup [--db-path PATH] [--backup-dir DIR]` — runs `create_backup(...)` once, prints the destination path, exits 0. NEW command: `ehr-simulator migrate [--db-path PATH]` — runs `apply_migrations(...)` once, then `PRAGMA wal_checkpoint(TRUNCATE)` so a researcher who `cp`'s the bare `.db` afterwards doesn't lose un-checkpointed writes (see [review-fix R14]). Prints the list of versions applied, exits 0. Total CLI surface after S6: 7 commands. |
| 21 | `tests/fixtures/db/migration_001_expected_schema.sql` | NEW. The expected post-migration schema as `sqlite_master`-equivalent CREATE TABLE / CREATE INDEX statements, sorted by name. The drift-check test (test #6) compares the live schema against this fixture; updating the fixture is a deliberate review step (mirrors the `docs/data-contract.md` drift-check pattern from S3). |
| 22 | `tests/test_db.py` | NEW. ~20 test functions covering migrations runner forward + idempotent + drift + **partial-apply recovery**, PRAGMA verification, per-table CRUD, unique-constraint upsert (REGRESSION), config_hash round-trip, FK enforcement, `clinicians.lookup_or_create` case-fold + trim + empty-name reject, **events.session_id NULL path** for clinician-level events, **ingestion_issues boot_id distinctness across boots**. |
| 23 | `tests/test_db_backup.py` | NEW. ~5 test functions covering backup file creation, backup-after-write integrity, error path (`backup_dir` doesn't exist → created automatically), **lifespan-shutdown backup skipped when write_counter==0**, and **lifespan-shutdown backup runs when write_counter>0**. |
| 24 | `tests/test_login.py` | NEW. ~7 test functions covering GET `/login` renders, POST `/login` lookup-or-create + cookie + redirect, POST `/logout` clears cookie + redirect, protected routes redirect to `/login` when no cookie, name normalization, the `clinician.login` event row written with `session_id=NULL`, **POST `/login` with empty `clinician_name` returns 400**, **HTMX request to a protected route receives `HX-Redirect: /login` (not 303)**. |
| 25 | `tests/test_app.py` | EXTENDED. +3 tests: `app.state.db` exists after lifespan boot; `ingestion_issues` table is populated when the dataset carries issues (lift the synthetic loader to a fake loader that exposes `.issues`); `app.state.known_clinicians` is populated from the DB on boot. |
| 26 | `tests/test_cli.py` | EXTENDED. +3 tests: `ehr-simulator backup` happy-path, `ehr-simulator migrate` happy-path + idempotent + post-migrate WAL checkpoint, `ehr-simulator serve --db-path X` plumbs the path into `create_app`. |
| 27 | `tests/test_config.py` | EXTENDED. +2 tests: `StudyConfig.db_path` resolves relative to YAML dir; `StudyConfig.db_path` rejects `..` traversal. |
| 28 | `tests/conftest.py` | EXTENDED. +`tmp_db_path` fixture (per-test SQLite file under `tmp_path`); +`tmp_backup_dir` fixture (per-test backup dir); +`db` fixture (opens `connect(tmp_db_path)` + applies migrations + yields the connection); +`logged_in_client` fixture (TestClient with the clinician cookie pre-set AND the corresponding clinicians row pre-seeded in the DB AND added to `app.state.known_clinicians` so the cache lookup succeeds — without the seed step, the [review-fix R11] cache check would 303 every test). **The existing `client` fixture is updated to pass `db_path=tmp_db_path` + `backup_dir=tmp_backup_dir`** so tests don't pollute the working directory ([review-fix R7]). |
| 29 | `.gitignore` | MODIFIED. Add `data/` to ignore the default DB location + `data/backups/` for the backup artifacts. |
| 30 | `.github/workflows/ci.yml` | EXTENDED. One new step `DB smoke` after the existing `CLI smoke`: runs `ehr-simulator migrate --db-path /tmp/ci_smoke.db` (asserts exit 0), then re-runs it (asserts exit 0 + idempotent), then `ehr-simulator backup --db-path /tmp/ci_smoke.db --backup-dir /tmp/ci_smoke_backups` (asserts exit 0 + a `.db` file lands in the backup dir). |

`README.md` and `LICENSE` are not touched in S6. README's "only JSONL logs hit disk" paragraph (lines 134–139) needs updating to reflect SQLite + backups, but that's owned by `/document-release` post-ship per CLAUDE.md skill routing — not by the implementation session.

The `cli_support.py` modification originally proposed by the spec (a `loader.issues` closure attribute) is **dropped** — the lifespan reads from `app.state.dataset.issues` instead, eliminating the awkward closure-attribute pattern and removing 5 LOC from the proposed delta (see [review-fix R6]).

---

## 3. Repo layout after Session 6 (diff vs end-of-S5)

```
ehr_simulator/
├── src/ehr_simulator/
│   ├── cli.py                                # MODIFIED (+backup, +migrate, +serve --db-path/--backup-dir)
│   ├── config/
│   │   ├── loader.py                         # MODIFIED (+compute_config_hash_from_models)
│   │   └── study.py                          # MODIFIED (+db_path field, +traversal guard)
│   ├── db/                                   # NEW
│   │   ├── __init__.py
│   │   ├── answers.py
│   │   ├── arm_assignments.py
│   │   ├── backup.py
│   │   ├── clinicians.py
│   │   ├── connection.py
│   │   ├── cookies.py
│   │   ├── events.py
│   │   ├── exceptions.py
│   │   ├── ingestion_issues.py
│   │   ├── migrations.py
│   │   └── sessions.py
│   └── web/
│       ├── app.py                            # MODIFIED (+db lifespan steps, +write-counter, +known_clinicians cache)
│       ├── routes.py                         # MODIFIED (+/login, +/logout, +HTMX-aware preamble)
│       └── templates/
│           ├── _chrome_dense.html            # MODIFIED (+logged-in stripe via partial)
│           ├── _chrome_epic.html             # MODIFIED (+logged-in stripe via partial)
│           ├── _login_stripe.html            # NEW
│           └── login.html                    # NEW
├── tests/
│   ├── conftest.py                           # MODIFIED (+tmp_db_path, +tmp_backup_dir, +db, +logged_in_client; client passes db_path)
│   ├── fixtures/
│   │   └── db/                               # NEW
│   │       └── migration_001_expected_schema.sql
│   ├── test_app.py                           # MODIFIED (+3 tests)
│   ├── test_cli.py                           # MODIFIED (+3 tests)
│   ├── test_config.py                        # MODIFIED (+2 tests)
│   ├── test_db.py                            # NEW
│   ├── test_db_backup.py                     # NEW
│   └── test_login.py                         # NEW
├── .gitignore                                # MODIFIED
└── .github/workflows/ci.yml                  # MODIFIED (+DB smoke step)
```

`cli_support.py` is **not** modified (loader-closure attribute idea dropped).

---

## 4. SQLite schema (the canonical shape)

DDL ships verbatim inside `db/migrations.py` as the `up_sql` of `Migration(version=1, name="initial", up_sql=...)`. Spec lists the shape; implementer copies into the migration string. **Every CREATE statement uses `IF NOT EXISTS`** so partial-apply on re-run is harmless ([review-fix R2]).

```sql
CREATE TABLE IF NOT EXISTS clinicians (
    clinician_id     TEXT PRIMARY KEY,           -- SHA256(name_normalized)[:16]
    name_normalized  TEXT NOT NULL UNIQUE,       -- case-folded + stripped
    first_seen_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,             -- uuid4().hex
    clinician_id  TEXT NOT NULL REFERENCES clinicians(clinician_id),
    patient_id    TEXT NOT NULL,
    started_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at      TIMESTAMP,                    -- NULL until S9b sets it
    arm           TEXT NOT NULL,                -- 'ai' | 'no_ai'
    config_hash   TEXT NOT NULL                 -- SHA256 hex from compute_config_hash_from_models
);

CREATE TABLE IF NOT EXISTS arm_assignments (
    clinician_id  TEXT NOT NULL REFERENCES clinicians(clinician_id),
    patient_id    TEXT NOT NULL,
    arm           TEXT NOT NULL,                -- 'ai' | 'no_ai'
    arm_source    TEXT NOT NULL,                -- 'phase1_stub' | 'phase2_randomized'
    seed          INTEGER,                      -- non-NULL only for phase2_randomized (S11 design TODO)
    assigned_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    config_hash   TEXT NOT NULL,
    PRIMARY KEY (clinician_id, patient_id)
);

CREATE TABLE IF NOT EXISTS answers (
    clinician_id  TEXT NOT NULL REFERENCES clinicians(clinician_id),
    patient_id    TEXT NOT NULL,
    timepoint     REAL NOT NULL,                -- t_minutes (study-config-agnostic, matches structlog `timepoint`)
    question_id   TEXT NOT NULL,
    value         TEXT NOT NULL,                -- caller-serialized; multi-select JSON-encoded in S9a
    arm           TEXT NOT NULL,
    config_hash   TEXT NOT NULL,
    ts_recorded   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ux_answers_cell UNIQUE (clinician_id, patient_id, timepoint, question_id)
);

CREATE TABLE IF NOT EXISTS events (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT REFERENCES sessions(session_id),  -- NULL for clinician-level events (login/logout)
    clinician_id   TEXT NOT NULL REFERENCES clinicians(clinician_id),
    patient_id     TEXT,                        -- NULL for non-patient events
    timepoint      REAL,
    kind           TEXT NOT NULL,               -- 'clinician.login', 'clinician.logout', 'panel.swap', etc.
    payload_json   TEXT NOT NULL DEFAULT '{}',  -- json.dumps(payload, sort_keys=True, separators=(",",":"))
    client_ts      TIMESTAMP,                   -- browser-side timestamp (S9a auto-save uses this)
    server_ts      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    client_seq     INTEGER                      -- monotonic per-tab counter (S9a)
);

CREATE TABLE IF NOT EXISTS ingestion_issues (
    boot_id      TEXT NOT NULL,                 -- lifespan-scoped, populated by record_batch [review-fix R5]
    dataset      TEXT NOT NULL,
    patient_id   TEXT,
    row_idx      INTEGER,
    reason       TEXT NOT NULL,
    loaded_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version      INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    applied_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_events_session_id        ON events (session_id);
CREATE INDEX IF NOT EXISTS ix_events_patient_timepoint ON events (patient_id, timepoint);
CREATE INDEX IF NOT EXISTS ix_answers_patient_clinician ON answers (patient_id, clinician_id);
CREATE INDEX IF NOT EXISTS ix_arm_clinician_patient    ON arm_assignments (clinician_id, patient_id);
CREATE INDEX IF NOT EXISTS ix_ingestion_issues_boot_id ON ingestion_issues (boot_id);
```

Notes for the implementer (carried by spec, not by the DDL string):

- `events.session_id` is **nullable** (no `NOT NULL` clause). Clinician-level events (`clinician.login`, `clinician.logout`) pass `session_id=NULL`; session-scoped analyses filter `WHERE session_id IS NOT NULL`. This was the resolution to the original sentinel-row design ([review-fix R1]).
- `answers.timepoint` is `REAL`, not `INTEGER`, to match `t_minutes` in the structlog ContextVar (`logging.py:40`) and `study.timepoints_minutes` (S5 `study.py`). Float-equality on `(clinician_id, patient_id, timepoint, question_id)` is safe because the upstream is `t * 60.0` from a deterministic int — no FP drift in practice. Tested by `test_answers_upsert_idempotent_for_float_timepoint`.
- `arm_assignments.seed` is nullable in S6 (always NULL because S6's stub doesn't randomize). S11's spec must define how seed is populated for `phase2_randomized` rows (deterministic from `(study_hash, clinician_id, patient_id)`? top-level `study.seed` field? open) — captured as a TODO in §14.
- `events.payload_json` defaults to `'{}'` (the empty JSON object) so callers can omit it. S6's `events.append` always sets it explicitly.
- `events.client_ts` and `events.client_seq` are nullable and unused in S6 (server-side login event has no browser timestamp). S9a will start populating them.
- `ingestion_issues.boot_id` is a UUID4 hex set once per app boot (`app.state.boot_id`). Analyses can filter to the latest boot (`SELECT * FROM ingestion_issues WHERE boot_id = (SELECT boot_id FROM ingestion_issues ORDER BY loaded_at DESC LIMIT 1)`) or aggregate across boots. The dedicated index makes both fast.
- The `ix_*` index names follow the project's prefix convention (consistent with `ux_answers_cell` for the unique constraint). Drift-check test #6 asserts these names verbatim.

---

## 5. Migrations runner shape

```python
# db/migrations.py (excerpt)
from typing import NamedTuple

class Migration(NamedTuple):
    version: int
    name: str
    up_sql: str

MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="initial", up_sql=_INITIAL_DDL),
)

def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply every unapplied migration. Idempotent.

    Atomicity note: ``conn.executescript()`` issues an implicit COMMIT both
    on entry and exit, so wrapping it in ``with conn:`` does NOT give us
    transactional rollback. Instead we rely on the DDL being written with
    ``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` so a
    mid-DDL crash leaves a partial schema that the next run cleanly extends.
    The ``schema_migrations`` row is the source-of-truth lock — if it isn't
    written, the migration is "not applied" and re-runs in full. This is
    test #4b's invariant. See [review-fix R2].
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    pending = [m for m in MIGRATIONS if m.version not in applied]
    if not pending:
        get_logger().info("migrations noop", event_kind="db.migrate.noop")
        return []
    versions = []
    for m in pending:
        conn.executescript(m.up_sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
            (m.version, m.name),
        )
        conn.commit()
        get_logger().info(
            "migration applied",
            event_kind="db.migrate.applied",
            version=m.version,
            name=m.name,
        )
        versions.append(m.version)
    return versions
```

Why a Python list, not SQL files in a directory? The list ships in the package, no install-time file-discovery dance, no `pkg_resources` shenanigans, and the test suite asserts `MIGRATIONS` is the tuple it expects (test #5). When S6.5+ adds migrations, append to the tuple; the runner picks them up via `version not in applied`.

---

## 6. Connection management + integration with FastAPI lifespan

### 6.1 Lifespan flow ASCII overview

```
                                 ┌───────────────────────┐
   uvicorn boot ──▶ setup_logging│  app.state.boot_id    │
                       │         │  app.state.write_ctr=0│
                       ▼         └──────────┬────────────┘
              dataset_loader()              │
              ┌────────┴──────────┐         │
        AdapterError       OtherException   │
        (validate-time      (FileNotFound,  │
         schema fail)        OSError, ...)  │
              │                  │          │
              ▼                  ▼          │
         log+exit 1         log+exit 1      │
                                            ▼
                          ┌────────────────────────────────┐
                          │ db_path = resolve_db_path(...) │
                          │ app.state.db = connect(...)    │
                          │ apply_migrations(app.state.db) │
                          │ app.state.known_clinicians =   │
                          │   set(SELECT clinician_id ...) │
                          └──────────────┬─────────────────┘
                                         ▼
                  if dataset has .issues:
                    record_batch(db, name, boot_id, issues)
                                         │
                                         ▼
                                    log("app.boot")
                                         │
                                       ─yield──
                                         │
                                ┌────────▼────────────────┐
                                │ shutdown:               │
                                │   if write_counter > 0: │
                                │     create_backup(...)  │
                                │   db.close()            │
                                └─────────────────────────┘
```

### 6.2 Lifespan code (excerpt; spec, not literal)

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(log_dir)
    log = get_logger()
    app.state.boot_id = uuid4().hex
    app.state.write_counter = 0

    try:
        app.state.dataset = dataset_loader()
    except AdapterError as exc:
        # ... S5-unchanged: log issues + exit 1 ...
        raise SystemExit(1) from exc
    except Exception as exc:  # [review-fix R9]
        log.error("boot failed", event_kind="app.boot.failed", error=repr(exc))
        raise SystemExit(1) from exc

    db_path_resolved = db_path or resolve_db_path(None)
    db_path_resolved.parent.mkdir(parents=True, exist_ok=True)
    app.state.db = connect(db_path_resolved)
    versions = apply_migrations(app.state.db)
    log.info("db ready", event_kind="db.ready", db_path=str(db_path_resolved), migrations=versions)

    app.state.known_clinicians = {
        row[0] for row in app.state.db.execute("SELECT clinician_id FROM clinicians")
    }

    issues = getattr(app.state.dataset, "issues", [])  # [review-fix R6]
    if issues:
        dataset_name = issues[0].dataset  # every IngestionIssue carries it
        n = ingestion_issues.record_batch(
            app.state.db, dataset_name, app.state.boot_id, issues
        )
        log.info("ingestion issues recorded", event_kind="db.ingestion_issues.recorded", count=n)

    log.info("boot ok", event_kind="app.boot")
    try:
        yield
    finally:
        try:
            if app.state.write_counter > 0:  # [review-fix R8]
                backup_dir_resolved = backup_dir or db_path_resolved.parent / "backups"
                dest = create_backup(db_path_resolved, backup_dir_resolved)
                log.info("backup ok", event_kind="db.backup.ok", dest=str(dest))
            else:
                log.info("backup skipped (no writes)", event_kind="db.backup.skipped")
        except Exception as exc:
            log.error("backup failed", event_kind="db.backup.failed", error=repr(exc))
        with contextlib.suppress(Exception):
            app.state.db.close()
        log.info("shutdown", event_kind="app.shutdown")
```

The shutdown-time backup is wrapped in try/except because shutdown must not fail loudly (uvicorn exits regardless); the `db.backup.failed` event is the loud signal a tail-of-JSONL alert can pick up.

### 6.3 `connect` + `resolve_db_path`

```python
# db/connection.py (excerpt)
def connect(db_path: Path, *, apply_pragmas: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False,  # FastAPI may dispatch handlers on different threads
    )
    conn.row_factory = sqlite3.Row
    if apply_pragmas:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def resolve_db_path(study: StudyConfig | None, *, cli_override: Path | None = None) -> Path:
    if cli_override is not None:
        return cli_override  # explicit operator decision; bypasses traversal guard
    env_override = os.environ.get("EHR_SIM_DB_PATH")
    if env_override:
        return _db_path_traversal_guard(Path(env_override))
    if study is not None and study.db_path is not None:
        return study.db_path  # already guarded by StudyConfig validator
    return Path("data/ehr_simulator.db")


def _db_path_traversal_guard(p: Path) -> Path:
    """Reject .. segments and absolute paths outside the CWD subtree.

    The CLI --db-path flag bypasses this (operator decision); env var and
    study YAML go through it. See [review-fix R13].
    """
    resolved = p.resolve() if p.is_absolute() else (Path.cwd() / p).resolve()
    cwd = Path.cwd().resolve()
    if not resolved.is_relative_to(cwd):
        raise ConfigError(
            f"db_path must be inside the project working directory; got {resolved}"
        )
    if ".." in p.parts:
        raise ConfigError(f"db_path must not contain '..' segments; got {p}")
    return resolved
```

`check_same_thread=False` is required because `app.state.db` is a single connection shared across FastAPI's threadpool-dispatched route handlers. SQLite's serialized threading mode (the build default since 3.5) makes this safe at the engine level for ≤1 concurrent clinician (the pilot constraint). **Note:** Python's `sqlite3` does not release the GIL for the duration of `Connection.execute()` — a slow query blocks all other request handlers. Acceptable at pilot scale; revisit if multi-clinician concurrent use ever lands.

Tested by 4 cases in test_db.py (each precedence level exercised once) + 2 cases for traversal-guard rejections.

### 6.4 Backup write-counter gate

The shutdown-time backup runs only when `app.state.write_counter > 0` ([review-fix R8]). The counter is incremented inside `answers.upsert` and `events.append` after a successful INSERT — the only DAOs that produce researcher-meaningful state changes in S6. This means:

- A pilot session (clinician logs in, walks patients, S9a will eventually upsert answers) → counter > 0 → backup runs. ✓
- Dev iteration with no login at all → counter = 0 → no backup, no clutter. ✓
- Boot, login, logout, shutdown without ever upsert'ing an answer → counter > 0 (login event incremented it) → backup runs. ✓ (researcher activity is real even if no answer landed)

Tested by 2 cases in test_db_backup.py (test #20a, #20b).

---

## 7. `/login` UI + cookie + clinician identity

### 7.1 Routes

```python
# web/routes.py (excerpt; spec, not literal)
@router.get("/login")
async def login_get(request: Request):
    return request.app.state.templates.TemplateResponse("login.html", {"request": request})

@router.post("/login")
async def login_post(request: Request):
    form = await request.form()
    raw_name = form.get("clinician_name", "").strip()
    if not raw_name:
        return request.app.state.templates.TemplateResponse(
            "login.html", {"request": request, "error": "Name required."}, status_code=400,
        )
    clinician_id = clinicians.lookup_or_create(
        request.app.state.db,
        raw_name,
        known_clinicians=request.app.state.known_clinicians,  # cache populated at lifespan
    )
    update_request_context(clinician_id=clinician_id)
    events.append(
        request.app.state.db,
        session_id=None,                  # [review-fix R1]: nullable for clinician-level events
        clinician_id=clinician_id,
        patient_id=None,
        timepoint=None,
        kind="clinician.login",
        payload={"name_normalized": _normalize(raw_name)},
    )
    request.app.state.write_counter += 1  # [review-fix R8]
    response = RedirectResponse("/", status_code=303)
    set_clinician_cookie(response, clinician_id)
    return response

@router.post("/logout")
async def logout_post(request: Request):
    response = RedirectResponse("/login", status_code=303)
    clear_clinician_cookie(response)
    return response
```

### 7.2 Protected-route preamble (HTMX-aware)

Existing `/` and `/patient/<pid>/timepoint/<t>` routes grow a one-line preamble:

```python
clinician_id, redirect = _require_clinician(request)
if redirect is not None:
    return redirect
update_request_context(clinician_id=clinician_id)
```

Where:

```python
def _require_clinician(request: Request) -> tuple[str | None, Response | None]:
    """Returns (clinician_id, None) on success or (None, redirect) on failure.

    The redirect is HTMX-aware [review-fix R10]:
      - HX-Request: true  → 200 + HX-Redirect: /login (HTMX swaps full page)
      - else              → 303 → /login (browser follows)

    Cookie validation against app.state.known_clinicians is in-memory
    [review-fix R11] — zero DB round-trips per protected request, even
    on HTMX polls.
    """
    clinician_id = read_clinician_id(request)
    if clinician_id is None or clinician_id not in request.app.state.known_clinicians:
        if request.headers.get("hx-request", "").lower() == "true":
            return None, Response(status_code=200, headers={"HX-Redirect": "/login"})
        return None, RedirectResponse("/login", status_code=303)
    return clinician_id, None
```

### 7.3 Why no sessions table involvement at /login

**The /login event has `session_id=NULL`.** A clinician's "session" is per-(clinician, patient), not per-login. The first time the clinician visits `/patient/<pid>/timepoint/<t>` (in S6 just for navigation; in S9a/b for answer writes), that route will call `sessions.start_or_resume(conn, clinician_id, pid, arm=..., config_hash=...)` and `arm_assignments.assign_or_lookup(conn, clinician_id, pid, ...)`. S6 stops short of this lazy bootstrap (S9a/b own those calls); the schema is in place, the DAOs exist and are tested, but no S6 route invokes them.

Login flow in ASCII:

```
    POST /login                                 GET /  or  /patient/...
        │                                            │
        ▼                                            ▼
   lookup_or_create  ─▶ INSERT-OR-IGNORE      _require_clinician(request)
   (uses cache)            into clinicians       ├─ no cookie OR not in cache:
        │                  + adds to cache       │     HX-Redirect or 303 → /login
        ▼                                        └─ in cache:
   events.append(                                      yield clinician_id
     session_id=NULL,                                  ├─ S6: render page
     kind="clinician.login")                           └─ S9a/b: also writes answers/events
        │                                                   └─ at first /patient visit,
        ▼                                                       sessions.start_or_resume
   write_counter += 1                                           + arm_assignments.assign_or_lookup
        │                                                           (deferred from S6)
        ▼
   set_cookie + 303 → /
```

### 7.4 Cookie shape

- Name: `ehrsim_clinician_id`
- Value: the 16-hex-char `clinician_id` (the SHA256-truncated pseudonym, NOT the raw name)
- Flags: `HttpOnly; SameSite=Strict; Path=/`
- No `Secure` (deployments are localhost-only per design doc)
- No signing/HMAC (local-only threat model). The cookie-tamper failure mode (a clinician manually setting the cookie to a 16-hex string that doesn't exist in `clinicians`) is caught by the cache lookup in `_require_clinician` → 303 → `/login` ([review-fix R11]).
- Max-Age: 30 days (a pilot session may span days; the cookie outlasts the browser process)

Tested by 3 cases in test_login.py covering happy-path round-trip, protected-route redirect (no cookie), and unknown-clinician_id cookie redirect.

### 7.5 Name normalization

```python
# db/clinicians.py (excerpt)
def _normalize(raw_name: str) -> str:
    return " ".join(raw_name.casefold().split())  # collapse whitespace + casefold
```

Tested by `test_clinicians_lookup_or_create_normalizes` against:
- `"Dr. Smith"`, `"DR. SMITH"`, `"  dr.   smith  "`, `"dr. smith"` → all resolve to the same `clinician_id`.
- `"Dr. Smyth"` → different `clinician_id`.
- `clinician_id` is stable across Python sessions (deterministic SHA256 truncation).

`clinician_id = sha256(name_normalized.encode("utf-8")).hexdigest()[:16]` — 16 hex chars = 64 bits, plenty of collision headroom for the realistic clinician-population scale (≤1000 names per pilot). The pseudonym separation between `name_normalized` (in DB) and `clinician_id` (in CSV exports per S9c) lets the IRB/keyfile policy from D9 land cleanly later.

---

## 8. CLI surface (Typer additions)

```
$ uv run ehr-simulator --help
Usage: ehr-simulator [OPTIONS] COMMAND [ARGS]...

Commands:
  serve              Run the FastAPI server via uvicorn.
  validate-config    Validate study_config.yaml + questions.yaml shape.
  validate-adapter   Resolve a study_config.yaml's dataset and try to load it.
  preflight          Walk every (patient_id, timepoint) headlessly.
  preview            Render a single patient's per-timepoint summary.
  migrate            [NEW in S6] Apply all unapplied DB migrations.
  backup             [NEW in S6] Snapshot the SQLite DB to a backup directory.
```

### 8.1 `serve` extensions

```
ehr-simulator serve [--config STUDY] [--questions QS] [--db-path PATH] [--backup-dir DIR] [--host H] [--port P] [--reload]
```

`--db-path` and `--backup-dir` are optional. Resolution precedence per §6.3 + per-flag override. The `--reload` + `--config` warning from S5 is unchanged. Without `--db-path`, the default is `./data/ehr_simulator.db` (parent dir auto-created at lifespan time).

### 8.2 `migrate`

```
ehr-simulator migrate [--db-path PATH]
```

Runs `apply_migrations(connect(db_path))`, then `PRAGMA wal_checkpoint(TRUNCATE)` so the bare `.db` file is a complete snapshot (no `-wal` / `-shm` sidecars needed for an `rsync` or `cp` backup — see [review-fix R14]). Exits 0 either way (success on apply, success on no-op). Stdout: `Applied migrations: [1]` or `No migrations to apply.`. Tested by 2 cases (forward + idempotent re-run).

### 8.3 `backup`

```
ehr-simulator backup [--db-path PATH] [--backup-dir DIR]
```

Runs `create_backup(db_path, backup_dir)`. Exits 0 on success with stdout: `Backup written to: <dest>`. On error (e.g., DB doesn't exist), exits 1 with stderr remediation. Tested by 1 happy-path test in test_cli.py + 5 backup-mechanic tests in test_db_backup.py.

---

## 9. Test inventory (target ≥10 from ROADMAP; final = 36 new + 9 carryover = 45 new test functions)

Numbered to match commits in §11. Following the S5 parametrization convention: same-validator-different-fixture cases collapse into `@pytest.mark.parametrize` blocks; CRUD shapes per table stay discrete because each has a different schema surface.

### `tests/test_db.py` (migrations + connection + per-table CRUD)

#### Connection + PRAGMAs (3)

1. **`test_connect_applies_pragmas`** — open a fresh `connect(tmp_db_path)`; assert `PRAGMA journal_mode` returns `wal`, `PRAGMA synchronous` returns `1`, `PRAGMA foreign_keys` returns `1`.
2. **`test_connect_check_same_thread_false`** — attempt a SELECT from a different thread against the same connection; assert no `ProgrammingError` raised.
3. **`test_resolve_db_path_precedence[ids=...]`** — `@pytest.mark.parametrize` over `["cli_override", "env_var", "study_config", "default"]`. Each case sets a different precedence level and asserts the resolved path matches.

#### Migrations runner (3 + 1 drift + 1 partial-apply NEW)

4. **`test_apply_migrations_forward`** — fresh DB; `apply_migrations(conn)` returns `[1]`; `schema_migrations` table has one row with `version=1, name="initial"`.
4b. **`test_apply_migrations_recovers_from_partial_apply`** [NEW — review-fix R2] — pre-create only `clinicians` table on the DB (simulating a mid-DDL crash that didn't reach `INSERT INTO schema_migrations`). Call `apply_migrations(conn)`; assert it returns `[1]` (re-runs migration 1 cleanly because of `IF NOT EXISTS`); assert all 7 tables now exist + one `schema_migrations` row.
5. **`test_apply_migrations_idempotent`** — call `apply_migrations(conn)` twice; second call returns `[]`; `schema_migrations` row count stays at 1; `event_kind="db.migrate.noop"` event fires (captured via `structlog.testing.capture_logs()`).
6. **`test_post_migration_schema_matches_fixture`** — apply migrations; query `sqlite_master` for table + index DDL sorted by name; compare against `tests/fixtures/db/migration_001_expected_schema.sql`.
7. **`test_migrations_constant_shape`** — assert `MIGRATIONS` is a tuple of `Migration` namedtuples with monotonically-increasing versions starting at 1, no gaps.

#### Per-table CRUD (11 — one per table + 3 NEW for empty-name reject, events.session_id NULL, ingestion_issues boot_id)

8. **`test_clinicians_lookup_or_create_normalizes`** — `lookup_or_create(conn, "Dr. Smith")` and `lookup_or_create(conn, "  DR.   SMITH  ")` return the same `clinician_id`; `clinician_id` length is 16 hex chars; one row in `clinicians`. Different name → different `clinician_id`.
8b. **`test_clinicians_lookup_or_create_rejects_empty_name`** [NEW] — `lookup_or_create(conn, "")` and `lookup_or_create(conn, "   ")` both raise `ValueError`.
9. **`test_sessions_start_or_resume_creates_then_resumes`** — first call creates a row; second call with same `(clinician_id, patient_id)` returns the same `session_id` (because `ended_at IS NULL`); after manually setting `ended_at`, third call creates a NEW `session_id`.
10. **`test_arm_assignments_phase1_stub_locks_no_ai`** — `assign_or_lookup(...)` returns `("no_ai", "phase1_stub")`; second call for same `(clinician_id, patient_id)` returns the same tuple even after monkey-patching the stub function (proves the row, not the function, is the source of truth).
11. **`test_arm_assignments_existing_row_not_rewritten[stub_then_randomized]`** — REGRESSION. Insert a `phase1_stub` row; mock-swap the assigner to a randomized one (S11 simulation); call `assign_or_lookup` again; assert the row's `arm_source` stays `"phase1_stub"` and `seed` stays NULL.
12. **`test_answers_upsert_idempotent_for_double_submit`** — REGRESSION. Two consecutive `upsert(...)` calls with identical `(clinician_id, patient_id, timepoint, question_id)` and different `value` produce 1 row; the row's `value` is the second call's value; `ts_recorded` reflects the second call. The network-retry-double-submit failure mode from S6 ROADMAP entry. Non-negotiable test.
13. **`test_answers_upsert_idempotent_for_float_timepoint`** — `upsert(..., timepoint=60.0)` and `upsert(..., timepoint=60.0)` against a DB where `timepoint` is REAL produce 1 row (locks the float-equality safety from §4 implementation note).
14. **`test_events_append_returns_autoincrement_id`** — three consecutive `append(...)` calls return `event_id=1, 2, 3`; `payload_json` is sorted-key JSON; `server_ts` is set; `client_ts` is NULL when not provided.
14b. **`test_events_append_with_null_session_id`** [NEW — review-fix R1] — `append(conn, session_id=None, kind="clinician.login", ...)` succeeds, returns an event_id, the row's `session_id` is NULL, the row is queryable via `SELECT * FROM events WHERE session_id IS NULL`.
15. **`test_ingestion_issues_record_batch_inserts_one_row_per_issue`** — pass 3 `IngestionIssue` instances + a `boot_id`; assert 3 rows; `dataset` column matches; `boot_id` matches; `loaded_at` set; assert all 3 rows came from the same transaction (record before vs after row count delta == 3 with one INSERT round-trip via executemany).
15b. **`test_ingestion_issues_boot_id_distinct_across_boots`** [NEW — review-fix R5] — record_batch with `boot_id="A"` for 2 issues, then `boot_id="B"` for 2 issues; assert 4 total rows; assert 2 rows per boot_id; `SELECT DISTINCT boot_id FROM ingestion_issues` returns exactly 2 rows.

#### FK + config_hash (2)

17. **`test_foreign_key_constraint_enforced_for_events_session_id`** — try `events.append(session_id="bogus_uuid", ...)`; assert `DbError` raised (wrapping `sqlite3.IntegrityError`). Confirm the `session_id=None` path in test #14b does NOT raise (different code path).
18. **`test_compute_config_hash_round_trips_through_db`** — compute hash from S5 fixtures via the new `compute_config_hash_from_models(study, questions)`; assert it equals the path-based `compute_config_hash(study_path, questions_path)` (proves the refactor preserves semantics — review-fix R3); insert into `answers.config_hash` via `upsert`; read back; assert identical 64-char hex; AND assert it changes if either YAML changes.

### `tests/test_db_backup.py` (5)

19. **`test_backup_creates_file_in_dest_dir`** — write some rows to a DB; call `create_backup(db_path, backup_dir)`; assert exactly one `.db` file lands in `backup_dir`; filename matches the `ehr_simulator_<utc_iso>.db` pattern.
20. **`test_backup_dest_is_readable_with_data_intact`** — call `create_backup(...)`; open the backup file; assert it has the same row count as the source for `clinicians` + `answers`.
20a. **`test_backup_skipped_when_write_counter_zero`** [NEW — review-fix R8] — boot a TestClient with no writes; trigger lifespan shutdown (exit the `with TestClient(...)` block); assert `backup_dir` is empty; assert `event_kind="db.backup.skipped"` event fired.
20b. **`test_backup_runs_when_write_counter_positive`** [NEW — review-fix R8] — boot a TestClient; POST to `/login` (increments write_counter via the events.append + login row); shutdown; assert one `.db` file in `backup_dir`; assert `event_kind="db.backup.ok"` event fired.
21. **`test_backup_creates_dest_dir_if_missing`** — point `backup_dir` at a path that doesn't exist; assert `create_backup(...)` creates the dir + writes the file (no `FileNotFoundError`).

### `tests/test_login.py` (8)

22. **`test_get_login_renders_form`** — `client.get("/login")` returns 200 with `<form` and `<input name="clinician_name"` in the body.
23. **`test_post_login_creates_clinician_and_sets_cookie_and_redirects`** — POST `/login` with `clinician_name="Dr. Smith"`; assert response is 303 → `/`; `Set-Cookie: ehrsim_clinician_id=<16-hex>; HttpOnly; SameSite=Strict; Path=/`; the `clinicians` table has 1 row with normalized name; one `events` row with `kind="clinician.login"` AND `session_id IS NULL` exists.
24. **`test_post_login_normalizes_name`** — POST with `"  DR. SMITH  "` and a separate POST with `"dr. smith"`; assert both responses' `Set-Cookie` carry the same `clinician_id`; `clinicians` table has 1 row, not 2.
24b. **`test_post_login_empty_name_returns_400`** [NEW] — POST with `clinician_name=""` and `clinician_name="   "`; assert both return 400 with `Name required.` in the body; `clinicians` table stays empty.
25. **`test_post_logout_clears_cookie_and_redirects`** — start with the login cookie set; POST `/logout`; assert 303 → `/login`; `Set-Cookie` clears `ehrsim_clinician_id` (Max-Age=0).
26. **`test_protected_route_redirects_to_login_when_no_cookie`** — `client.get("/")` with no cookies; assert 303 → `/login`; same for `client.get("/patient/synth_001/timepoint/0")`.
26b. **`test_protected_route_htmx_uses_hx_redirect`** [NEW — review-fix R10] — `client.get("/patient/synth_001/timepoint/0", headers={"HX-Request": "true"})` with no cookies; assert response is 200, `HX-Redirect: /login` header is set, body is empty.
26c. **`test_protected_route_unknown_clinician_id_redirects`** [NEW — review-fix R11] — set the cookie to a 16-hex string that's not in `clinicians`; `client.get("/")`; assert 303 → `/login` (cache lookup fails).
27. **`test_protected_route_succeeds_with_logged_in_client`** — uses `logged_in_client` fixture; `client.get("/")` returns 200; `clinician_id` ContextVar is bound during request handling.

### `tests/test_app.py` (+3 carryover extensions)

28. **`test_lifespan_wires_db_and_runs_migrations`** — boot a `TestClient(create_app(...))`; assert `app.state.db` exists, `schema_migrations` row exists, `event_kind="db.ready"` event fired with the resolved DB path.
29. **`test_lifespan_records_ingestion_issues_when_dataset_carries_them`** [REVISED — review-fix R6] — fake dataset with `.issues = [IngestionIssue("synth", "p1", 7, "bad")]`; loader returns it; boot app; assert `ingestion_issues` table has 1 row with matching values AND a `boot_id` matching `app.state.boot_id`; `event_kind="db.ingestion_issues.recorded"` event fired with `count=1`.
29b. **`test_lifespan_populates_known_clinicians_cache`** [NEW — review-fix R11] — pre-seed the test DB with 2 clinicians rows (use `tmp_db_path` + direct SQL); boot app pointed at that DB; assert `app.state.known_clinicians` is a set of 2 strings matching the seeded ids.
29c. **`test_lifespan_handles_non_adapter_exceptions`** [NEW — review-fix R9] — provide a loader that raises `FileNotFoundError`; assert the app boot logs `event_kind="app.boot.failed"` and raises `SystemExit(1)`; assert no DB file is created at `db_path`.

### `tests/test_cli.py` (+3 carryover extensions)

30. **`test_cli_migrate_forward_then_idempotent`** — `migrate --db-path tmp_db`; exit 0 + stdout `Applied migrations: [1]`. Re-run: exit 0 + stdout `No migrations to apply.`. Inspect the DB: `PRAGMA wal_checkpoint(PASSIVE)` after the migrate run shows zero un-checkpointed frames (proves the TRUNCATE checkpoint ran — review-fix R14).
31. **`test_cli_backup_creates_file`** — `migrate` first, then `backup --db-path tmp_db --backup-dir tmp_backup`; exit 0 + stdout `Backup written to: <path>`; assert the file exists on disk.
32. **`test_cli_serve_db_path_passes_through_to_create_app`** — `serve --db-path /tmp/X.db`; monkeypatch `uvicorn.run`; capture the `create_app` kwargs; assert `db_path=Path("/tmp/X.db")`.

### `tests/test_config.py` (+2 carryover extensions)

33. **`test_study_config_resolves_db_path_against_yaml_dir`** — write a study YAML at `tmp_path/study.yaml` with `db_path: data/local.db`; assert `study.db_path == tmp_path / "data" / "local.db"`.
33b. **`test_study_config_rejects_db_path_traversal`** [NEW — review-fix R13] — write a study YAML with `db_path: ../../etc/passwd.db`; assert `load_study_config(...)` raises `ConfigError` mentioning the offending path.

### E2E (1 NEW)

34. **`tests/test_e2e_login_walk.py::test_login_to_patient_walk`** [NEW] — Playwright walk: GET `/login` → submit form with `clinician_name="Dr. Test"` → land on `/` → click first patient → reach `/patient/<pid>/timepoint/0`. Asserts the `_chrome_*.html` "Logged in as Dr. Test · Logout" stripe is rendered. Marker `@pytest.mark.e2e`.

**Total: 36 new test functions in NEW test files (test_db.py: 21, test_db_backup.py: 5, test_login.py: 9, test_e2e_login_walk.py: 1) + 9 carryover extensions in existing files (test_app.py: 4, test_cli.py: 3, test_config.py: 2) = 45 new test functions; case count ~50 once parametrization expands.** ROADMAP bar (≥10) cleared by 4.5×. Project total after S6: **132 + 45 = 177 test functions** (excluding `e2e` and `real_data` markers from the headline count).

---

## 10. CI changes (`.github/workflows/ci.yml`)

Add **after** the existing `CLI smoke` step:

```yaml
- name: DB smoke
  run: |
    rm -rf /tmp/ci_db_smoke /tmp/ci_db_backups
    mkdir -p /tmp/ci_db_smoke
    uv run ehr-simulator migrate --db-path /tmp/ci_db_smoke/test.db
    uv run ehr-simulator migrate --db-path /tmp/ci_db_smoke/test.db   # idempotent
    uv run ehr-simulator backup \
      --db-path /tmp/ci_db_smoke/test.db \
      --backup-dir /tmp/ci_db_backups
    test "$(ls /tmp/ci_db_backups/*.db | wc -l)" -ge 1
```

This catches the "installed entry point works post-`uv sync`" failure mode that pytest's in-process tests can miss. Runs on Python 3.11 + 3.12 (matrix-inherited).

---

## 11. Commit discipline (target ~7-8 commits, ~2 days)

| # | Commit | Files |
|---|---|---|
| 1 | `session-06 commit 1: db package scaffold + connection + migrations runner + initial schema (with IF NOT EXISTS)` | `src/ehr_simulator/db/{__init__.py,connection.py,exceptions.py,migrations.py}`; `tests/conftest.py` (+`tmp_db_path`, +`tmp_backup_dir`, +`db`); `tests/fixtures/db/migration_001_expected_schema.sql`; `tests/test_db.py` (tests #1-#7 + #4b); `.gitignore` (+`data/`). |
| 2 | `session-06 commit 2: per-table DAO modules (clinicians, sessions, arm_assignments, answers, events, ingestion_issues) + boot_id + write_counter` | `src/ehr_simulator/db/{clinicians.py,sessions.py,arm_assignments.py,answers.py,events.py,ingestion_issues.py}`; `tests/test_db.py` (+tests #8, #8b, #9, #10, #11, #12, #13, #14, #14b, #15, #15b, #17, #18). |
| 3 | `session-06 commit 3: backup module + CLI command + lifespan shutdown hook (write-counter gated)` | `src/ehr_simulator/db/backup.py`; `src/ehr_simulator/cli.py` (+`backup` command); `src/ehr_simulator/web/app.py` (shutdown branch + write-counter gate); `tests/test_db_backup.py` (tests #19-#21 + #20a, #20b); `tests/test_cli.py` (+test #31). |
| 4 | `session-06 commit 4: app.py lifespan wires db + ingestion_issues + migrate CLI + non-AdapterError handling + known_clinicians cache + compute_config_hash_from_models` | `src/ehr_simulator/web/app.py` (full lifespan growth + AdapterError + Exception catch + known_clinicians cache); `src/ehr_simulator/cli.py` (+`migrate` command + post-migrate WAL checkpoint); `src/ehr_simulator/config/study.py` (+`db_path` field + traversal guard); `src/ehr_simulator/config/loader.py` (+`compute_config_hash_from_models`); `src/ehr_simulator/db/connection.py` (`resolve_db_path` + `_db_path_traversal_guard`); `tests/test_app.py` (+tests #28, #29, #29b, #29c); `tests/test_cli.py` (+tests #30, #32); `tests/test_config.py` (+tests #33, #33b). |
| 5a | `session-06 commit 5a: cookies + _require_clinician (HTMX-aware) + protected-route preamble + protected-route tests` | `src/ehr_simulator/db/cookies.py`; `src/ehr_simulator/web/routes.py` (+`_require_clinician` preamble on `/` and `/patient/...`); `tests/conftest.py` (+`logged_in_client` fixture + client db-path threading); `tests/test_login.py` (tests #26, #26b, #26c, #27). |
| 5b | `session-06 commit 5b: /login + /logout routes + login.html template + chrome stripe + login tests` | `src/ehr_simulator/web/routes.py` (+/login GET/POST, +/logout POST); `src/ehr_simulator/web/templates/login.html`; `src/ehr_simulator/web/templates/_login_stripe.html` + `_chrome_dense.html` + `_chrome_epic.html` (logged-in stripe); `tests/test_login.py` (tests #22, #23, #24, #24b, #25); `tests/test_e2e_login_walk.py` (test #34). |
| 6 | `session-06 commit 6: CI smoke + TODOS.md updates` | `.github/workflows/ci.yml` (+`DB smoke` step); `TODOS.md` (strike `D10 SQLite backup cadence` from policy commitments; strike the plan-eng-review residual on S6 backup; add new TODOs surfaced by S6 — see §14). |
| 7 | `session-06 commit 7: ruff/format pass + final docs` | ruff check + ruff format pass on the whole tree. |

Commit 5 is split into 5a + 5b up front (it would otherwise blow past ~500 lines); 5a lands the protected-route preamble first so 5b's login routes can land in a green tree.

---

## 12. Acceptance criteria (how you know S6 is done)

Every item is a check a reviewer can run.

- [ ] `uv sync` clean (no new deps to install).
- [ ] `uv run pytest` green; **36 new test functions in NEW test files + 9 carryover extensions in existing files = 45 new tests total**; project-wide ~177 test functions across S1-S6.
- [ ] All previous tests stay green throughout (132 from S1-S5 unchanged in semantics; the conftest `client` fixture update to pre-set the login cookie + thread `db_path=tmp_db_path` is structurally compatible with S2+ route tests).
- [ ] `uv run ehr-simulator migrate --db-path /tmp/X.db` exits 0; re-running exits 0 with `No migrations to apply.`
- [ ] `uv run ehr-simulator backup --db-path /tmp/X.db --backup-dir /tmp/X_backups` exits 0; one `.db` file in `/tmp/X_backups/` matching `ehr_simulator_*.db`.
- [ ] `uv run ehr-simulator serve --db-path /tmp/srv.db` boots successfully; `sqlite3 /tmp/srv.db .schema` shows all 7 tables + 5 indexes (the new `ix_ingestion_issues_boot_id` included); `PRAGMA journal_mode` returns `wal`.
- [ ] `curl -X POST -d "clinician_name=Dr. Smith" http://localhost:8000/login` returns 303 → `/`; the response sets the `ehrsim_clinician_id` cookie; `sqlite3 /tmp/srv.db "SELECT name_normalized FROM clinicians"` shows `dr. smith`.
- [ ] `curl -L http://localhost:8000/` (no cookie) follows a 303 to `/login`.
- [ ] `curl -i -H "HX-Request: true" http://localhost:8000/` (no cookie) returns 200 + `HX-Redirect: /login` header (NOT a 303).
- [ ] `curl -L --cookie "ehrsim_clinician_id=$(sqlite3 /tmp/srv.db 'SELECT clinician_id FROM clinicians LIMIT 1')" http://localhost:8000/` returns 200 (the patient list).
- [ ] `curl -L --cookie "ehrsim_clinician_id=ffffffffffffffff" http://localhost:8000/` (bogus 16-hex id not in clinicians) follows a 303 to `/login`.
- [ ] Submitting the same `clinician_name` twice with different whitespace/case results in one row in `clinicians`, not two.
- [ ] `sqlite3 /tmp/srv.db "SELECT kind, session_id IS NULL, COUNT(*) FROM events GROUP BY kind, session_id IS NULL"` shows at least one `clinician.login` row with NULL session_id after one `/login` POST.
- [ ] **REGRESSION:** writing the same `(clinician_id, patient_id, timepoint, question_id)` cell twice via `answers.upsert(...)` produces 1 row, not 2 (covered by test #12).
- [ ] **REGRESSION:** simulating a partial migration (pre-create one of the new tables) followed by `apply_migrations(...)` still succeeds and leaves a single `schema_migrations` row (covered by test #4b).
- [ ] `compute_config_hash_from_models(study, questions) == compute_config_hash(study_path, questions_path)` for matching inputs (covered by test #18).
- [ ] Server graceful shutdown after `/login` POST produces a backup file in `data/backups/` with the current UTC ISO in its name; `db.backup.ok` event in the JSONL log.
- [ ] Server graceful shutdown WITHOUT any writes (boot, idle, Ctrl+C) produces NO backup file; `db.backup.skipped` event in the JSONL log.
- [ ] Server graceful shutdown with a read-only `backup_dir` produces a `db.backup.failed` event but does NOT prevent the process from exiting.
- [ ] Booting `serve` against a study YAML with `db_path: ../../tmp/x.db` exits with `ConfigError`.
- [ ] Booting `serve` against a dataset that raises `FileNotFoundError` logs `app.boot.failed` and exits 1; no DB file is created at `db_path`.
- [ ] `uv run ruff check .` and `uv run ruff format --check .` clean.
- [ ] CI passes on a PR opened against `main`, including the new `DB smoke` step on both Python 3.11 and 3.12.
- [ ] `uv run pytest -m real_data` still exits 0 (S3+S4 real-data smokes unchanged; the `ingestion_issues` table now records geneva/mimic issues at boot but the test runs against fresh tmp DBs).
- [ ] `uv run pytest -m e2e` exits 0 (S2 Playwright walk extended through `/login` per test #34).
- [ ] `app.state.db`, `app.state.boot_id`, `app.state.write_counter`, `app.state.known_clinicians` are all set after lifespan boot for both `create_app()` and `app_from_study_config(...)` paths.
- [ ] FK violations from inserting an `events` row with a bogus `session_id` raise `DbError` (test #17); `events.append(session_id=None, ...)` succeeds (test #14b).

---

## 13. Conventions

- `from __future__ import annotations` at the top of every new module.
- Module docstrings on every new module under `src/ehr_simulator/db/` and every new test file.
- Type hints on every public function. SQLite connection passed in by callers (no module-level singleton); `sqlite3.Row` row factory for ergonomic dict-like access.
- Pure functions in DAO modules; no classes. Mirrors the project's `load_synthetic` / `compute_config_hash` style.
- `Path` (not `str`) for every filesystem-bound argument across the new modules.
- Test names: `test_<subject>_<expected_behavior>`. Mirror S5/S4/S3 naming.
- DDL strings live in `db/migrations.py` as Python triple-quoted strings, not in `.sql` files. One source of truth, package-installable, no install-time file-discovery dance. **Every CREATE statement uses `IF NOT EXISTS`** for partial-apply recovery.
- Backup filenames: `ehr_simulator_<utc_iso>.db` where `<utc_iso>` is `datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")`. Sortable, no separators inside the timestamp.
- Cookie name: `ehrsim_clinician_id` (note the prefix; matches the project's deployment-isolation convention).
- `events.payload_json` always serialized via `json.dumps(payload, sort_keys=True, separators=(",", ":"))` — deterministic, compact, joinable across pilots.
- **Write-counter pattern:** every DAO that persists researcher-meaningful state (`answers.upsert`, `events.append`) increments `app.state.write_counter` after a successful INSERT. The shutdown-time backup gates on this counter — no writes, no backup. S9a/b/c add new producers via the same pattern.
- **Known-clinicians cache pattern:** `app.state.known_clinicians: set[str]` is populated at lifespan boot (`SELECT clinician_id FROM clinicians`). `_require_clinician` reads it; `lookup_or_create` writes to it. The cache is small (≤1000 ids per pilot) and read-only outside the login path.
- **Backup time vs uvicorn graceful-timeout:** at pilot scale the online-backup completes in <1s. For DBs >100 MB, set `--graceful-timeout 60` on the uvicorn invocation. Documented in v1.0 README.
- No comments that restate code (per repo `CLAUDE.md`). Only the cookie-not-signed and the events-session_id-nullable decisions get explanatory comments (load-bearing for security/integrity reviewers).

---

## 14. Open decisions deferred to later sessions

- **Answer capture POST endpoint** — deferred to S9a per ROADMAP. S6 ships `db.answers.upsert(...)` + the unique-constraint regression test; S9a wires the route + auto-save-on-blur.
- **`/advance` endpoint + question gating** — deferred to S9b. S6 leaves `sessions.ended_at` always NULL; S9b's `/advance` on the final timepoint will set it.
- **Session `ended_at` lifecycle** — deferred to S9b alongside `/advance`. S6 spec lists "set on final-timepoint advance" as the contract; S9b implements.
- **CSV export with cell-injection guard + pseudonymization keyfile** — deferred to S9c.
- **Phase-2 randomized arm assigner + seed strategy** — deferred to S11. S6 ships `arm_assignments.assign_or_lookup(...)` returning `("no_ai", "phase1_stub")` always; S11 swaps the body. The `seed` column stays NULL in S6. **NEW TODO:** S11 spec must define how `seed` is populated for `phase2_randomized` rows — candidates: (a) deterministic from `(study.seed, clinician_id, patient_id)`, (b) top-level `study.seed` field, (c) per-row uuid4. Test #11 only locks "existing rows are never rewritten"; reproducibility of new randomized rows is S11's design problem.
- **`events.kind` enumerated `Literal` type** — deferred to S9a when more producers exist. S6 ships free-text; S9a tightens.
- **Backup rotation / pruning** — punted to v1.0 prep. Revival criterion: when more than 50 backup files accumulate in a real pilot deployment.
- **Cookie signing / HMAC** — punted. Local-only threat model.
- **Auth beyond name-typed-into-form** — punted. Per design doc.
- **Session resume across browser restart** — partially handled (cookie has Max-Age=30 days).
- **Multi-DB / multi-study concurrent runs on one laptop** — punted.
- **README.md persistence paragraph update** — owned by `/document-release` post-ship.

---

## 15. What Session 6 does NOT lock

- Real-data UI experience under perf load — S8.
- Geneva AI predictions adapter — S7.
- Answer capture POST endpoint, question gating `/advance`, CSV export — S9a/b/c.
- Divergence view — S10.
- Phase-2 arm randomization + seed design — S11.
- Polished v1.0 release notebook + README quickstart — S12.
- DICOM rendering — punted (TODOS.md).
- Mobile/tablet layouts — punted (TODOS.md).
- Backup rotation policy — punted to v1.0 prep.
- Cookie signing — punted.
- `events.kind` type-narrowing — deferred to S9a.

---

## 16. What already exists (carried into S6)

- **`src/ehr_simulator/web/app.py`** — `create_app(*, log_dir, dataset_loader)` factory + `app_from_study_config(...)`. **Modified in S6:** `create_app` gains `db_path` + `backup_dir` kwargs; lifespan grows DB init + migrations + ingestion_issues batch + write-counter gated shutdown backup + non-AdapterError catch + known_clinicians cache.
- **`src/ehr_simulator/web/routes.py`** — patient/timepoint slicing routes from S2/S5. **Modified in S6:** `/` and `/patient/...` grow a one-line `_require_clinician(request)` preamble (HTMX-aware redirect).
- **`src/ehr_simulator/web/templates/_chrome_*.html`** — base templates. **Modified in S6:** logged-in stripe via `_login_stripe.html` partial.
- **`src/ehr_simulator/cli.py`** — Typer CLI from S5 with 5 commands. **Modified in S6:** `serve` gains `--db-path` + `--backup-dir`; `backup` and `migrate` commands added (total 7).
- **`src/ehr_simulator/cli_support.py`** — `build_dataset_loader`, `walk_preflight`, `render_preview`. **Unchanged in S6** (the originally-proposed `loader.issues` closure attribute is dropped — lifespan reads from `app.state.dataset.issues` directly).
- **`src/ehr_simulator/config/study.py`** — Pydantic `StudyConfig`. **Modified in S6:** +`db_path` field + traversal guard validator. Additive, no `schema_version` bump needed.
- **`src/ehr_simulator/config/loader.py`** — `compute_config_hash(study_path, questions_path)`. **Modified in S6:** add sibling `compute_config_hash_from_models(study, questions)`; the path version delegates to it.
- **`src/ehr_simulator/logging.py`** — structlog pipeline + 8 mandatory ContextVars. **Unchanged in S6**; the `clinician_id` ContextVar that's been bound to None forever finally has a real producer.
- **`src/ehr_simulator/ingestion/exceptions.py`** — `IngestionIssue` dataclass + `AdapterError`. **Unchanged in S6**; S6's `ingestion_issues.record_batch(...)` consumes `IngestionIssue` instances directly.
- **`src/ehr_simulator/ingestion/{geneva,mimic}.py`** — adapter signatures unchanged. The `dataset.issues` list (already populated in lenient mode) becomes the source for `ingestion_issues` rows.
- **`tests/conftest.py`** — `dataset`, `tmp_log_dir`, `client`, `geneva_fixture_dir`, `mimic_fixture_dir`, `study_fixture_dir` fixtures all reused. **Modified in S6:** +`tmp_db_path`, +`tmp_backup_dir`, +`db`, +`logged_in_client`. The existing `client` fixture is updated to thread `db_path=tmp_db_path` + `backup_dir=tmp_backup_dir` (without this, every test pollutes the project's `data/` directory — review-fix R7).
- **`pyproject.toml` `[tool.pytest.ini_options]`** — `addopts` + `markers` unchanged.
- **`scripts/gen_data_contract.py` + `docs/data-contract.md`** — both unchanged in S6 (canonical pandera shapes are the in-memory data contract, distinct from the SQLite persistence contract).
- **`.github/workflows/ci.yml`** — extended with the `DB smoke` step.

---

## 17. Verification (end-to-end)

Run after the implementation lands to confirm the spec matches reality:

1. **Pytest end-to-end:**
   ```
   uv run pytest                # ~177 green (132 prior + 45 new)
   uv run pytest -m e2e         # S2 + S6 Playwright walks; green
   uv run pytest -m real_data   # S3+S4 real-data smokes green
   ```

2. **DB CLI smoke:**
   ```
   rm -rf /tmp/s6_smoke /tmp/s6_backups
   uv run ehr-simulator migrate --db-path /tmp/s6_smoke/x.db
   uv run ehr-simulator migrate --db-path /tmp/s6_smoke/x.db   # idempotent
   uv run ehr-simulator backup --db-path /tmp/s6_smoke/x.db --backup-dir /tmp/s6_backups
   ls /tmp/s6_backups/                                          # one .db file
   ```

3. **Schema introspection:**
   ```
   sqlite3 /tmp/s6_smoke/x.db "SELECT name FROM sqlite_master WHERE type IN ('table','index') ORDER BY name"
   #   → clinicians, sessions, arm_assignments, answers, events, ingestion_issues, schema_migrations,
   #     ix_answers_patient_clinician, ix_arm_clinician_patient, ix_events_patient_timepoint,
   #     ix_events_session_id, ix_ingestion_issues_boot_id
   sqlite3 /tmp/s6_smoke/x.db "PRAGMA journal_mode; PRAGMA synchronous; PRAGMA foreign_keys"
   #   → wal, 1, 1
   ```

4. **Live login + protected-route walk:**
   ```
   uv run ehr-simulator serve --db-path /tmp/s6_smoke/x.db &
   curl -i -X POST -d "clinician_name=Dr. Smith" http://localhost:8000/login
   #   → 303, Set-Cookie: ehrsim_clinician_id=<16-hex>; HttpOnly; SameSite=Strict; Path=/
   COOKIE="ehrsim_clinician_id=$(sqlite3 /tmp/s6_smoke/x.db 'SELECT clinician_id FROM clinicians LIMIT 1')"
   curl -i -L --cookie "$COOKIE" http://localhost:8000/
   #   → 200 with patient list
   curl -i -L http://localhost:8000/                       # no cookie
   #   → 303 → /login
   curl -i -H "HX-Request: true" http://localhost:8000/    # HTMX, no cookie
   #   → 200, HX-Redirect: /login
   sqlite3 /tmp/s6_smoke/x.db "SELECT clinician_id, name_normalized FROM clinicians"
   #   → one row; name_normalized is "dr. smith"
   sqlite3 /tmp/s6_smoke/x.db "SELECT kind, session_id IS NULL, count(*) FROM events GROUP BY kind, session_id IS NULL"
   #   → clinician.login | 1 | 1
   kill %1
   ls data/backups/   # one snapshot file written on graceful shutdown (write_counter > 0)
   ```

5. **Unique-constraint regression sanity check** (the load-bearing test #12):
   ```
   uv run python -c "
   import sqlite3, tempfile
   from pathlib import Path
   from ehr_simulator.db import connect, apply_migrations, answers, clinicians
   db = connect(Path(tempfile.mkstemp(suffix='.db')[1]))
   apply_migrations(db)
   cid = clinicians.lookup_or_create(db, 'Dr. Test')
   answers.upsert(db, clinician_id=cid, patient_id='p1', timepoint=60.0, question_id='q1', value='Yes', arm='no_ai', config_hash='h')
   answers.upsert(db, clinician_id=cid, patient_id='p1', timepoint=60.0, question_id='q1', value='No', arm='no_ai', config_hash='h')
   row_count = db.execute('SELECT COUNT(*) FROM answers').fetchone()[0]
   value = db.execute('SELECT value FROM answers').fetchone()[0]
   assert row_count == 1, f'expected 1 row, got {row_count}'
   assert value == 'No', f'expected No (latest write), got {value}'
   print(f'OK: {row_count} row, value={value}')
   "
   ```

If all five verification steps pass on a fresh clone post-`uv sync`, S6 is shipped.

---

## 18. Review history

This spec was reviewed by `/plan-eng-review` on 2026-05-10 (prior to commit 1). The following design decisions were made interactively with the user during that review and are now baked into the spec:

| Review-fix | Original design | Resolved design |
|---|---|---|
| R1 | Sentinel session row + non-null `events.session_id` | `events.session_id` is nullable; clinician-level events pass NULL |
| R2 | `executescript` inside `with conn:` (broken atomicity) | Every CREATE uses `IF NOT EXISTS`; partial-apply recovery test added |
| R3 | `compute_config_hash(study, questions)` (signature error) | Add `compute_config_hash_from_models(study, questions)`; path version delegates |
| R4 | `db_path` field on StudyConfig without validator update | `_resolve_relative_paths` learns about `db_path` |
| R5 | `ingestion_issues` re-records on every boot, no dedup | Add `boot_id` column + index; per-boot scoping |
| R6 | `loader.issues` closure attribute | Read from `app.state.dataset.issues` directly; `cli_support.py` unchanged |
| R7 | `client` fixture lacks `db_path` plumbing | `client` fixture passes `db_path=tmp_db_path` + `backup_dir=tmp_backup_dir` |
| R8 | Backup runs on every shutdown (clutter in dev) | Backup gated on `app.state.write_counter > 0` |
| R9 | Lifespan only catches `AdapterError` | Catches `Exception` too; logs `app.boot.failed`; exits 1 |
| R10 | `_require_clinician` returns 303 for HTMX swaps (silent failure) | HTMX-aware: returns `HX-Redirect` header for HX-Request, 303 otherwise |
| R11 | Cookie validation does DB lookup per protected request | `app.state.known_clinicians: set[str]` cache; zero DB cost |
| R12 | `sqlite3.Connection.backup()` may exceed uvicorn `--graceful-timeout` (5s default) on >100 MB DBs | Pilot scale (≤30 patients, ≤10 MB) is comfortably inside 5s. Documented in deliverable #12 + §13; v1.0 README will surface `--graceful-timeout 60` for larger pilots. TODOS.md captures the v1.0 README pass. |
| R13 | `db_path` allows `..` traversal via study YAML / env var | `_db_path_traversal_guard` rejects `..` and out-of-CWD absolute paths (CLI flag bypasses) |
| R14 | CLI `migrate` leaves un-checkpointed WAL frames | Post-migrate `PRAGMA wal_checkpoint(TRUNCATE)` |

These are not separate questions to revisit — the spec body is the source of truth. The table is for cross-referencing review notes against implementation.

All other forks (login event row dropped vs kept, db_path sandbox flavor, cookie validation flavor) were resolved as documented inline. No further user clarifications needed before implementation begins.

---

## Spec destination

This document lives at `specs/session-06-sqlite-persistence.md` (matches the `session-NN-name.md` convention from the roadmap working agreement). The original 2026-05-10 pre-review draft is overwritten in place.
