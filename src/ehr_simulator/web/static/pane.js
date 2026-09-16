// pane.js — pinned htmx.org@2.0.4
//
// The questions pane is a right-hand drawer (S9b feedback round 1). This
// file owns its open/collapsed state:
//
//   - the edge tab (.pane-tab) and the `q` key toggle it;
//   - the state lives in sessionStorage per tab and is re-applied after
//     every #patient-view swap (the aside is re-rendered by the server);
//   - the tab shows the CTA's remaining-count so the gate stays visible
//     while the drawer is closed.
//
// The collapsed class sits on #patient-view so CSS can move both the aside
// and the chrome margin from one hook. No inline JS (CSP script-src 'self').

(function () {
    "use strict";

    const STORAGE_KEY = "ehrsim:pane-collapsed";
    const COLLAPSED_CLASS = "pane-collapsed";
    const TOGGLE_KEY = "q";
    const VIEW_ID = "patient-view";
    const PANE_ID = "questions-pane";
    const CTA_ID = "advance-form";
    const TAB_SELECTOR = ".pane-tab";
    const COUNT_SELECTOR = ".pane-tab-count";
    const TOGGLE_ACTION_SELECTOR = '[data-action="toggle-pane"]';

    function readCollapsed() {
        try {
            return sessionStorage.getItem(STORAGE_KEY) === "1";
        } catch (err) {
            return false;
        }
    }

    function writeCollapsed(collapsed) {
        try {
            if (collapsed) {
                sessionStorage.setItem(STORAGE_KEY, "1");
            } else {
                sessionStorage.removeItem(STORAGE_KEY);
            }
        } catch (err) {
            // sessionStorage unavailable — state lives in the DOM only.
        }
    }

    function apply(collapsed) {
        const view = document.getElementById(VIEW_ID);
        const pane = document.getElementById(PANE_ID);
        const tab = document.querySelector(TAB_SELECTOR);
        if (!view || !pane || !tab) return;
        view.classList.toggle(COLLAPSED_CLASS, collapsed);
        tab.setAttribute("aria-expanded", String(!collapsed));
        pane.setAttribute("aria-hidden", String(collapsed));
        // inert keeps a hidden drawer out of the tab order.
        if (collapsed) {
            pane.setAttribute("inert", "");
        } else {
            pane.removeAttribute("inert");
        }
    }

    function updateCount() {
        const count = document.querySelector(COUNT_SELECTOR);
        if (!count) return;
        const cta = document.getElementById(CTA_ID);
        if (!cta) {
            count.hidden = true; // locked / completed pane: nothing to answer
            return;
        }
        count.textContent = cta.dataset.remaining;
        count.hidden = false;
    }

    function toggle() {
        const collapsed = !readCollapsed();
        writeCollapsed(collapsed);
        apply(collapsed);
    }

    function sync() {
        apply(readCollapsed());
        updateCount();
    }

    function isEditable(el) {
        const shared = window.ehrsim && window.ehrsim.isEditable;
        return shared ? shared(el) : false;
    }

    document.addEventListener("click", function (e) {
        if (e.target.closest && e.target.closest(TOGGLE_ACTION_SELECTOR)) toggle();
    });

    document.addEventListener("keydown", function (e) {
        if (e.key !== TOGGLE_KEY || e.metaKey || e.ctrlKey || e.altKey) return;
        if (isEditable(e.target)) return;
        if (!document.querySelector(TAB_SELECTOR)) return;
        e.preventDefault();
        toggle();
    });

    document.body.addEventListener("htmx:afterSwap", function (e) {
        const target = e.detail && e.detail.target;
        if (target && target.id === VIEW_ID) {
            sync();
            return;
        }
        updateCount(); // the OOB CTA behind every /answer 200
    });

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", sync);
    } else {
        sync();
    }
})();
