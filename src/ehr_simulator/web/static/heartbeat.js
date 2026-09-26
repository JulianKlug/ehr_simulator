// heartbeat.js — S11e case heartbeat.
//
// While a measured case page is open, POST /case/{pid}/heartbeat every
// data-heartbeat-interval-ms (rendered from HEARTBEAT_INTERVAL_SECONDS), and
// once more whenever the tab becomes visible again. The anchor lives in the
// questions pane, so it is re-read on every tick: htmx swaps replace it.
// A 409 carries HX-Redirect: the case is closed or paused — leave the page.
//
// No inline JS anywhere (CSP script-src 'self').

(function () {
    "use strict";

    const ANCHOR_SELECTOR = "#case-heartbeat";
    const HTTP_CONFLICT = 409;
    const REDIRECT_HEADER = "HX-Redirect";

    let timer = null;

    function anchor() {
        return document.querySelector(ANCHOR_SELECTOR);
    }

    function beat() {
        const el = anchor();
        if (!el) return;
        fetch(el.dataset.heartbeatUrl, { method: "POST", credentials: "same-origin" })
            .then(function (response) {
                if (response.status !== HTTP_CONFLICT) return;
                const target = response.headers.get(REDIRECT_HEADER);
                if (target) window.location.assign(target);
            })
            .catch(function () {
                // A lost beat is harmless: the grace period absorbs it.
            });
    }

    function start() {
        const el = anchor();
        if (timer !== null || !el) return;
        timer = window.setInterval(beat, Number(el.dataset.heartbeatIntervalMs));
    }

    document.addEventListener("DOMContentLoaded", start);
    document.addEventListener("htmx:afterSwap", start);
    document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "visible") beat();
    });
})();
