# Session 10 — Behavioral timing + rough divergence view

## 1. Goal

Session 10 must do three things:

1. record when a clinician enters and leaves each editable timepoint;
2. add derived wall-clock timing to `export-answers`;
3. generate a rough per-patient divergence figure for Phase-1 dogfooding.

This session is the behavioral-analysis bridge between S9 answer capture/export and S11 randomization.

Do not implement active-attention measurement, publication-quality figures, or Phase-2 statistics.

---

## 2. Locked decisions

### Timing is event-derived

Do not add start/end columns to `answers`.

Record explicit events and derive timing from them.

Add event kinds:

```text
timepoint.enter
timepoint.exit
```

Existing events remain unchanged.

Use `server_ts` as the authoritative timestamp. Do not derive research timing from browser clocks.

### Timing means wall-clock elapsed time

For one `(clinician_id, patient_id, timepoint)`:

```text
timepoint_started_at = first valid timepoint.enter
timepoint_ended_at   = first valid timepoint.exit after that enter
elapsed_seconds      = ended - started
```

`elapsed_seconds` is wall-clock elapsed time, not active dwell time.

Do not label it `dwell_time`.

Do not attempt to subtract tab-background time, inactivity, refreshes, or pauses.

### Export remains one row per timepoint

Extend the S9c answers CSV. Do not create a second timing CSV.

New fixed-column order:

```text
patient_id
clinician_id
t_index
timepoint_minutes
timepoint_started_at
timepoint_ended_at
elapsed_seconds
arm
completed_at
config_hash
<questions in questions.yaml order>
```

---

## 3. Event capture

### `timepoint.enter`

Emit when the application successfully presents the clinician's **current editable frontier timepoint**.

Required fields use the existing `events` schema:

```text
session_id
clinician_id
patient_id
timepoint
kind = "timepoint.enter"
server_ts
```

Payload:

```json
{"t_index": 1}
```

Rules:

* emit only after patient/timepoint access checks succeed;
* emit only for the current editable frontier;
* do not emit for blocked future navigation;
* do not emit for viewing frozen past timepoints;
* refresh/resume may emit another `timepoint.enter`;
* duplicate enter events are valid append-only behavioral data.

### `timepoint.exit`

Emit when the clinician successfully leaves the current frontier through progression.

Payload:

```json
{"t_index": 1, "reason": "advance"}
```

or:

```json
{"t_index": 2, "reason": "finish"}
```

Rules:

* emit only after all gating checks succeed;
* blocked advance must not emit `timepoint.exit`;
* final patient completion must emit `timepoint.exit` with `reason="finish"`;
* preserve existing `advance.ok`, `advance.blocked`, `session.start`, `session.end`, and answer events.

Progress mutation and `timepoint.exit` must occur in the same successful application operation. Do not emit a successful exit if progression failed.

---

## 4. Timing derivation

Add:

```text
src/ehr_simulator/timing.py
```

Public API:

```python
@dataclass(frozen=True)
class TimepointTiming:
    clinician_id: str
    patient_id: str
    timepoint: float
    started_at: datetime | None
    ended_at: datetime | None
    elapsed_seconds: int | None


class TimingError(ValueError):
    pass


def derive_timepoint_timings(
    events,
    *,
    clinician_id: str,
    patient_id: str,
) -> dict[float, TimepointTiming]:
    ...
```

Derivation rules:

1. group by clinician, patient and timepoint;
2. sort by `server_ts`, then deterministic event id/order;
3. `started_at` is the earliest `timepoint.enter`;
4. `ended_at` is the earliest `timepoint.exit` at or after `started_at`;
5. later enters/exits do not replace the first completed interval;
6. if no enter exists, all timing fields are blank;
7. if enter exists but exit does not, export `started_at` and leave end/elapsed blank;
8. if an exit precedes every enter, ignore that exit;
9. if a selected end timestamp is earlier than start, raise `TimingError`;
10. never infer missing timestamps from answer `ts_recorded`, progress, or session timestamps.

Legacy S9 data without `timepoint.enter` remains exportable with blank timing columns.

Timestamp serialization:

```text
YYYY-MM-DD HH:MM:SS
```

`elapsed_seconds` is an integer number of seconds.

---

## 5. `export-answers` changes

`build_export()` must fetch the relevant event data inside the **same explicit read transaction** already used by S9c.

The snapshot must therefore cover:

```text
answers
progress
arm_assignments
events used for timing
optional clinician keyfile lookup
```

Extend every emitted row with:

```text
timepoint_started_at
timepoint_ended_at
elapsed_seconds
```

Examples:

Completed interval:

```text
2026-09-18 14:01:12,2026-09-18 14:02:45,93
```

Current incomplete frontier:

```text
2026-09-18 14:03:01,,
```

Legacy/no timing information:

```text
,,
```

All S9c guarantees remain mandatory:

* deterministic row ordering;
* formula-injection guard on every CSV cell;
* no clinician login-name mapping in answers CSV;
* strict persisted-answer decoding;
* config-drift refusal;
* arm-integrity refusal;
* staged output installation;
* read-only SQLite access.

Update CI's literal expected header.

---

## 6. Rough divergence view

Add:

```text
src/ehr_simulator/divergence.py
```

Use **plotnine**. Add no second charting stack.

CLI:

```text
ehr-simulator divergence-view STUDY_CONFIG QUESTIONS
    --db-path P
    --patient PATIENT_ID
    --out FILE.svg
```

The command opens the DB read-only and refuses stale schema/config incompatibility.

One invocation renders one patient.

### Figure structure

The SVG has:

1. time on the x-axis using `timepoint_minutes`;
2. one answer panel per configured question;
3. one timing panel at the bottom;
4. an annotation at each timepoint summarizing newly visible data.

Do not create one global "divergence score".

### Numeric questions

For:

```text
likert
probability-0-100
```

Plot:

* individual clinician observations;
* separate arm series;
* per-arm median at each timepoint.

### Categorical questions

For `categorical`, calculate for every configured option:

```text
count(option) / answered responses
```

by:

```text
patient × timepoint × arm
```

Plot option proportions.

Do not impose numeric ordering on categorical options.

### Multi-select questions

For every configured option calculate:

```text
count(responses containing option) / answered responses
```

by patient, timepoint and arm.

Each option is independent; proportions need not sum to 1.

### Free text

Never place free-text contents in the divergence figure.

Display only the number of non-empty answers by arm/timepoint.

### Timing panel

Plot median non-null `elapsed_seconds` by arm and timepoint plus individual observations.

Label the axis:

```text
wall-clock elapsed seconds
```

Never call this active time or dwell time.

### One-arm data

The figure must still render when only one arm is present.

Do not fabricate a missing comparison.

Include the textual annotation:

```text
Single-arm data at this patient/timepoint; no between-arm comparison available.
```

S11 will later provide randomized arm assignment.

---

## 7. Newly-visible-data annotations

The divergence command must load the configured dataset through the existing adapter and determine what became newly visible at each study timepoint.

For timepoint `t_i`, define the newly visible interval as:

```text
i == 0: data visible at or before t_0
i > 0:  t_(i-1) < data_time <= t_i
```

Summarize by canonical/source category and count only.

Example annotation:

```text
new: vitals 6 · labs 4 · AI 1
```

Static admission data is shown at the first timepoint as:

```text
admission
```

Do not print raw clinical values in annotations.

The annotation must reflect the same `data <= t` visibility rule used by the UI.

---

## 8. CLI exit-code regression

Fix the discovered CLI entry-point bug in this session.

The installed `ehr-simulator` command must propagate command failure status to the operating system.

A refusal that raises/returns exit code `1` must result in:

```bash
ehr-simulator ...
echo $?
# 1
```

Do not consider `CliRunner` coverage sufficient.

Add a subprocess regression test against the installed console entry point.

---

## 9. Deliverables

```text
src/ehr_simulator/timing.py                 NEW
src/ehr_simulator/divergence.py             NEW
src/ehr_simulator/export.py                 MOD
src/ehr_simulator/cli.py                    MOD
src/ehr_simulator/db/events.py              MOD if fetch API needed
src/ehr_simulator/web/...                   MOD event capture
tests/test_timing.py                         NEW
tests/test_divergence.py                     NEW
tests/test_export.py                         MOD
tests/test_cli.py                            MOD
.github/workflows/ci.yml                     MOD export header
specs/ROADMAP.md                             MOD S10 shipped wording after implementation
```

No database migration is required unless the existing events schema cannot store the specified event kinds/payloads.

---

## 10. Test inventory

Minimum acceptance inventory: **18 behavioral tests**.

### Timing/event capture

1. editable frontier render emits `timepoint.enter`;
2. blocked future navigation does not emit enter;
3. frozen past-timepoint view does not emit enter;
4. successful advance emits `timepoint.exit(reason=advance)`;
5. blocked advance emits no exit;
6. final completion emits `timepoint.exit(reason=finish)`;
7. duplicate enters derive the earliest start;
8. first exit after start is selected;
9. incomplete frontier has start but no end/elapsed;
10. legacy eventless row derives blank timing.

### Export

11. new timing columns appear in exact documented order;
12. completed timing round-trips with integer elapsed seconds;
13. event reads occur inside the existing explicit SQLite snapshot;
14. S9c privacy/formula/config/arm guarantees remain green.

### Divergence

15. numeric question rendering works;
16. categorical/multi-select proportions are correct;
17. free-text values never appear in SVG;
18. one-arm data renders without failure.

### Required regression tests

Also add:

* installed CLI refusal returns process status `1`;
* data-visibility annotation never includes rows after the current timepoint.

Do not use an exact total pytest collection count as an acceptance condition.

---

## 11. Manual acceptance

Using a disposable pilot DB:

1. open `synth_001` t=0;
2. wait several seconds;
3. advance to t=60;
4. wait several seconds;
5. finish the patient;
6. export while the server remains running.

Verify the CSV contains non-empty start/end/elapsed values for exited timepoints and a start-only value for any currently open frontier.

Then run:

```text
ehr-simulator divergence-view \
    configs/example_config.yaml \
    configs/example_questions.yaml \
    --db-path <pilot.db> \
    --patient synth_001 \
    --out /tmp/synth_001-divergence.svg
```

Verify:

* SVG opens successfully;
* answer panels correspond to recorded answers;
* timing panel corresponds to exported elapsed seconds;
* free-text contents are absent;
* newly visible data annotations respect the timepoint boundary;
* one-arm pilot data renders without inventing an AI/no-AI comparison.

Finally deliberately run a failing CLI command and verify the shell exit status is `1`.

---

## 12. Acceptance criteria

Session 10 is complete when:

```text
uv run pytest
uv run pytest -m e2e
uv run ruff check .
uv run ruff format --check .
```

all pass, and manual acceptance succeeds.

The implementation must leave these semantics locked:

```text
answers      = research responses
events       = behavioral history
timing       = deterministic derivation from events
export CSV   = analysis-ready answers + derived wall-clock timing
divergence   = descriptive dogfood view, not a statistical endpoint
```

Active-attention measurement, browser visibility tracking, formal AI-vs-no-AI inference, randomized assignment, and publication-quality figures remain out of scope.

