// answers.js — pinned htmx.org@2.0.4
//
// Client side of the S9a questions pane. Two jobs:
//
//   1. Stamp every answer POST with client_ts (ISO-8601) and client_seq
//      (per-tab monotonic counter from client_seq.js, shared with advance.js).
//   2. Let the badge fragment swap in on 4xx. htmx 2 skips swaps for
//      error statuses; the server always answers with the badge, so we
//      opt back in — but only when the body really is our fragment. A
//      5xx page or a dead network gets a locally built "Save failed" badge
//      instead of a silently stale one.
//
// No inline JS anywhere (CSP script-src 'self').

(function () {
    "use strict";

    const QUESTION_FORM_SELECTOR = "form.question";
    const BADGE_SELECTOR = ".answer-status";
    const FRAGMENT_MARKER = "data-state=";
    const HTTP_ERROR_MIN = 400;
    const FAILED_BADGE_HTML =
        '<span class="answer-status is-error" data-state="error" role="status" aria-live="polite">' +
        "Save failed — retry</span>";

    function isQuestionForm(el) {
        return !!(el && el.matches && el.matches(QUESTION_FORM_SELECTOR));
    }

    function writeFailedBadge(form) {
        const badge = form.querySelector(BADGE_SELECTOR);
        if (!badge) return;
        badge.outerHTML = FAILED_BADGE_HTML;
    }

    document.body.addEventListener("htmx:configRequest", function (e) {
        if (!isQuestionForm(e.detail.elt)) return;
        e.detail.parameters.client_ts = new Date().toISOString();
        e.detail.parameters.client_seq = window.ehrsim.nextClientSeq();
    });

    document.body.addEventListener("htmx:beforeSwap", function (e) {
        const form = e.detail.requestConfig && e.detail.requestConfig.elt;
        if (!isQuestionForm(form)) return;
        const xhr = e.detail.xhr;
        if (!xhr || xhr.status < HTTP_ERROR_MIN) return;
        if ((xhr.responseText || "").indexOf(FRAGMENT_MARKER) !== -1) {
            e.detail.shouldSwap = true;
            return;
        }
        e.detail.shouldSwap = false;
        writeFailedBadge(form);
    });

    document.body.addEventListener("htmx:sendError", function (e) {
        const form = e.detail.requestConfig && e.detail.requestConfig.elt;
        if (isQuestionForm(form)) writeFailedBadge(form);
    });
})();
