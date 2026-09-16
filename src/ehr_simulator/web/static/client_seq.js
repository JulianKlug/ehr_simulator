// client_seq.js — one per-tab monotonic counter for every event-bearing POST.
//
// answers.js (answer autosaves) and advance.js (/advance) both stamp
// client_seq; S10 orders a tab's actions by it. A single counter keeps
// the two streams from colliding — including the in-memory fallback used
// when sessionStorage is blocked (private window), which two copies of
// this function could not share.
//
// Loaded before answers.js / advance.js (defer preserves order). Exposes
// window.ehrsim.nextClientSeq(). No inline JS (CSP script-src 'self').

(function () {
    "use strict";

    const SEQ_STORAGE_KEY = "ehrsim:client-seq";
    let memory = 0;

    function nextClientSeq() {
        let seq = memory;
        try {
            seq = parseInt(sessionStorage.getItem(SEQ_STORAGE_KEY) || "0", 10) || memory;
        } catch (err) {
            // sessionStorage unavailable — the in-memory counter carries on.
        }
        seq += 1;
        memory = seq;
        try {
            sessionStorage.setItem(SEQ_STORAGE_KEY, String(seq));
        } catch (err) {
            // soft-fail; memory already holds the value.
        }
        return seq;
    }

    window.ehrsim = window.ehrsim || {};
    window.ehrsim.nextClientSeq = nextClientSeq;
})();
