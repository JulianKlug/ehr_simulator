// case_tab_guard.js — S11m one tab per active measured case (pinned htmx.org@2.0.4).
//
// A guarded #patient-view (data-tab-claim-url) must hold the case lease
// before it may write:
//
//   render ──► pending ──POST claim {tab_id, render_id}──► 204 granted
//                                                     └──► 409 refused
//   granted: every htmx request carries X-Ehrsim-Tab-Id / X-Ehrsim-Render-Id,
//            heartbeat.js asks tabHeaders(), the pause form gets hidden fields
//   refused: blocking notice, answer/advance/pause cancelled; Retry, focus
//            and becoming visible claim again
//   pagehide ──► sendBeacon release (the server ignores a stale render's)
//
// telemetry.js asks tabState(renderId) and holds pending renders' events.
// A second tab can briefly show the case before its claim is refused; it
// never writes and never reports. No inline JS (CSP script-src 'self').

(function () {
    "use strict";

    const VIEW_ID = "patient-view";
    const PAUSE_FORM_SELECTOR = "form.case-pause";
    const NOTICE_CLASS = "tab-conflict";
    const RETRY_ACTION = "tab-retry";
    const TAB_HEADER = "X-Ehrsim-Tab-Id";
    const RENDER_HEADER = "X-Ehrsim-Render-Id";
    const TAB_FIELD = "ehrsim_tab_id";
    const RENDER_FIELD = "ehrsim_render_id";
    const CONFLICT_HEADER = "X-Ehrsim-Tab";
    const CONFLICT_EVENT = "ehrsim:tabconflict";
    const HTTP_NO_CONTENT = 204;
    const HTTP_CONFLICT = 409;
    const RETRY_MS = 5000;

    const PENDING = "pending";
    const GRANTED = "granted";
    const REFUSED = "refused";
    const UNGUARDED = "unguarded";

    const states = {}; // render_id -> state
    let retryTimer = null;

    function guardedView() {
        const view = document.getElementById(VIEW_ID);
        return view && view.dataset.tabClaimUrl && view.dataset.renderId ? view : null;
    }

    function tabState(renderId) {
        return states[renderId] || UNGUARDED;
    }

    function tabHeaders() {
        const view = guardedView();
        if (!view) return {};
        const headers = {};
        headers[TAB_HEADER] = window.ehrsim.tabId();
        headers[RENDER_HEADER] = view.dataset.renderId;
        return headers;
    }

    // --- UI -------------------------------------------------------------

    function notice(view) {
        let el = view.querySelector("." + NOTICE_CLASS);
        if (el) return el;
        el = document.createElement("div");
        el.className = NOTICE_CLASS;
        el.setAttribute("role", "alert");
        el.appendChild(
            document.createTextNode(
                "This case is open in another tab or window. Close it there, then press Retry."
            )
        );
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.action = RETRY_ACTION;
        button.textContent = "Retry";
        el.appendChild(button);
        view.insertBefore(el, view.firstChild);
        return el;
    }

    function apply(view) {
        const state = tabState(view.dataset.renderId);
        view.dataset.tabState = state;
        const existing = view.querySelector("." + NOTICE_CLASS);
        if (state === REFUSED) {
            notice(view).hidden = false;
        } else if (existing) {
            existing.hidden = true;
        }
    }

    function setState(renderId, state) {
        states[renderId] = state;
        const view = guardedView();
        if (view && view.dataset.renderId === renderId) apply(view);
    }

    // --- claim / release ------------------------------------------------

    function scheduleRetry() {
        if (retryTimer !== null) return;
        retryTimer = window.setTimeout(function () {
            retryTimer = null;
            const view = guardedView();
            if (view && tabState(view.dataset.renderId) === PENDING) claim(view);
        }, RETRY_MS);
    }

    function claim(view) {
        const renderId = view.dataset.renderId;
        if (tabState(renderId) !== GRANTED) setState(renderId, PENDING);
        fetch(view.dataset.tabClaimUrl, {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tab_id: window.ehrsim.tabId(), render_id: renderId }),
        })
            .then(function (response) {
                if (response.status === HTTP_NO_CONTENT) {
                    setState(renderId, GRANTED);
                } else if (response.status === HTTP_CONFLICT) {
                    setState(renderId, REFUSED);
                } else {
                    scheduleRetry(); // server trouble: stay pending, try again
                }
            })
            .catch(scheduleRetry);
    }

    function release() {
        const view = guardedView();
        if (!view || tabState(view.dataset.renderId) !== GRANTED || !navigator.sendBeacon) return;
        const body = JSON.stringify({
            tab_id: window.ehrsim.tabId(),
            render_id: view.dataset.renderId,
            reason: "pagehide",
        });
        navigator.sendBeacon(
            view.dataset.tabReleaseUrl,
            new Blob([body], { type: "application/json" })
        );
    }

    function attach() {
        const view = guardedView();
        if (!view) return;
        const state = tabState(view.dataset.renderId);
        if (state === UNGUARDED) states[view.dataset.renderId] = PENDING;
        apply(view);
        claim(view); // idempotent when the advance already moved the lease
    }

    function reclaimIfRefused() {
        const view = guardedView();
        if (view && tabState(view.dataset.renderId) === REFUSED) claim(view);
    }

    // --- writes ---------------------------------------------------------

    function isWrite(detail) {
        return (detail.verb || "").toLowerCase() === "post";
    }

    document.body.addEventListener("htmx:configRequest", function (e) {
        const view = guardedView();
        if (!view || !isWrite(e.detail)) return;
        if (tabState(view.dataset.renderId) !== GRANTED) {
            e.preventDefault(); // no write before (or after losing) the lease
            return;
        }
        Object.assign(e.detail.headers, tabHeaders());
    });

    document.body.addEventListener("htmx:afterRequest", function (e) {
        const xhr = e.detail && e.detail.xhr;
        if (!xhr || xhr.status !== HTTP_CONFLICT || !xhr.getResponseHeader(CONFLICT_HEADER)) return;
        const view = guardedView();
        if (view) setState(view.dataset.renderId, REFUSED);
    });

    document.addEventListener(CONFLICT_EVENT, function () {
        const view = guardedView();
        if (view) setState(view.dataset.renderId, REFUSED);
    });

    document.addEventListener(
        "submit",
        function (e) {
            const form = e.target;
            const view = guardedView();
            if (!view || !form.matches || !form.matches(PAUSE_FORM_SELECTOR)) return;
            if (tabState(view.dataset.renderId) !== GRANTED) {
                e.preventDefault();
                return;
            }
            [
                [TAB_FIELD, window.ehrsim.tabId()],
                [RENDER_FIELD, view.dataset.renderId],
            ].forEach(function (pair) {
                let input = form.querySelector('input[name="' + pair[0] + '"]');
                if (!input) {
                    input = document.createElement("input");
                    input.type = "hidden";
                    input.name = pair[0];
                    form.appendChild(input);
                }
                input.value = pair[1];
            });
        },
        true
    );

    document.addEventListener("click", function (e) {
        const target = e.target;
        if (target && target.dataset && target.dataset.action === RETRY_ACTION) {
            const view = guardedView();
            if (view) claim(view);
        }
    });

    // --- lifecycle ------------------------------------------------------

    document.body.addEventListener("htmx:afterSwap", function (e) {
        const target = e.detail && e.detail.target;
        if (target && target.id === VIEW_ID) attach();
    });
    window.addEventListener("focus", reclaimIfRefused);
    document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "visible") reclaimIfRefused();
    });
    window.addEventListener("pagehide", release);

    window.ehrsim = window.ehrsim || {};
    window.ehrsim.tabState = tabState;
    window.ehrsim.tabHeaders = tabHeaders;

    // Deferred scripts run after parsing: the view exists, and this runs
    // before telemetry.js attaches, so its first events already wait.
    attach();
})();
