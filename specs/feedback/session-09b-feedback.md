# Session 09b — clinician-facing feedback

## Round 01 (2026-09-16, owner dogfood on synthetic, study mode)

### F1 — "Saved ✓" shifts every question below it

**Observed:** each time a badge flips from blank to `Saved ✓`, the questions underneath move down ~3px. Irritating on every answer.

**Cause (measured with Playwright at 1000px and 1400px):** the blank badge reserves `min-height: 1.2em` (16.3px at 13.6px font) but the rendered line box with `line-height: normal` is 19px, so the saved badge is 3px taller than the blank one.

**Change:** `.answer-status` gets an explicit `line-height: 1.4` and `min-height: 1.4em`, so blank and saved occupy the same box. Locked by `tests/e2e/test_pane_layout.py::test_saving_an_answer_does_not_shift_other_questions` (question bounding boxes before/after a save must be identical, both widths).

### F2 — questions below the chrome are hard to work with

**Observed:** on a laptop (< 1200px) the pane stacked below the five tabs; the clinician scrolls past the data to answer, then back up to read. Requested: a pane on the right that can be toggled in and out.

**Change:** the pane is a fixed right-hand drawer at every width (`--pane-w = min(26rem, 92vw)`). Open by default; the chrome keeps a right margin equal to the drawer width at ≥ 900px (side by side) and is overlaid below that. An edge tab (`.pane-tab`, vertical, attached to the drawer's left edge) toggles it and carries the remaining-count so the gate stays visible while closed; `q` toggles from the keyboard (ignored inside text fields, like `[`/`]`). State is per browser tab in `sessionStorage` and re-applied after every `#patient-view` swap and on reload. Collapsed: `aria-hidden` + `inert`, `aria-expanded` on the tab. Locked by `test_pane_is_a_toggleable_right_drawer`.

**Not changed:** pane content, CTA, lock note. The `/plan-design-review` on the pane is still owed (TODOS).
