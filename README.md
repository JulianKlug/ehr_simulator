# EHR simulator

A browser-based simulated electronic health record. It replays a real patient's
timeline to you at a few discrete moments in time, optionally shows you what an
AI model predicted, and asks you a short set of questions at each timepoint.

The point isn't the chart. The point is to measure how AI assistance changes
the assessments and decisions you'd make.

> **Status — September 2026.** Sessions 1–6, 9a + 9b. You sign in with your
> name, walk three synthetic patients across three timepoints with vitals,
> labs, admission, imaging, and AI panels visible, and answer the configured
> questions at each timepoint — answers auto-save to a local SQLite file, and
> the next timepoint stays locked until the current one is answered.
> **CSV export and AI-on/AI-off randomization are not in this build yet** —
> they ship in Sessions 9c and 11. If a teammate has asked you to use this for
> an actual study session, you are an early reviewer, not an end user. See
> *What this build is for* below.

---

## Running it

Requires [`uv`](https://docs.astral.sh/uv/) on the machine.

```bash
uv sync
uv run ehr-simulator serve \
  --config configs/example_config.yaml \
  --questions configs/example_questions.yaml
```

`--config` and `--questions` go together. Pass neither and you get the same
three synthetic patients without a questions pane — sign-in and the database
are there either way. A study config must declare a `study_id` (lowercase
letters, digits, `-`, `_`): it names the study, and its answers are bound to
exactly one database. One database holds exactly one study — the server
refuses to boot against a database owned by another `study_id`, or a database
that already holds unlabelled data.

Then open http://localhost:8000 in any modern browser. Type your name on the
sign-in page, pick a patient, pick a chrome variant (see below), and you're in.
The name is all the login is — no password, no account. It is case-folded and
hashed into a short `clinician_id` that every stored answer is keyed by. You
stay signed in for 30 days; **Logout** in the header stripe switches clinician.

The server stays in your terminal — `Ctrl-C` to stop. Logs go to
`./logs/current.jsonl` (one JSON record per request, rolled at UTC midnight);
answers and interaction events go to `./data/study_<study_id>.db` in study
mode, or `./data/ehr_simulator.db` without a study config.

---

## Walking a patient

You'll see the patient view at **timepoint 0** (the moment of first contact).

| Key | Action |
|---|---|
| <kbd>]</kbd> | Next timepoint (presses the advance button when you're at the newest one) |
| <kbd>[</kbd> | Previous timepoint |
| <kbd>?</kbd> | Show keyboard shortcuts overlay |

Pressing past the first timepoint — or past the last one with nothing left to
advance to — **does nothing** and shows a small "already at first/last
timepoint" notice in the summary header. There is no wraparound.

The summary card at the top always shows: patient ID, age, sex, the current
clinical time `t = N min`, and how many rows of each kind have been revealed
so far.

### What you see, and when

The simulator only reveals data **up to and including the current timepoint**.
At t=0 you see what was knowable at first contact; at t=60 you also see what
was recorded in the first hour; and so on. You cannot peek ahead — neither in
the visible panels nor in the underlying HTML — and that's enforced by a
regression test on every commit.

---

## The five panels

| Panel | What's in it |
|---|---|
| **Vitals** | HR, SBP, DBP, SpO₂, temp — one timeline per variable |
| **Labs** | hgb, sodium, creatinine, glucose — one timeline per variable |
| **Admission** | static facts: age, sex, NIHSS on admission, stroke location, time of onset |
| **Imaging** | per-timepoint imaging entries (modality + report text) |
| **AI** | the precomputed model output for this patient at each revealed timepoint |

Each panel can be in one of five states. The state is shown in the panel's
`aria-label` and via a small italicized note when relevant:

- **Loading** — data is present and rendered. The default for a healthy panel.
- **Empty (expected)** — this patient simply has no data of this kind. You are
  not missing anything.
- **Empty (unexpected)** — the dataset has rows for this patient at *later*
  timepoints, but none yet. A signal that something will arrive.
- **Partial** — some data is here, some isn't yet. The panel will tell you so.
- **Error** — the panel itself failed to render. The other four panels are
  unaffected; the page does not crash.

If a panel is empty-expected for a patient (e.g., an outpatient with no head
CT), that's not a bug — that patient really has no data of that kind.

---

## Answering the questions

In study mode a questions pane sits beside the panels, one form per question
from your `--questions` file. There is no save button: every choice saves as
you make it, free text 1.5 seconds after you stop typing. The badge under each
question reads `Saved ✓`, `Cleared`, the reason the save was rejected, or
`Save failed — retry` if the request never reached the server. Clearing every
field of a question deletes that answer. Free text is capped at 4000
characters; a probability must be a whole number from 0 to 100.

`[` and `]` still work while a radio or checkbox has focus, but not from
inside the free-text box — click out of it first.

Questions are required by default. `required: false` in your `--questions` file
opts one out — that's how a free-text "anything else?" box stays optional. A
required multi-select has to offer an explicit opt-out (the example config uses
`None of these`), because an empty multi-select stores nothing and the gate
reads nothing as unanswered.

### Advancing

The button at the foot of the pane is the only way forward. It stays muted
until every required question is answered, and its label counts what's left —
`Next timepoint · 3 unanswered`. Clicking it while blocked doesn't move you: it
scrolls to the first unanswered question and focuses it. Once you're done the
label becomes `Next timepoint ›`, or `Finish patient ✓` on the last timepoint,
which takes you back to the patient list. <kbd>]</kbd> presses the same button.

**Advancing freezes what's behind you.** Earlier timepoints stay readable with
your answers pre-filled, but every field is disabled and the pane says
`Locked — answered before you advanced.` That's the point of the study: an
answer at t=60 has to reflect what you knew at t=60, not what t=180 told you.
A URL for a timepoint you haven't unlocked bounces you back to the one you're
on, and no patient data is rendered on the way.

The patient list marks each patient `not started`, `in progress · t 2/3` or
`complete ✓`, and every link resumes where you left off.

There is no undo. If you press Next by mistake, whoever runs the study can
rewind it for you:

```bash
uv run ehr-simulator reset-progress configs/example_config.yaml \
  --clinician "Your Name" --patient synth_001 --to-t-index 0
```

`--to-t-index` defaults to 0. Answers *after* the target timepoint are deleted;
answers at it survive and pre-fill the re-opened pane. `--db-path` points it at
a database other than the study config's.

---

## Two chrome variants

The same data, two different layouts. Pick whichever feels more natural —
during the embedded-clinician sessions we want feedback on which one you'd
actually use day-to-day.

- **`?chrome=dense`** — single scrollable page, all five panels visible at
  once, tighter type. Optimized for "see everything."
- **`?chrome=epic`** — tabbed interface (one panel at a time), larger type and
  more whitespace, layout closer to Epic's conventions. Optimized for
  "focus on one panel."

The chrome is in the URL, not a cookie, so you can paste a link to the exact
view you want a colleague to look at.

---

## What this build is for

Reviewer sessions with the embedded neurologist target layout, density, and
discoverability — not clinical accuracy.

A note on the data: **the patient values are synthetic and not clinically
realistic.** They're physiologically plausible noise, not real cases. Don't
read into the numbers. We're asking you to evaluate the *interface*, not the
*patients*.

If you have feedback during the session, we're particularly interested in:

- Which panel did you look at first, and why?
- Did the timepoint walking feel natural with `[`/`]`, or did you reach for the
  mouse?
- When a panel was in `partial` or `empty-unexpected` state, was it obvious
  what that meant?
- Anything in the chrome that felt "wrong" relative to a real EHR — even
  small things.

Findings from earlier sessions: `specs/feedback/session-02-feedback.md`.

---

## Privacy

The simulator runs entirely on your local machine. Nothing leaves your laptop.
No cloud, no telemetry. Three things are written to disk:

- `logs/current.jsonl` — request paths, timepoint indices, and your
  `clinician_id`. Never any free-text input.
- `data/ehr_simulator.db` (or `data/study_<study_id>.db` in study mode) —
  your name (case-folded), your answers including
  free text, and one row per interaction event.
- `data/backups/` — a copy of that database, made when a server that wrote
  something shuts down. `serve --backup-dir` moves it.

Move the database with `serve --db-path`, the `EHR_SIM_DB_PATH` environment
variable, or `db_path:` in the study config. The last two must point inside the
working directory; `--db-path` is taken at face value. In study mode the
per-study default is `data/study_<study_id>.db` — each study gets its own
database, and the identity check still applies to any path you name.

---

## What's *not* in this build

These are scheduled for later sessions. If you're missing one of these, you
are not missing it because of a bug:

- **CSV export of answers** — they live in SQLite only for now. (Session 9c.)
- **AI on/off randomization** — the AI panel is always shown for now.
  (Session 11.)
- **MIMIC / Geneva real-data** — only synthetic patients today. (Sessions 7
  and 8.)
- **DICOM image rendering** — the imaging panel shows the report text, not
  the images. (Out of scope for v1.)

---

## Reporting issues

If something breaks or feels wrong, please grab these and send them along:

1. The URL in the address bar at the moment it broke.
2. The last few lines of `logs/current.jsonl` (one JSON object per line — easy
   to copy).
3. If it happened while answering a question, the badge text you saw.

That's enough to reconstruct what happened on our end.
