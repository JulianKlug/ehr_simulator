// advance.js — pinned htmx.org@2.0.4
//
// Client side of the S9b advance CTA (#advance-form). Four jobs:
//
//   1. Stamp every /advance POST with client_ts + client_seq from the
//      shared per-tab counter in client_seq.js.
//   2. Let the server's 409 (blocked → CTA fragment, retargeted onto itself)
//      and 412 (stale → the frontier's #patient-view) swap in; htmx 2 skips
//      4xx swaps by default. Anything else ≥400, or a dead network, gets a
//      local "Could not advance" hint instead of a silently stuck button.
//   3. After a 409 swap, scroll to the first unanswered question, focus its
//      first control and flash it. Scoped to 409s from #advance-form only:
//      htmx fires htmx:afterSwap for out-of-band inserts too, and the CTA
//      rides OOB behind every /answer 200 — an unscoped handler would yank
//      focus out of a textarea on each debounced autosave.
//   4. Nothing else. `]` at the frontier is keyboard.js clicking #advance-btn.
//
// Listeners are delegated on document.body and filter on
// e.detail.requestConfig.elt: htmx dispatches beforeSwap on the pre-retarget
// node, so the dispatch target is not the element to key on.
// No inline JS anywhere (CSP script-src 'self').

(function () {
    "use strict";

    const ADVANCE_FORM_ID = "advance-form";
    const ADVANCE_HINT_SELECTOR = ".advance-hint";
    const QUESTION_FORM_SELECTOR = "form.question";
    const FIRST_CONTROL_SELECTOR = "input:not([type=hidden]), textarea";
    const HIGHLIGHT_CLASS = "is-highlighted";
    const HIGHLIGHT_MS = 1500;
    const HTTP_CONFLICT = 409; // blocked → CTA fragment
    const HTTP_PRECONDITION_FAILED = 412; // stale → frontier view
    const HTTP_ERROR_MIN = 400;
    const CTA_MARKER = 'id="' + ADVANCE_FORM_ID + '"';
    const VIEW_MARKER = 'id="patient-view"';
    // Keeps id="advance-hint": the blocked button's aria-describedby points at it.
    const FAILED_HINT_HTML =
        '<p id="advance-hint" class="advance-hint is-error" role="alert">Could not advance — retry</p>';

    function isAdvanceForm(el) {
        return !!(el && el.id === ADVANCE_FORM_ID);
    }

    function requester(e) {
        return e.detail && e.detail.requestConfig && e.detail.requestConfig.elt;
    }

    function writeFailedHint() {
        const form = document.getElementById(ADVANCE_FORM_ID);
        if (!form) return;
        const hint = form.querySelector(ADVANCE_HINT_SELECTOR);
        if (hint) {
            hint.outerHTML = FAILED_HINT_HTML;
        } else {
            form.insertAdjacentHTML("beforeend", FAILED_HINT_HTML);
        }
    }

    function prefersReducedMotion() {
        return !!(
            window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches
        );
    }

    function scrollToFirstUnanswered() {
        const form = document.getElementById(ADVANCE_FORM_ID);
        const qid = form && form.dataset.firstUnanswered;
        if (!qid) return;
        // Attribute selector: question ids may start with a digit (S9a R4).
        const question = document.querySelector(
            QUESTION_FORM_SELECTOR + '[data-question-id="' + qid + '"]'
        );
        if (!question) return;

        question.scrollIntoView({
            block: "center",
            behavior: prefersReducedMotion() ? "auto" : "smooth",
        });
        const control = question.querySelector(FIRST_CONTROL_SELECTOR);
        if (control) control.focus({ preventScroll: true });

        question.classList.add(HIGHLIGHT_CLASS);
        clearTimeout(question._highlightTimer);
        question._highlightTimer = setTimeout(function () {
            question.classList.remove(HIGHLIGHT_CLASS);
        }, HIGHLIGHT_MS);
    }

    document.body.addEventListener("htmx:configRequest", function (e) {
        if (!isAdvanceForm(e.detail.elt)) return;
        e.detail.parameters.client_ts = new Date().toISOString();
        e.detail.parameters.client_seq = window.ehrsim.nextClientSeq();
    });

    document.body.addEventListener("htmx:beforeSwap", function (e) {
        if (!isAdvanceForm(requester(e))) return;
        const xhr = e.detail.xhr;
        if (!xhr || xhr.status < HTTP_ERROR_MIN) return;
        const body = xhr.responseText || "";
        const blocked = xhr.status === HTTP_CONFLICT && body.indexOf(CTA_MARKER) !== -1;
        const stale = xhr.status === HTTP_PRECONDITION_FAILED && body.indexOf(VIEW_MARKER) !== -1;
        if (blocked || stale) {
            e.detail.shouldSwap = true;
            return;
        }
        e.detail.shouldSwap = false;
        writeFailedHint();
    });

    document.body.addEventListener("htmx:afterSwap", function (e) {
        if (!isAdvanceForm(requester(e))) return;
        const xhr = e.detail.xhr;
        if (!xhr || xhr.status !== HTTP_CONFLICT) return;
        scrollToFirstUnanswered();
    });

    document.body.addEventListener("htmx:sendError", function (e) {
        if (isAdvanceForm(requester(e))) writeFailedHint();
    });
})();
