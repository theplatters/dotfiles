// ApprovalGrammar.js — one preview/approval grammar shared by the palette
// approval dialog and the planner inline approval (S-059, completing the
// S-024 bounded exact-preview pattern across surfaces).
//
// PURE derivation helpers with no QML context (the AmbientContext.js
// shared-helper precedent): every input arrives as an argument, nothing
// touches root/Qt/Quickshell. QML surfaces keep thin wrappers with the
// same names and delegate here, so the same request renders the same
// preview, consequence, and destination on every surface; only the chrome
// (layout, sizing, focus restore) stays per-surface.
//
// Surface-specific context (the planner's sessionSwitching picker) arrives
// as an optional `options` argument — { sessionSwitching, journalMode } —
// never as a fork of the grammar.
//
// Consequence/footer truth (S-048): Defer/closing parks the request
// queued — it never expires while parked, it never reopens on its own
// while the view stays open (same-id suppression per surface session),
// and it is still pending when the user comes back to it; Reject
// cancels it; Stop cancels everything.
//
// Callers: widgets/PaletteApprovalDialog.qml, widgets/ProjectPlanner.qml
// (as ApprovalGrammar), and tests via node.

// Bound untrusted text: strip controls, collapse whitespace, trim, cap.
// The cap appends "…" (single char); the default cap is 240.
function boundedText(value, limit) {
    var text = "";
    try {
        if (value !== undefined && value !== null) text = String(value);
    } catch (error) {
        text = "";
    }
    text = text.replace(/[\u0000-\u001F\u007F]/g, " ").replace(/\s+/g, " ").trim();
    var cap = limit || 240;
    if (text.length > cap) text = text.substring(0, Math.max(0, cap - 1)) + "…";
    return text;
}

// Specific titles: prefer the request's own bounded title, else name the
// method's effect. Never a bare "Approval". The planner's session-picker
// context keeps its "Restore session" title via options.sessionSwitching.
function titleFor(value, options) {
    if (!value) return "Approval";
    var switching = !!(options && options.sessionSwitching);
    if (switching && value.method === "select") {
        var picked = "";
        try {
            if (value.title !== undefined && value.title !== null)
                picked = String(value.title);
        } catch (error) {
            picked = "";
        }
        return boundedText(picked || "Restore session", 160);
    }
    var named = "";
    try {
        if (value.title !== undefined && value.title !== null)
            named = String(value.title);
    } catch (error) {
        named = "";
    }
    named = boundedText(named, 80);
    if (named) return named;
    if (value.method === "select") return "Choose an option";
    if (value.method === "confirm") return "Confirm requested action";
    if (value.method === "editor") return "Edit and submit response";
    if (value.method === "input") return "Provide a response";
    return "Pi requests a response";
}

// Headline message: the request's own message/title/prompt, else the
// explicit-response fallback. The planner's session picker names its
// destination via options.{sessionSwitching, journalMode}.
function messageFor(value, options) {
    if (!value) return "";
    var switching = !!(options && options.sessionSwitching);
    if (switching && value.method === "select") {
        var journal = !!(options && options.journalMode);
        return journal ? "Choose a saved journal session." : "Choose a saved session for this project.";
    }
    return value.message || value.title || value.prompt ||
        "Pi is requesting an explicit response.";
}

// Consequence line: what Accept/Reject do, and that Defer/closing parks
// the request queued (never expires while parked, never reopens on its
// own while the view stays open — same-id suppression — and still
// pending when the user comes back to it). Only Reject cancels it and
// Stop cancels everything.
function consequenceFor(value) {
    if (!value) return "";
    if (value.method === "confirm")
        return "Accept runs the requested action · Reject cancels it · Defer or closing parks it queued — it never expires, it will not reopen on its own while this view stays open, and it is still pending when you come back to it";
    if (value.method === "select")
        return "Accept sends the highlighted option · Reject cancels it · Defer or closing parks it queued — it never expires, it will not reopen on its own while this view stays open, and it is still pending when you come back to it";
    return "Accept submits the text · Reject discards it · Defer or closing parks it queued — it never expires, it will not reopen on its own while this view stays open, and it is still pending when you come back to it";
}

// Destination / what-will-run line: bounded, never a raw dump. The
// highlighted select option arrives as an explicit index argument (the
// palette passes its choiceIndex, the planner its approval.choiceIndex).
function destinationFor(value, choiceIndex) {
    if (!value) return "";
    if (value.method === "select") {
        var options = value.options || [];
        var current = "";
        try {
            var at = Number(choiceIndex) || 0;
            current = boundedText(options[at] || options[0], 120);
        } catch (error) {
            current = "";
        }
        var count = 0;
        try {
            count = options.length;
        } catch (error) {
            count = 0;
        }
        return "Options: " + count + (current ? " · highlighted: " + current : "");
    }
    if (value.method === "confirm")
        return "Will run: " + boundedText(value.message || value.title || value.prompt, 160);
    var prefill = "";
    try {
        prefill = boundedText(value.prefill, 160);
    } catch (error) {
        prefill = "";
    }
    return prefill ? "Draft: " + prefill : "Submits the text entered below";
}

// Bounded exact-preview excerpt: consequence-relevant fields only, capped
// for display (1200 chars). Never a raw JSON dump of args. The planner's
// session picker shows only its option list, so it previews as "" via
// options.sessionSwitching.
function argumentPreview(value, options) {
    if (!value) return "";
    var switching = !!(options && options.sessionSwitching);
    if (switching && value.method === "select") return "";
    var parts = [];
    var push = function (label, text) {
        var clean = boundedText(text, 240);
        if (clean) parts.push(label + ": " + clean);
    };
    try {
        push("Message", value.message);
        push("Title", value.title);
        push("Prompt", value.prompt);
        if (value.method === "select") {
            var selectOptions = value.options || [];
            var shown = [];
            for (var i = 0; i < Math.min(6, selectOptions.length); ++i)
                shown.push(boundedText(selectOptions[i], 80));
            if (shown.length) parts.push("Options: " + shown.join(" | "));
            if (selectOptions.length > shown.length)
                parts.push("…and " + (selectOptions.length - shown.length) + " more");
        } else if (value.method === "input" || value.method === "editor") {
            push("Prefill", value.prefill);
            push("Placeholder", value.placeholder);
        } else {
            var args = value.arguments !== undefined ? value.arguments : value.input;
            if (typeof args === "string") push("Details", args);
            else if (args && typeof args === "object") {
                var keys = Object.keys(args).slice(0, 8);
                var details = [];
                for (var k = 0; k < keys.length; ++k)
                    details.push(keys[k] + "=" + boundedText(args[keys[k]], 80));
                if (details.length) parts.push("Details: " + details.join(", "));
            }
        }
    } catch (error) {}
    return parts.join("\n").substring(0, 1200);
}

// Pure choice clamping for select-method cursor movement: returns the
// clamped index; callers position their own view. Count 0 stays at 0.
function moveChoice(delta, index, count) {
    var at = Number(index) || 0;
    var total = Number(count) || 0;
    var step = Number(delta) || 0;
    return Math.max(0, Math.min(total - 1, at + step));
}

// Node/test entry point only: QML exposes the top-level functions above
// natively on import. Guarded so the QML engine never touches `module`.
try {
    if (typeof module !== "undefined" && module && module.exports) {
        module.exports = {
            boundedText: boundedText,
            titleFor: titleFor,
            messageFor: messageFor,
            consequenceFor: consequenceFor,
            destinationFor: destinationFor,
            argumentPreview: argumentPreview,
            moveChoice: moveChoice
        };
    }
} catch (exportError) {}
