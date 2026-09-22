# Session 11 — Phase 2 Implementation Roadmap

## 1. Purpose

Session 11 implements the Phase 2 study framework defined by the locked Phase 2 gate.

The original Session 11 roadmap entry described only pairwise AI versus no AI randomisation. That scope has been superseded by the Phase 2 gate and the Session 11 implementation checklist.

Session 11 is therefore split into small, independently reviewable subsessions:

**S11a → S11o**

Each subsession should:

* have its own specification under `specs/`
* have a numbered test inventory
* produce a coherent independently reviewable change
* avoid introducing unresolved scientific study decisions
* preserve backwards compatibility where explicitly required
* update the Session 11 implementation checklist as requirements are completed

---

# 2. Dependency overview

Recommended primary sequence:

`11a → 11b → 11c → 11d → 11e → 11f`

After the configuration foundation in 11b, three lanes can proceed with limited overlap:

**Allocation and lifecycle**

`11c → 11d → 11e → 11f`

**Study behaviour**

`11g → 11h → 11i`

**Telemetry**

`11j → 11k → 11l`

These converge into:

`11m → 11n → 11o`

Conceptually:

```text
11a Study identity
 │
 ▼
11b Configuration versions
 │
 ├─────────────┬────────────────┐
 ▼             ▼                ▼
11c           11g              11j
Scheduler     AI delivery       Browser telemetry
 │             │                │
 ▼             ▼                ▼
11d           11h              11k
Start case    Questions         Panel exposure
 │             │                │
 ▼             ▼                ▼
11e           11i              11l
Lifecycle     Study policies    AI viewed + PP
 │
 ▼
11f
Replacements
 │
 └─────────────┬────────────────┘
               ▼
              11m
        Privacy + integrity
               │
               ▼
              11n
         Phase 2 exports
               │
               ▼
              11o
       Integration / Phase 2 gate
```

---

# S11a — Study identity and Phase 2 configuration foundation

## Goal

Give every study an explicit identity and make the database unambiguously belong to exactly one study.

## Scope

Implement:

* `study_id`
* study ID validation
* study specific default database path such as `study_<study_id>.db`
* persistent study identity inside SQLite
* startup refusal when configured `study_id` and database `study_id` differ
* protection against cross study database reuse
* study identity available to application state and downstream persistence
* initial Phase 2 configuration schema migration

Also resolve the existing S5 TODO concerning configuration schema versioning and `config_hash`.

Recommended engineering decision:

* treat Phase 2 study configuration as a new schema generation rather than silently changing the meaning of schema v1
* explicitly document how historical v1 `config_hash` values remain interpretable

Do not introduce unresolved study specific values.

## Likely areas

* `config/study.py`
* `config/loader.py`
* `db/connection.py`
* `db/migrations.py`
* application startup
* config fixtures
* CLI validation

## Key tests

1. Valid `study_id` loads.
2. Invalid `study_id` refuses validation.
3. Default database filename contains the study ID.
4. New database records its study identity.
5. Matching study/database identity boots.
6. Mismatching study/database identity refuses startup.
7. Existing historical database migration is deterministic.
8. Two studies cannot silently share one database.

## Checklist coverage

Primarily sections 2 and 36.

---

# S11b — Configuration version history and immutable configuration provenance

## Goal

Allow one study to evolve through multiple valid configuration versions while preserving exactly which version governed each research observation.

## Scope

Implement persistent configuration history containing:

* `config_version`
* `config_hash`
* activation timestamp
* change description
* optional reason

Define one active configuration version at any given moment.

Guarantee:

* historical versions remain queryable
* a new configuration version does not create a new `study_id`
* already activated cases retain their original configuration identity
* subsequently activated cases use the newly active version
* unknown configuration identities are rejected
* configuration version and hash are available to downstream records

Do not yet build the final Phase 2 exports. This subsession creates the provenance model that later exports consume.

## Likely areas

* configuration models
* hashing
* `db/migrations.py`
* new configuration history DAO
* app startup/config registration
* session/case context

## Key tests

1. First configuration registers successfully.
2. Same version/hash is idempotent.
3. New valid version is recorded without replacing history.
4. `config_version` maps to the expected immutable `config_hash`.
5. Activated case remains pinned after configuration changes.
6. New activation receives the new version.
7. Unknown version/hash combinations refuse.
8. Configuration history survives restart.
9. `study_id` remains unchanged across versions.

## Checklist coverage

Section 3 and the provenance foundation for sections 31 to 33.

---

# S11c — Adaptive randomisation scheduler and planned allocation provenance

## Goal

Implement randomisation as a reproducible scheduling problem rather than independent pairwise coin flips.

## Scope

Create a scheduler capable of generating a clinician's planned case schedule using:

* study master randomisation seed
* clinician identity
* randomisation algorithm version
* current activated allocation state
* configured patient pool
* configured block rules
* configured starting arm rules

The scheduler must support:

* random patient order per clinician
* approximately balanced AI versus no AI exposure within clinician
* patient level AI versus no AI balance
* balancing using assignments actually activated so far
* configurable blocks
* configurable block sequence
* balanced starting arm
* deterministic reproduction from stored inputs

Store planned metadata including:

* patient order
* planned arm
* case position
* block number
* position within block
* preceding block/arm where applicable
* cases since previous AI exposure where applicable
* generation timestamp
* allocation state/context used for generation
* algorithm version
* configuration version used during generation

Important invariant:

**Generating a later clinician's schedule must never rewrite existing schedules or activated assignments.**

The scheduler should preferably be implemented as a pure domain layer with persistence wrapped around it. This will make balance and reproducibility tests considerably easier.

## Likely areas

* new randomisation/scheduling module
* new scheduler persistence DAO
* SQLite migrations
* deterministic seed derivation
* scheduler unit tests

## Key tests

1. Identical inputs reproduce identical schedule.
2. Patient order varies reproducibly by clinician.
3. Clinician AI/no AI balance obeys configured constraints.
4. Patient level imbalance is considered.
5. Starting arm balancing works.
6. Block constraints are respected.
7. Activated allocation state affects future scheduling.
8. Unactivated schedules do not count as realised exposure.
9. Generating clinician B does not rewrite clinician A.
10. Existing activated assignments remain immutable.
11. Algorithm version is persisted.
12. Generation context is sufficient to reproduce the schedule.

## Checklist coverage

Sections 8 and 9, planned allocation portion.

---

# S11d — Explicit Start Case and allocation concealment

## Goal

Move assignment activation from page access to an explicit clinician action.

This is an important architectural change from the current implementation.

At present, `bootstrap_session()` calls `arm_assignments.assign_or_lookup()` when a patient page is opened. Phase 2 requires GET/navigation to remain non consuming.

## Scope

Introduce explicit:

`Start case`

Before Start case:

* clinician may navigate the case/index UI permitted by the study
* no allocation is activated
* assigned arm is concealed
* no AI/no AI distinction is exposed
* accidental GET requests cannot consume an allocation

On successful Start case:

* scheduled patient is committed
* scheduled arm becomes immutable
* activation timestamp is recorded
* configuration version is recorded
* overall case position is recorded
* session/case timing begins
* activation happens atomically

Reopening an activated case must resume it rather than activate a new assignment.

Remove the current GET side effect from `bootstrap_session`.

## Likely areas

* `web/study_session.py`
* routes
* patient/index templates
* scheduler persistence
* sessions
* events
* `arm_assignments.py`
* progress model

## Key tests

1. GET does not activate an assignment.
2. Patient index navigation does not activate an assignment.
3. Start case activates exactly once.
4. Double POST cannot create two activations.
5. Activation transaction is atomic.
6. Activated patient and arm are immutable.
7. Configuration version is pinned at activation.
8. Reopening resumes the existing case.
9. Arm cannot be inferred before activation.
10. Failed Start case leaves no partially activated state.

## Checklist coverage

Section 4 and realised allocation fields from section 9.

---

# S11e — Case lifecycle, reconnection, pause/resume and clinician stopping

## Goal

Represent what happens to an activated case after Start case.

## Scope

Support lifecycle states sufficient to distinguish:

* planned
* activated
* active
* paused
* resumed
* completed
* incomplete/abandoned

Implement:

* pause event
* resume event
* interruption detection
* configurable reconnection grace period
* configurable voluntary pause policy
* interruption timeout
* structured incomplete reason
* activated cases never return to allocation pool
* incomplete activated cases remain part of the study record

Add clinician level stopping:

* configurable target completed cases
* configurable maximum activated cases
* no new cases after the applicable clinician rule is reached
* no automatic whole study stopping

Keep optional overall study target informational only.

## Key tests

1. Activation transitions to active.
2. Pause and resume produce auditable transitions.
3. Resume inside grace period continues the same case.
4. Timeout beyond grace period produces incomplete activated case.
5. Timed out assignment remains allocated.
6. Completed count triggers configured stopping.
7. Maximum activated count triggers configured stopping.
8. Whole study is not automatically closed.
9. Structured incomplete reason round trips.

## Checklist coverage

Sections 5 and 7.

---

# S11f — Replacement case scheduling

## Goal

Provide replacements without modifying or erasing the original incomplete case.

## Scope

Implement replacement selection from the clinician's unused patient pool.

Guarantee:

* clinician never sees the same patient twice
* original incomplete case remains intact
* replacement relationship is recorded
* replacement respects balance as closely as possible
* patient level balance is considered
* block/sequence constraints are considered
* replacement activation still happens through Start case

Keep replacement logic separate from ordinary scheduler generation where possible.

## Key tests

1. Replacement never reuses a seen patient.
2. Original incomplete case remains unchanged.
3. Replacement link is bidirectionally auditable or otherwise reconstructable.
4. Replacement is drawn only from eligible unused patients.
5. Replacement selection considers arm balance.
6. Replacement respects configured sequence constraints.
7. Original and replacement both appear in audit data.

## Checklist coverage

Section 6.

---

# S11g — AI/no AI intervention delivery, artifact provenance and temporal preflight

## Goal

Deliver the randomised intervention correctly and prove that the displayed intervention is temporally valid.

## Scope

For AI assigned cases:

* render AI panel
* bind it to frozen prediction artifact identity
* retain model/system version
* prediction artifact hash
* explanation artifact hash where applicable
* presentation/template version
* intervention build identifier

For no AI cases:

* AI panel must not exist
* no disabled AI placeholder
* no "AI unavailable" message
* avoid intervention revealing empty space
* non AI functionality remains the same

Add preflight checks for:

* direct reference outcome leakage
* post timepoint clinical information
* AI output corresponding to correct timepoint
* frozen artifact availability and identity

This subsession validates availability and intended rendering. Structured runtime intervention failures are handled later in S11l.

## Key tests

1. AI panel exists only for AI assigned case.
2. No AI HTML contains no AI panel/placeholder.
3. Correct frozen artifact is used.
4. Artifact hash is retained.
5. Wrong timepoint AI artifact fails preflight.
6. Known future clinical variable fails preflight.
7. Outcome revealing field fails preflight.
8. Non AI interface behaviour remains equivalent between arms.

## Checklist coverage

Sections 10 and 11.

---

# S11h — Conditional question engine and first use case question set

## Goal

Extend the existing flat question system into a deterministic branch aware question system.

## Scope

Support conditions in question configuration.

Implement first use case behaviour:

* deterioration Yes → show primary cause
* deterioration No → hide and ungate primary cause
* changing Yes to No clears or explicitly invalidates stale hidden cause response
* good neurological outcome Yes → death at three months becomes No
* good neurological outcome No → death question requires explicit response

The advance gate must operate on the questions required by the **current branch**, not on the entire static question list.

Conditional state must survive:

* autosave
* refresh
* session resume

Update first use case question configuration to include only:

* deterioration within next six hours
* confidence
* primary cause
* good neurological outcome at three months
* death at three months

Do not silently retain the old example questions.

## Likely areas

* `config/questions.py`
* question schema generation/version
* `web/gating.py`
* answer capture
* question templates/JS
* answer codec
* study fixtures

## Key tests

1. Deterioration Yes displays cause.
2. Deterioration No hides cause.
3. Hidden cause does not block advance.
4. Branch change after saved cause handles stale answer correctly.
5. Good outcome Yes forces death No.
6. Good outcome No exposes explicit death response.
7. Conditional state survives refresh.
8. Conditional state survives resume.
9. First use case does not contain old example questions.

## Checklist coverage

Sections 12 and 13.

---

# S11i — Study behaviour policy switches

## Goal

Implement remaining configurable clinician facing study behaviour without hard coding first use case choices into the platform.

## Scope

Add configuration and runtime handling for:

### Backward navigation

When enabled:

* previous answers frozen
* revisit distinguishable from first presentation
* original timing not overwritten
* revisit exposure identifiable

When disabled:

* measured case backward navigation refused cleanly

### Feedback

Default:

* no ground truth
* no correctness feedback
* no running performance score
* no AI correctness feedback

### Practice

Support explicit practice mode for future studies.

Practice must not contribute to:

* ITT
* PP
* case target count
* randomisation balance

First use case keeps practice disabled.

### Free text

* disabled by default
* explicit opt in
* treated as potentially identifying
* excluded from behavioural summaries/figures
* routine export handling explicitly controlled

## Key tests

Focus tests on each policy boundary and on ensuring configuration, rather than code constants, controls behaviour.

## Checklist coverage

Sections 25, 26, 27 and 29.

---

# S11j — Browser visibility, focus, active time and tab identity

## Goal

Create the client side telemetry foundation upon which panel exposure is measured.

## Scope

Introduce browser/tab identifier.

Record raw state transitions for:

* document visible/hidden
* focus
* blur
* qualifying activity

Use a browser monotonic clock such as `performance.now()` for duration measurement.

Retain server timestamps for ordering/audit.

Derive:

* `elapsed_seconds`
* `foreground_seconds`
* `active_seconds`

Foreground time counts only while:

* document visible
* browser focused

Active time additionally excludes inactivity beyond configured threshold.

Activity includes:

* click
* touch
* scroll
* keyboard input
* answer modification
* panel open/close
* timepoint navigation

Passive mouse movement does not reset inactivity.

Add enough tab identity infrastructure to identify simultaneous views. Full conflict handling can be finalised in S11m.

## Key tests

1. Hidden tab does not accumulate foreground time.
2. Unfocused browser does not accumulate foreground time.
3. Refocus resumes foreground accumulation.
4. Inactivity threshold removes excess inactive duration.
5. Qualifying input resumes active time.
6. Mouse movement alone does not resume active time.
7. Browser tab ID is stable for a tab/session.
8. Durations use monotonic browser timing rather than network round trip.

## Checklist coverage

Sections 14, 15 and 16 plus the identity foundation from section 34.

---

# S11k — Panel exposure telemetry and viewing episodes

## Goal

Measure actual panel exposure independently from interaction.

## Scope

Instrument all major information panels.

For each panel retain raw events sufficient to derive:

* cumulative qualifying duration
* viewed Yes/No
* viewing episode count
* time to first view
* first view timestamp
* last view timestamp
* panel open count

A qualifying episode requires simultaneously:

* configured viewport threshold satisfied
* document visible
* browser focused
* panel expanded

Collapsed headers do not count.

Implement:

* `IntersectionObserver` or equivalent viewport measurement
* cumulative episodes within each timepoint
* reset on new timepoint
* episode end reasons
* panel open and close events

First use case configuration:

* viewport threshold = 5%
* viewed threshold = 2 cumulative seconds

Reading without interacting must still count.

Raw events remain authoritative.

## Key tests

1. 4.9% visibility does not qualify under a 5% threshold.
2. Qualifying visibility accumulates.
3. Scroll away ends episode.
4. Return begins a new episode.
5. Tab hide ends episode.
6. Focus loss ends episode.
7. Collapse ends episode.
8. Separate episodes sum correctly.
9. Timepoint change resets cumulative exposure.
10. Panel open alone does not imply viewed.
11. Passive reading accumulates exposure.

## Checklist coverage

Sections 17 to 20.

---

# S11l — AI viewed, intervention integrity, PP status and missing response provenance

## Goal

Turn raw intervention and telemetry data into observation level study variables without changing ITT assignment.

## Scope

At each:

`clinician × patient × timepoint`

derive:

* assigned arm
* AI actually delivered
* cumulative AI qualifying exposure
* `ai_viewed`
* intervention failure
* intervention leakage
* PP compliant

For first use case:

`ai_viewed = cumulative qualifying AI exposure >= 2 seconds`

AI assigned PP compliance requires:

* AI delivered
* AI viewing threshold reached

No AI PP compliance requires:

* AI remained unavailable
* no leakage occurred

Add structured integrity events for:

* AI render failure
* missing AI artifact
* AI display failure
* accidental AI exposure in no AI condition
* other intervention integrity failure

Never mutate ITT assignment because of failure or leakage.

Add structured missing response states:

* case abandoned
* technical failure
* reached timepoint but unanswered
* timepoint never reached

## Key tests

1. 1.99 seconds is not AI viewed.
2. 2.00 seconds is AI viewed.
3. Exposure resets at next timepoint.
4. AI viewed does not require active interaction.
5. AI failure preserves AI ITT arm.
6. Leakage preserves no AI ITT arm.
7. PP is observation specific.
8. One case may contain compliant and non compliant observations.
9. Missing answer remains missing.
10. Missing reason remains separately identifiable.

## Checklist coverage

Sections 21 to 24.

---

# S11m — Privacy, multi tab protection and backup identity

## Goal

Harden Phase 2 behavioural data collection around identity, conflicting browser state, and study specific persistence.

## Scope

### Clinician privacy

* remove `name_normalized` from new behavioural payloads
* use `clinician_id`
* retain name only for operational lookup
* preserve separate optional name mapping keyfile
* do not migrate historical events solely for this change

### Multi tab protection

Using tab IDs introduced in S11j:

* detect simultaneous active views of the same measured case
* preferably prevent conflicting active views
* where prevention cannot be guaranteed, record conflict explicitly

### Backups

Every backup remains attributable to:

* `study_id`
* schema/database version
* creation timestamp

Do not combine backups between studies.

## Key tests

1. New behavioural events contain clinician ID.
2. New behavioural payloads contain no normalised clinician name.
3. Keyfile behaviour remains separate.
4. Two tabs cannot silently act as one active measured case.
5. Unavoidable tab conflict produces an audit record.
6. Backup identity contains study ID and DB version.
7. Different studies cannot silently share backup identity.

## Checklist coverage

Sections 28, 30 and 34.

---

# S11n — Linked Phase 2 research exports

## Goal

Replace the single export assumption with a reproducible linked Phase 2 export bundle.

## Scope

Produce separate linked outputs for at least:

### Answers and timepoints

* clinician responses
* assigned arm
* elapsed time
* foreground time
* active time
* completion/missing status
* configuration identity
* PP relevant fields where appropriate

### Panel summaries

* panel viewed
* cumulative duration
* episode count
* first view latency
* first/last view timestamp
* panel open count

### Raw behavioural events

* source telemetry
* tab ID
* configuration identity

### Randomisation audit

* planned patient order
* planned arm
* block/sequence metadata
* generation context
* activated status
* activation timestamp
* lifecycle outcome
* replacement relationship
* randomisation algorithm version

### Configuration history

* config version
* config hash
* activation timestamp
* description
* reason

Every appropriate output exposes stable identifiers including:

* `study_id`
* `clinician_id`
* `patient_id`
* timepoint
* case/session identity
* `config_version`
* `config_hash`

Change current export validation behaviour:

* multiple known configuration versions within the same study are valid
* missing configuration identity refuses export
* unknown configuration identity refuses export
* internally inconsistent identity refuses export

Provide configuration version counts.

Routine exports remain pseudonymised.

## Key tests

1. Multiple valid config versions export successfully.
2. Each row retains its own version/hash.
3. Unknown version refuses.
4. Missing version refuses.
5. Version/hash mismatch refuses.
6. All export datasets join through expected identifiers.
7. Randomisation planned versus realised history is reconstructable.
8. Incomplete cases remain visible.
9. Panel raw events reproduce panel summary.
10. Configuration summary counts agree with observations.
11. Free text does not leak into prohibited telemetry outputs.

## Checklist coverage

Sections 31 to 33 plus export related requirements throughout the gate.

---

# S11o — Phase 2 integration, regression gate and documentation reconciliation

## Goal

Prove that the individual S11 subsessions operate as one coherent Phase 2 system.

No major new feature should originate here.

## Scope

Build complete integration scenarios including:

### Normal AI case

schedule → Start case → AI delivery → answers → telemetry → completion → export

### Normal no AI case

schedule → Start case → no AI UI → answers → completion → export

### Incomplete case

activation → interruption → timeout → incomplete record → replacement → export

### Configuration update

case A activated under version 1 → version 2 activated → case A remains v1 → case B uses v2 → mixed version export succeeds

### Intervention failure

AI assignment → artifact/display failure → ITT remains AI → PP non compliant → failure exported

### Leakage

no AI assignment → leakage event → ITT remains no AI → PP non compliant

### Conditional questions

branch changes and stale answers do not break gating or export.

### Multi tab conflict

conflicting active case views are prevented or audited.

Run the complete numbered Session 11 regression inventory.

Update:

* `specs/ROADMAP.md`
* Session 11 implementation checklist
* project architecture documentation
* example study configuration
* example question configuration
* CLI/help text where necessary

Remove or revise outdated roadmap statements describing S11 as simple independent pairwise randomisation.

## Final acceptance

Phase 2 implementation is complete when the simulator can:

1. identify and isolate one study
2. preserve configuration history
3. generate reproducible constrained randomisation
4. activate assignments only through explicit Start case
5. preserve incomplete activated cases
6. schedule non duplicating replacements
7. deliver AI and no AI correctly
8. record failures and leakage
9. record elapsed, foreground, active and panel exposure timing
10. derive observation level AI viewed status
11. support conditional first use case questions
12. preserve ITT and PP variables
13. produce linked research exports
14. reconstruct planned and realised allocation
15. preserve configuration provenance for every research observation
16. avoid silently introducing unresolved study design decisions

---

# 3. Agent ownership rule

An agent assigned one subsession should receive:

1. the Phase 2 gate
2. the Session 11 implementation checklist
3. the specific S11x specification
4. relevant existing session specifications
5. the current repository state

The agent should not be asked to implement "anything else from Phase 2."

Every S11x specification should explicitly contain:

* goal
* in scope
* out of scope
* existing behaviour being replaced
* database/schema changes
* configuration changes
* public API/route changes
* invariants
* failure behaviour
* numbered tests
* acceptance criteria
* migration/backwards compatibility considerations
* checklist items closed by the subsession

This keeps each unit reviewable and prevents an agent from solving an unresolved scientific question while implementing an engineering task.

---

# 4. Important cross session invariants

The following should be repeated in every relevant subsession spec.

### Allocation

An activated assignment is immutable.

### Patient reuse

A clinician never reviews the same measured patient twice.

### ITT

Technical failure, non viewing or leakage never changes the randomised arm.

### Configuration

An activated case remains pinned to the configuration version under which it started.

### Scheduling

Future scheduling may react to realised activated allocation state. Historical schedules and activated cases are never rewritten.

### Telemetry

Raw behavioural events are the source of truth. Derived exposure variables must be reproducible from them.

### Missingness

The simulator records missingness and its reason. It never manufactures a response.

### Scientific configuration

Parameters left study specific by the Phase 2 gate remain configuration inputs rather than code constants.

---

# 5. Suggested spec filenames

```text
specs/session-11a-study-identity.md
specs/session-11b-config-version-history.md
specs/session-11c-adaptive-randomisation.md
specs/session-11d-explicit-case-start.md
specs/session-11e-case-lifecycle.md
specs/session-11f-replacement-cases.md
specs/session-11g-intervention-delivery.md
specs/session-11h-conditional-questions.md
specs/session-11i-study-behaviour-policies.md
specs/session-11j-browser-telemetry.md
specs/session-11k-panel-exposure.md
specs/session-11l-ai-viewed-and-pp.md
specs/session-11m-privacy-multitab-backups.md
specs/session-11n-phase2-exports.md
specs/session-11o-phase2-integration.md
```

