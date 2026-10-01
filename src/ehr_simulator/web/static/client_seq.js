// client_seq.js — one per-tab monotonic counter for every event-bearing POST.
//
// answers.js (answer autosaves) and advance.js (/advance) both stamp
// client_seq; S10 orders a tab's actions by it. A single counter keeps
// the two streams from colliding — including the in-memory fallback used
// when sessionStorage is blocked (private window), which two copies of
// this function could not share.
//
// S11m: also the one tab id (sessionStorage ehrsim:tab-id, UUID v4) that
// case_tab_guard.js claims with and telemetry.js reports under — one
// function, so the memory fallback cannot hand out two different ids.
//
// Loaded before answers.js / advance.js (defer preserves order). Exposes
// window.ehrsim.nextClientSeq() and window.ehrsim.tabId().
// No inline JS (CSP script-src 'self').

(function () {
    "use strict";

    const SEQ_STORAGE_KEY = "ehrsim:client-seq";
    const TAB_ID_KEY = "ehrsim:tab-id";
    let memory = 0;
    let tabIdMemory = null;

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

    function uuid4() {
        if (window.crypto && typeof window.crypto.randomUUID === "function") {
            return window.crypto.randomUUID();
        }
        const bytes = new Uint8Array(16);
        window.crypto.getRandomValues(bytes);
        bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
        bytes[8] = (bytes[8] & 0x3f) | 0x80; // RFC 4122 variant
        const hex = Array.prototype.map
            .call(bytes, function (b) {
                return (b + 0x100).toString(16).slice(1);
            })
            .join("");
        return (
            hex.slice(0, 8) + "-" + hex.slice(8, 12) + "-" + hex.slice(12, 16) + "-" +
            hex.slice(16, 20) + "-" + hex.slice(20)
        );
    }

    function tabId() {
        if (tabIdMemory) return tabIdMemory;
        try {
            tabIdMemory = sessionStorage.getItem(TAB_ID_KEY);
        } catch (err) {
            tabIdMemory = null; // storage blocked: the id lives in memory
        }
        if (!tabIdMemory) {
            tabIdMemory = uuid4();
            try {
                sessionStorage.setItem(TAB_ID_KEY, tabIdMemory);
            } catch (err) {
                // soft-fail; memory already holds the id.
            }
        }
        return tabIdMemory;
    }

    window.ehrsim = window.ehrsim || {};
    window.ehrsim.nextClientSeq = nextClientSeq;
    window.ehrsim.tabId = tabId;
})();
