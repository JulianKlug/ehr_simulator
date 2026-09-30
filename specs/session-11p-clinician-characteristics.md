# Session 11p — Clinician characteristics

## Goal

Collect the clinician characteristics needed for descriptive reporting and prespecified subgroup analyses, without changing clinician identity, randomisation or study behaviour.

S11a through S11o are complete.

## Decisions taken where the request was open

1. **Opt in per study.** An optional `clinician_profile` block on `StudyConfig` switches collection on; it is omitted from serialization when absent, so earlier snapshots and hashes do not move. Preflight WARNs a Phase 2 study without it. The shipped Phase 2 example declares it.
2. **Vocabularies are configuration, roles are code.** `professional_role` is the fixed enum `physician | nurse` (the specialty rule depends on it). `primary_specialty` and `country_of_practice` come from configured lists (`specialties`, `countries`; countries are ISO 3166-1 alpha-2 codes), so a study never stores free text here.
3. **Value at enrolment, never silently changed.** A profile may be created and edited freely until the clinician's first measured case is activated. From then on it is locked, both in the service and by a database trigger; a correction needs an explicit operator action (out of scope). Later sessions show the stored values for confirmation.
4. **Gate at Start case only.** Without a complete profile, Start case refuses to activate a new measured case (409), and the index shows a *Complete your profile* action. Resuming an open case and practice cases are not blocked. After a successful login the clinician is sent to the profile page when one is still missing.
5. **Validation against the active configuration at save time.** Stored values are exported as stored; a later configuration version with other vocabularies does not reinterpret them.
6. **Sanity bound.** `0 <= years_of_practice <= 80` (`MAX_YEARS_OF_PRACTICE`), finite, stored as REAL.
7. **Audit without values.** Each save appends `clinician.profile_saved` with payload `{"action": "created|updated"}`; characteristics never enter an event payload.

## Core invariants

1. `clinician_id` stays the only research identifier; the name stays in `clinicians` only.
2. Characteristics are not an input of scheduling, activation, lifecycle, telemetry or any derivation.
3. A profile never changes after the clinician's first measured case exists.
4. Nurses have no specialty; physicians always have one (database CHECK).
5. No characteristic appears in `events.payload_json`.

## Configuration

```yaml
clinician_profile:
  specialties: [neurology, emergency_medicine, internal_medicine, intensive_care, other]
  countries: [CH, FR, DE, IT, AT]
```

- both lists required, non empty, unique; specialties match `^[a-z][a-z0-9_]{0,63}$`; countries match `^[A-Z]{2}$`
- the block is pinned in the configuration snapshot like every other block

## Database (migration 14 `s11p_clinician_profiles`)

```
CREATE TABLE clinician_profiles (
    clinician_id         TEXT PRIMARY KEY REFERENCES clinicians(clinician_id),
    professional_role    TEXT NOT NULL CHECK (professional_role IN ('physician', 'nurse')),
    years_of_practice    REAL NOT NULL CHECK (years_of_practice >= 0),
    country_of_practice  TEXT NOT NULL,
    primary_specialty    TEXT,
    recorded_at          TIMESTAMP NOT NULL,
    updated_at           TIMESTAMP NOT NULL,
    CHECK ((professional_role = 'physician') = (primary_specialty IS NOT NULL))
);
```

Triggers refuse any update once `arm_assignments` holds a `phase2_randomized` row of the clinician, and refuse every delete.

## Layers

```
routes  GET/POST /profile ─► web/clinician_profile_page (form ⇄ service)
          └─► clinician_profile.py   pure: parse + validate against the pinned vocabulary
          └─► db/clinician_profiles.py   sole writer; lock + CHECKs in SQLite
case_start._refuse_without_profile (Phase A and Phase B)
export_phase2 ─► clinicians.csv
```

## Routes

- `GET /profile`: the form, prefilled; read only with a "locked" note once a measured case exists. 404 outside a study whose active configuration has `clinician_profile`.
- `POST /profile` (form fields `professional_role`, `years_of_practice`, `country_of_practice`, `primary_specialty`): 303 → `/` on success; 422 with the form and field messages on invalid input; 409 when locked. Unknown clinician → login redirect.
- `POST /login` redirects to `/profile` instead of `/` while the active configuration requires a profile the clinician lacks.

## Validation

- `professional_role` ∈ `physician | nurse`
- `years_of_practice`: a finite number, `0 <= y <= 80`
- `country_of_practice` ∈ configured `countries`
- physician: `primary_specialty` ∈ configured `specialties`; nurse: `primary_specialty` must be empty (a submitted specialty is refused, not dropped)

## Export

`export-phase2` adds `clinicians.csv`, one row per clinician in the bundle (the same set the keyfile covers):

```
study_id clinician_id profile_status professional_role years_of_practice country_of_practice primary_specialty
```

`profile_status` is `complete` or `missing` (blank characteristics). No name, no recorded_at. The manifest lists the file like every other.

## Required tests

1. A valid physician profile is stored and restored on the form.
2. A valid nurse profile stores no specialty.
3. Without a profile, Start case refuses (409), writes no schedule or assignment, and the index offers the profile action.
4. Negative, non numeric, non finite or > 80 years are refused (422), nothing stored.
5. A physician without specialty, a nurse with one, an unknown role, specialty or country are refused.
6. The profile is editable before the first measured case and locked after it (service 409 and trigger).
7. Login redirects to `/profile` while it is missing and to `/` once saved.
8. `clinicians.csv` lists every bundle clinician with characteristics and `profile_status`.
9. No routine export file and no event payload contains the clinician name or a characteristic.
10. Saving a profile writes only `clinician_profiles` and one `clinician.profile_saved`; schedules, assignments and existing cases are unchanged, and the planned schedule is identical whatever the profile values (a physician and a nurse under the same configuration get the same items). Adding the block itself is a configuration change and moves `config_hash` like any other.
11. A study without the block: no gate, `/profile` is 404, preflight WARNs for Phase 2.
12. Migration 14, the schema snapshot, and the role/specialty CHECK.
13. An open case started before a version that adds the block still resumes without a profile.

## Out of scope

Operator correction of a locked profile, profile history, free text specialties, clinician characteristics as randomisation strata.

## Acceptance

Characteristics are collected, validated, persisted and exported pseudonymously; allocation, lifecycle, telemetry and identity behave exactly as before.
