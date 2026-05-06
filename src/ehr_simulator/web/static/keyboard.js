// keyboard.js — pinned htmx.org@2.0.4
//
// Keyboard shortcuts for the simulator:
//   ] = next timepoint
//   [ = previous timepoint
//   ? = toggle the shortcuts overlay
//
// Shortcuts are ignored when focus is in an input/textarea/select/contenteditable.
// Out-of-range presses do NOT make the network request; they populate the
// summary-card flash slot instead.

(function () {
    "use strict";

    function isEditable(el) {
        if (!el) return false;
        const tag = (el.tagName || "").toLowerCase();
        if (tag === "input" || tag === "textarea" || tag === "select") return true;
        if (el.isContentEditable) return true;
        return false;
    }

    function readPatientView() {
        const view = document.getElementById("patient-view");
        if (!view) return null;
        return {
            view: view,
            patientId: view.dataset.patientId,
            tIndex: parseInt(view.dataset.tIndex, 10),
            tCount: parseInt(view.dataset.tCount, 10),
            chrome: view.dataset.chrome || "dense",
        };
    }

    function showFlash(message) {
        const slot = document.getElementById("summary-flash");
        if (!slot) return;
        slot.textContent = message;
        slot.classList.add("flash-warn");
        clearTimeout(slot._flashTimer);
        slot._flashTimer = setTimeout(function () {
            slot.textContent = "";
            slot.classList.remove("flash-warn");
        }, 2000);
    }

    function navigate(deltaIndex) {
        const state = readPatientView();
        if (!state) return;
        const next = state.tIndex + deltaIndex;
        if (next < 0) {
            showFlash("Already at first timepoint");
            return;
        }
        if (next >= state.tCount) {
            showFlash("Already at last timepoint");
            return;
        }
        const url =
            "/patient/" +
            encodeURIComponent(state.patientId) +
            "/timepoint/" +
            next +
            "?chrome=" +
            encodeURIComponent(state.chrome);
        if (window.htmx && typeof window.htmx.ajax === "function") {
            window.htmx.ajax("GET", url, { target: "#patient-view", swap: "outerHTML" });
        } else {
            window.location.href = url;
        }
    }

    function toggleOverlay() {
        const overlay = document.getElementById("shortcut-overlay");
        if (!overlay) return;
        overlay.open = !overlay.open;
    }

    function onKeyDown(e) {
        if (isEditable(e.target)) return;
        if (e.metaKey || e.ctrlKey || e.altKey) return;
        if (e.key === "]") {
            e.preventDefault();
            navigate(1);
        } else if (e.key === "[") {
            e.preventDefault();
            navigate(-1);
        } else if (e.key === "?") {
            e.preventDefault();
            toggleOverlay();
        }
    }

    // Tab persistence (FINDING-009): the epic-chrome template re-renders
    // with Admission selected after every htmx swap. Survive timepoint
    // navigation by remembering the active tab in sessionStorage and
    // re-applying it after each swap.
    const TAB_STORAGE_KEY = "ehrsim:active-tab";

    function getChromeKey() {
        const view = document.getElementById("patient-view");
        const chrome = view && view.dataset.chrome ? view.dataset.chrome : "default";
        return TAB_STORAGE_KEY + ":" + chrome;
    }

    function rememberActiveTab(tabName) {
        try {
            sessionStorage.setItem(getChromeKey(), tabName);
        } catch (err) {
            // sessionStorage unavailable (private mode, etc.) — soft-fail.
        }
    }

    function readRememberedTab() {
        try {
            return sessionStorage.getItem(getChromeKey());
        } catch (err) {
            return null;
        }
    }

    function activateTab(tab) {
        const tabs = tab.parentElement.querySelectorAll('[role="tab"]');
        const targetId = tab.getAttribute("aria-controls");
        tabs.forEach(function (t) {
            const isActive = t === tab;
            t.setAttribute("aria-selected", isActive ? "true" : "false");
        });
        const panels = document.querySelectorAll('[role="tabpanel"]');
        panels.forEach(function (p) {
            if (p.id === targetId) {
                p.removeAttribute("hidden");
            } else {
                p.setAttribute("hidden", "");
            }
        });
        const tabName = tab.dataset.tab;
        if (tabName) {
            rememberActiveTab(tabName);
        }
    }

    function restoreActiveTab() {
        const remembered = readRememberedTab();
        if (!remembered) return;
        const tab = document.querySelector(
            '[role="tab"][data-tab="' + remembered + '"]'
        );
        if (tab) activateTab(tab);
    }

    function onClick(e) {
        const tab = e.target.closest('[role="tab"]');
        if (tab) {
            e.preventDefault();
            activateTab(tab);
        }
    }

    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("click", onClick);
    // Restore on initial page load.
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", restoreActiveTab);
    } else {
        restoreActiveTab();
    }
    // Restore after every htmx swap that touches #patient-view.
    document.body.addEventListener("htmx:afterSwap", function (e) {
        const target = e.detail && e.detail.target;
        if (target && target.id === "patient-view") {
            restoreActiveTab();
        }
    });
})();
