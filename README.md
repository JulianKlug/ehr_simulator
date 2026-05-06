# EHR simulator

A browser-based simulated electronic health record. It replays a real patient's
timeline to you at a few discrete moments in time, optionally shows you what an
AI model predicted, and (in later versions) asks you a short set of questions
at each timepoint.

The point isn't the chart. The point is to measure how AI assistance changes
the assessments and decisions you'd make.

> **Status — May 2026.** Session 02 build. You can walk three synthetic patients
> across three timepoints with vitals, labs, admission, imaging, and AI panels
> visible. **Login, question-answering, CSV export, and AI-on/AI-off
> randomization are not in this build yet** — they ship in Sessions 5–11.
> If a teammate has asked you to use this for an actual study session, you
> are an early reviewer, not an end user. See *What this build is for* below.

---

## Running it

Requires [`uv`](https://docs.astral.sh/uv/) on the machine.

```bash
uv sync
uv run ehr-simulator serve
```

Then open http://localhost:8000 in any modern browser. Pick a patient, pick a
chrome variant (see below), and you're in.

The server stays in your terminal — `Ctrl-C` to stop. Logs go to
`./logs/current.jsonl` (one JSON record per request, rolled at UTC midnight).

---

## Walking a patient

You'll see the patient view at **timepoint 0** (the moment of first contact).

| Key | Action |
|---|---|
| <kbd>]</kbd> | Next timepoint |
| <kbd>[</kbd> | Previous timepoint |
| <kbd>?</kbd> | Show keyboard shortcuts overlay |

Pressing past the first or last timepoint **does nothing** and shows a small
"already at first/last timepoint" notice in the summary header. There is no
wraparound.

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

This is the build used in the **first chrome A/B session with the embedded
neurologist**. The goal of that session is to lock layout, density, and
discoverability — not to validate clinical accuracy.

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

We'll write up findings in `specs/session-02-validation-findings.md` after.

---

## Privacy

The simulator runs entirely on your local machine. Nothing leaves your laptop.
No accounts, no cloud, no telemetry. The only thing written to disk is the
local JSONL log under `logs/`, which records request paths and timepoint
indices — never any free-text input.

---

## What's *not* in this build

These are scheduled for later sessions. If you're missing one of these, you
are not missing it because of a bug:

- **Login** — there is no `clinician_name` field yet. (Session 5.)
- **Questions to answer** — no answer-capture form, no per-timepoint gating.
  (Session 9.)
- **CSV export of answers** — depends on the above. (Session 9.)
- **AI on/off randomization** — the AI panel is always shown for now.
  (Session 11.)
- **MIMIC / Geneva real-data** — only synthetic patients today. (Sessions 4
  and 8.)
- **DICOM image rendering** — the imaging panel shows the report text, not
  the images. (Out of scope for v1.)

---

## Reporting issues

If something breaks or feels wrong, please grab two things and send them along:

1. The URL in the address bar at the moment it broke.
2. The last few lines of `logs/current.jsonl` (one JSON object per line — easy
   to copy).

That's enough to reconstruct what happened on our end.
