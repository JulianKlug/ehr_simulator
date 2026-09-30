// telemetry.js — S11j/S11k browser telemetry (pinned htmx.org@2.0.4).
//
// Reports raw facts about the rendered #patient-view to POST
// /telemetry/events. The server derives foreground/active time and panel
// exposure from them; nothing is computed or thresholded here.
//
//   render attach ──► browser.timepoint_enter {visible, focused}
//                     panel.mount × panels, panel.viewport (observer)
//   while attached ─► browser.state (changes + every PERIODIC_STATE_MS
//                     while foreground), browser.activity (throttled),
//                     panel.open / panel.close (user tab changes)
//   render detach ──► browser.timepoint_exit {swap|pagehide}, flush
//
// A view without data-render-id (telemetry off, or the write-free 412
// view) is never reported. Monotonic times (performance.now()) are only
// compared within one render: one render lives in one document.
//
// Activity throttling keeps the leading and the trailing activity of every
// ACTIVITY_THROTTLE_MS window, each stamped with its own time. The window
// is far shorter than any inactivity threshold, so the union of activity
// windows is unchanged. A state change flushes the pending sample first,
// so one window never spans two foreground states.
//
// S11m: a guarded render's events wait until case_tab_guard.js reports its
// claim granted (window.ehrsim.tabState); a refused render's events are
// dropped unreported — the server would refuse them anyway.
//
// Never recorded: key values, text, answer values, coordinates.
// No inline JS anywhere (CSP script-src 'self').

(function () {
    "use strict";

    const VIEW_ID = "patient-view";
    const PANE_ID = "questions-pane";
    const PANEL_SELECTOR = "section[data-panel]";
    const CONTENT_SELECTOR = "[data-panel-content]";
    const TABPANEL_SELECTOR = '[role="tabpanel"]';
    const TABCHANGE_EVENT = "ehrsim:tabchange";
    const TABCHANGE_USER = "user";

    const FLUSH_INTERVAL_MS = 5000;
    const PERIODIC_STATE_MS = 15000;
    const ACTIVITY_THROTTLE_MS = 1000;
    const MAX_BATCH = 100; // mirrors MAX_BATCH_EVENTS server side
    const MAX_QUEUE = 1000;
    const HTTP_CLIENT_ERROR_MIN = 400;
    const HTTP_SERVER_ERROR_MIN = 500;
    const FULL_RATIO = 1;
    const TAB_PENDING = "pending";
    const TAB_REFUSED = "refused";
    const TAB_UNGUARDED = "unguarded";

    let queue = [];
    let dropped = {}; // render_id -> count of discarded events
    let sending = false;
    let current = null; // the attached render

    // --- identity -------------------------------------------------------

    function readTabId() {
        return window.ehrsim.tabId(); // client_seq.js: one id per tab
    }

    function tabState(renderId) {
        const lookup = window.ehrsim && window.ehrsim.tabState;
        return lookup ? lookup(renderId) : TAB_UNGUARDED;
    }

    // --- queue + delivery -----------------------------------------------

    function enqueue(kind, payload, mono) {
        if (!current) return;
        queue.push({
            kind: kind,
            render_id: current.renderId,
            client_seq: window.ehrsim.nextClientSeq(),
            client_mono_ms: mono === undefined ? performance.now() : mono,
            client_ts: new Date().toISOString(),
            payload: payload,
        });
        if (queue.length > MAX_QUEUE) discard(queue.splice(0, queue.length - MAX_QUEUE));
        if (queue.length >= MAX_BATCH) flush();
    }

    function discard(events) {
        events.forEach(function (e) {
            if (e.kind === "browser.gap") return; // never report a lost report
            dropped[e.render_id] = (dropped[e.render_id] || 0) + 1;
        });
    }

    function gapEvents() {
        // One browser.gap per render that lost events; its time is not used.
        const gaps = Object.keys(dropped).map(function (renderId) {
            return {
                kind: "browser.gap",
                render_id: renderId,
                client_seq: window.ehrsim.nextClientSeq(),
                client_mono_ms: performance.now(),
                client_ts: new Date().toISOString(),
                payload: { dropped: dropped[renderId] },
            };
        });
        dropped = {};
        return gaps;
    }

    function dropRefused() {
        queue = queue.filter(function (e) {
            return tabState(e.render_id) !== TAB_REFUSED;
        });
        Object.keys(dropped).forEach(function (renderId) {
            if (tabState(renderId) === TAB_REFUSED) delete dropped[renderId];
        });
    }

    function takeBatch() {
        // Pending renders (claim in flight) stay queued, in order.
        dropRefused();
        const waitingGaps = {};
        Object.keys(dropped).forEach(function (renderId) {
            if (tabState(renderId) !== TAB_PENDING) return;
            waitingGaps[renderId] = dropped[renderId];
            delete dropped[renderId];
        });
        const ready = [];
        const waiting = [];
        queue.forEach(function (e) {
            (tabState(e.render_id) === TAB_PENDING ? waiting : ready).push(e);
        });
        const batch = gapEvents().concat(ready.splice(0, MAX_BATCH));
        const overflow = batch.splice(MAX_BATCH);
        queue = overflow.concat(ready, waiting);
        Object.assign(dropped, waitingGaps);
        return batch;
    }

    function body(batch) {
        return JSON.stringify({ tab_id: readTabId(), events: batch });
    }

    function url() {
        return (current && current.url) || lastUrl;
    }

    let lastUrl = null;

    function flush() {
        if (sending || (queue.length === 0 && Object.keys(dropped).length === 0)) return;
        const target = url();
        if (!target) return;
        const batch = takeBatch();
        if (!batch.length) return; // everything waits for a tab claim
        sending = true;
        fetch(target, {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: body(batch),
            keepalive: true,
        })
            .then(function (response) {
                const status = response.status;
                if (response.redirected) {
                    discard(batch); // never a success: the server answers 204
                } else if (status >= HTTP_SERVER_ERROR_MIN) {
                    queue = batch.filter(notGap).concat(queue); // retry later
                } else if (status >= HTTP_CLIENT_ERROR_MIN) {
                    discard(batch); // refused: report the loss, never resend
                }
            })
            .catch(function () {
                queue = batch.filter(notGap).concat(queue);
            })
            .finally(function () {
                sending = false;
            });
    }

    function notGap(e) {
        if (e.kind !== "browser.gap") return true;
        dropped[e.render_id] = (dropped[e.render_id] || 0) + e.payload.dropped;
        return false;
    }

    function flushBeacon() {
        // pagehide: fetch may be cancelled, sendBeacon survives the unload.
        const target = url();
        if (!target || !navigator.sendBeacon) {
            flush();
            return;
        }
        while (queue.length || Object.keys(dropped).length) {
            const batch = takeBatch();
            if (!batch.length) return; // unclaimed renders: nothing may go
            const blob = new Blob([body(batch)], { type: "application/json" });
            if (!navigator.sendBeacon(target, blob)) {
                discard(batch);
                return;
            }
        }
    }

    // --- browser state --------------------------------------------------

    function isVisible() {
        return document.visibilityState === "visible";
    }

    function isFocused() {
        return document.hasFocus();
    }

    function onStateChange(reason) {
        if (!current) return;
        const visible = isVisible();
        const focused = isFocused();
        if (visible === current.visible && focused === current.focused) return;
        sendPendingActivity();
        current.visible = visible;
        current.focused = focused;
        current.lastActivityMono = null; // the next activity is sent at once
        enqueue("browser.state", { visible: visible, focused: focused, reason: reason });
    }

    function onPeriodic() {
        if (!current || !current.visible || !current.focused) return;
        enqueue("browser.state", {
            visible: current.visible,
            focused: current.focused,
            reason: "periodic",
        });
    }

    // --- activity -------------------------------------------------------

    function sendPendingActivity() {
        if (!current || !current.pendingActivity) return;
        const pending = current.pendingActivity;
        current.pendingActivity = null;
        clearTimeout(current.trailingTimer);
        current.lastActivityMono = pending.mono;
        enqueue("browser.activity", { activity_kind: pending.kind }, pending.mono);
    }

    function onActivity(kind) {
        if (!current) return;
        const now = performance.now();
        const last = current.lastActivityMono;
        if (last === null || now - last >= ACTIVITY_THROTTLE_MS) {
            current.lastActivityMono = now;
            enqueue("browser.activity", { activity_kind: kind }, now);
            return;
        }
        if (!current.pendingActivity) {
            current.trailingTimer = setTimeout(sendPendingActivity, last + ACTIVITY_THROTTLE_MS - now);
        }
        current.pendingActivity = { kind: kind, mono: now };
    }

    // --- panels (S11k) --------------------------------------------------

    function panelExpanded(section) {
        const tabpanel = section.closest(TABPANEL_SELECTOR);
        return !tabpanel || !tabpanel.hidden;
    }

    function attachPanels(view) {
        const threshold = Number(view.dataset.viewportThreshold);
        const byContent = new Map();
        view.querySelectorAll(PANEL_SELECTOR).forEach(function (section) {
            enqueue("panel.mount", {
                panel_id: section.dataset.panel,
                expanded: panelExpanded(section),
                collapsible: !!section.closest(TABPANEL_SELECTOR),
                state: section.dataset.state,
            });
            const content = section.querySelector(CONTENT_SELECTOR);
            if (content) byContent.set(content, section.dataset.panel);
        });
        if (!byContent.size || !("IntersectionObserver" in window)) return;

        const observer = new IntersectionObserver(
            function (entries) {
                entries.forEach(function (entry) {
                    enqueue("panel.viewport", {
                        panel_id: byContent.get(entry.target),
                        intersection_ratio: Math.min(FULL_RATIO, entry.intersectionRatio),
                    });
                });
            },
            { threshold: [0, threshold, FULL_RATIO] }
        );
        byContent.forEach(function (_id, content) {
            observer.observe(content);
        });
        current.observer = observer;
    }

    function onTabChange(e) {
        const detail = e.detail || {};
        if (!current || detail.source !== TABCHANGE_USER) return;
        if (detail.from) enqueue("panel.close", { panel_id: detail.from });
        if (detail.to) enqueue("panel.open", { panel_id: detail.to });
    }

    // --- render lifecycle -----------------------------------------------

    function attach() {
        const view = document.getElementById(VIEW_ID);
        if (!view || !view.dataset.renderId || !window.ehrsim) return;
        readTabId(); // the tab's id exists from its first reported render
        current = {
            renderId: view.dataset.renderId,
            url: view.dataset.telemetryUrl,
            visible: isVisible(),
            focused: isFocused(),
            lastActivityMono: null,
            pendingActivity: null,
            trailingTimer: null,
            observer: null,
        };
        lastUrl = current.url;
        const enterMono = performance.now();
        enqueue(
            "browser.timepoint_enter",
            { visible: current.visible, focused: current.focused },
            enterMono
        );
        current.lastActivityMono = enterMono; // entry is the navigation activity
        attachPanels(view);
    }

    function detach(reason) {
        if (!current) return;
        sendPendingActivity();
        if (current.observer) current.observer.disconnect();
        enqueue("browser.timepoint_exit", { reason: reason });
        current = null;
    }

    function isViewSwap(e) {
        const target = e.detail && e.detail.target;
        return !!(target && target.id === VIEW_ID);
    }

    document.body.addEventListener("htmx:beforeSwap", function (e) {
        if (!isViewSwap(e) || !e.detail.shouldSwap) return;
        detach("swap");
        flush();
    });

    document.body.addEventListener("htmx:afterSwap", function (e) {
        if (isViewSwap(e)) attach();
    });

    document.addEventListener("visibilitychange", function () {
        onStateChange("visibilitychange");
    });
    window.addEventListener("focus", function () {
        onStateChange("focus");
    });
    window.addEventListener("blur", function () {
        onStateChange("blur");
    });

    window.addEventListener("pagehide", function () {
        detach("pagehide");
        flushBeacon();
    });
    window.addEventListener("pageshow", function (e) {
        // A back/forward cache restore shows a render that already exited.
        if (e.persisted) window.location.reload();
    });

    document.addEventListener(
        "click",
        function () {
            onActivity("click");
        },
        true
    );
    document.addEventListener(
        "pointerdown",
        function (e) {
            if (e.pointerType === "touch" || e.pointerType === "pen") onActivity("touch");
        },
        true
    );
    document.addEventListener(
        "keydown",
        function () {
            onActivity("keyboard");
        },
        true
    );
    document.addEventListener(
        "scroll",
        function () {
            onActivity("scroll");
        },
        { capture: true, passive: true }
    );
    document.addEventListener(
        "wheel",
        function () {
            onActivity("scroll");
        },
        { capture: true, passive: true }
    );
    ["change", "input"].forEach(function (type) {
        document.addEventListener(
            type,
            function (e) {
                if (e.target.closest && e.target.closest("#" + PANE_ID)) onActivity("answer_change");
            },
            true
        );
    });
    document.addEventListener(TABCHANGE_EVENT, onTabChange);

    window.setInterval(onPeriodic, PERIODIC_STATE_MS);
    window.setInterval(flush, FLUSH_INTERVAL_MS);

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", attach);
    } else {
        attach();
    }
})();
