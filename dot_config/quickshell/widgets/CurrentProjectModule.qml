import QtQuick
import QtQuick.Controls
import "../theme"

// Top-bar tray element showing the authoritative current working project.
// Source of truth is scripts/desktop_projects.py current-project (JSON
// {project:{id,name,matched_by}|null, registry, status,...}); window names
// or the planner's selected project are never inferred here. Both
// "associated" and "name-only-no-linkage" are valid identities (no Logseq
// link required). Click opens ProjectPlanner on the stable id via
// openProject(id,'',''); "No project" and backend-unavailable states open
// the plain project list with a blank id (never a stale id).
Item {
    id: root
    implicitWidth: projectButton.implicitWidth
    implicitHeight: 32
    width: implicitWidth
    height: 32

    // Injected by Bar (shell binds the shared ProjectPlanner), analogous to
    // calendarPopout/controlCenter.
    property var projectPlanner: null
    // Compact overview popup (shell-owned ProjectOverviewPopup, anchored to
    // the tray button). Valid-project clicks open it; no-project and
    // backend-unavailable states fall back to the plain project list.
    property var overviewPopup: null
    // Shared DailyAgenda injected by Bar for the session badge (count
    // of sessions needing attention). This module only reads the badge
    // count (agenda.ledgerInboxBadge) and calls the guarded refresh
    // (agenda.refreshLedgerInboxBadge()).
    property var agenda: null
    // Width-threshold compactness driven by Bar (bar.compactNetwork, a plain
    // bar.width threshold). Deliberately NOT tightSides: tightSides derives
    // from measured sideReserve, which includes this module, so reading it
    // here would feed back into the layout loop.
    property bool compact: false
    // Bounded label budget: full name stays in the tooltip.
    property int maxLabelWidth: compact ? 80 : 140

    // Shared shell-level source (S-005b): one poller per desktop, not per
    // monitor. Identity state binds here; this module keeps only the
    // button/popup/badge UI plus the pure parsers. No Process, no Timer.
    property var projectSource: null

    // Identity mirrors the shared source (local fallbacks keep the module
    // renderable with no source, e.g. headless tests).
    property string projectId: root.projectSource ? root.projectSource.projectId : ""
    property string projectName: root.projectSource ? root.projectSource.projectName : ""
    property string projectStatus: root.projectSource ? root.projectSource.projectStatus : ""
    property bool hasProject: root.projectSource ? root.projectSource.hasProject : false
    // False only when the backend call failed or its payload was invalid.
    // Distinct from hasProject===false, which is a valid "No project".
    property bool backendOk: root.projectSource ? root.projectSource.backendOk : true

    readonly property string displayLabel: {
        if (!root.backendOk) return "Project unavailable";
        if (root.hasProject) {
            if (root.projectName !== "") return root.projectName;
            return root.projectId;
        }
        return "No project";
    }
    readonly property string tooltipText: {
        if (!root.backendOk) return "Current project unavailable (backend error)";
        if (root.hasProject) {
            let full = root.projectName !== "" ? root.projectName : root.projectId;
            if (root.projectStatus !== "") return full + " (" + root.projectStatus + ")";
            return full;
        }
        return "No current project — open the project list";
    }
    readonly property int ledgerInboxBadge: root.parseLedgerInboxBadge(root.agenda ? root.agenda.ledgerInboxBadge : 0)

    // Pure payload parser (no root refs) so unit tests can exercise it via
    // node. code !== 0, empty, malformed, or unknown shapes => {ok:false}.
    // Valid identities: associated / name-only-no-linkage with a non-empty
    // project.id. Valid empties: unassociated / stale-removed with a null
    // project (stale is honestly "no current project" here; its reported id
    // is never reused for navigation).
    function parseCurrentProject(output, code) {
        if (code !== 0) return { ok: false };
        let data = null;
        try {
            data = JSON.parse(output || "");
        } catch (error) {
            return { ok: false };
        }
        if (!data || typeof data !== "object" || Array.isArray(data)) return { ok: false };
        if (typeof data.status !== "string") return { ok: false };
        let status = data.status;
        let project = data.hasOwnProperty("project") ? data.project : undefined;
        if (status === "associated" || status === "name-only-no-linkage") {
            if (!project || typeof project !== "object" || Array.isArray(project)) return { ok: false };
            // Strict identity types: a non-string id/name (object, number,
            // null) is malformed and must never coerce via String() into a
            // seemingly valid identity. Empty name is legitimate (the UI
            // falls back to the id); empty id is not.
            if (typeof project.id !== "string" || project.id === "") return { ok: false };
            if (typeof project.name !== "string") return { ok: false };
            return { ok: true, hasProject: true, id: project.id, name: project.name, status: status };
        }
        if (status === "unassociated" || status === "stale-removed") {
            if (project !== null && project !== undefined) return { ok: false };
            return { ok: true, hasProject: false, id: "", name: "", status: status };
        }
        return { ok: false };
    }

    // Strict badge parser (no root refs) so unit tests can exercise it via
    // node. Mirrors the strict-type style of parseCurrentProject: only a
    // finite non-negative number is accepted, otherwise 0; floors floats
    // and caps at 999. No coercion of strings/booleans/objects.
    function parseLedgerInboxBadge(value) {
        if (typeof value !== "number" || !isFinite(value) || value < 0) return 0;
        return Math.min(Math.floor(value), 999);
    }

    // Guarded badge refresh: delegates to the shared DailyAgenda's guarded
    // inbox read (safe to call on demand). Never throws; false when there is
    // no agenda or no refresh method. The steady cadence now comes from the
    // shell-level source's 60 s timer, not from a per-monitor poll.
    function refreshLedgerInboxBadge() {
        if (!root.agenda) return false;
        try {
            if (typeof root.agenda.refreshLedgerInboxBadge === "function") return !!root.agenda.refreshLedgerInboxBadge();
            return false;
        } catch (error) {
            return false;
        }
    }

    // Navigation target: the stable id only when a valid project is held;
    // blank otherwise (never a stale id).
    function targetProjectId() {
        if (root.backendOk && root.hasProject && root.projectId !== "") return root.projectId;
        return "";
    }

    // Plain openProject(id,'','') safely selects the stable id, activates
    // the projects tab and reloads the registry; blank id opens the list.
    function openCurrentProject() {
        let target = root.targetProjectId();
        try {
            if (root.projectPlanner) {
                if (typeof root.projectPlanner.openProject === "function") root.projectPlanner.openProject(target, "", "");
                else if (typeof root.projectPlanner.open === "function") root.projectPlanner.open();
            }
        } catch (error) {}
        return target;
    }

    // Click gate: a valid held project opens the compact overview popup
    // anchored to the tray button; anything else (no project,
    // backend-unavailable) falls back to the plain project list with a
    // blank id, never a stale id.
    function shouldOpenOverview() {
        return !!(root.backendOk && root.hasProject && root.projectId !== "");
    }

    function activateCurrentProject() {
        if (root.shouldOpenOverview()) {
            try {
                if (root.overviewPopup && typeof root.overviewPopup.openFor === "function") {
                    let name = root.projectName !== "" ? root.projectName : root.projectId;
                    if (root.overviewPopup.openFor(root.projectId, name, projectButton)) return root.projectId;
                }
            } catch (error) {}
        }
        return root.openCurrentProject();
    }

    // Keyboard-operable tray button (Button, not a MouseArea div): StrongFocus,
    // Accessible name, hover/focus tooltip with the full name/status, theme
    // hover/pressed/focus styling. 32px tall to sit in the shared capsule.
    Button {
        id: projectButton
        objectName: "currentProjectButton"
        height: 32
        anchors.verticalCenter: parent.verticalCenter
        focusPolicy: Qt.StrongFocus
        text: root.displayLabel
        Accessible.name: root.displayLabel
        Accessible.description: root.ledgerInboxBadge > 0 ? root.displayLabel + ", " + root.ledgerInboxBadge + (root.ledgerInboxBadge === 1 ? " session needing attention" : " sessions needing attention") : ""
        ToolTip.text: root.tooltipText
        ToolTip.visible: root.tooltipText !== "" && (hovered || activeFocus)
        ToolTip.delay: 400
        onClicked: root.activateCurrentProject()

        contentItem: Row {
            spacing: 6
            Text {
                color: Theme.subtext1
                font.family: Theme.iconFontFamily
                font.pixelSize: 14
                anchors.verticalCenter: parent.verticalCenter
                text: "󰉋"
            }
            Text {
                id: projectLabel
                objectName: "currentProjectLabel"
                color: Theme.text
                font.pixelSize: 12
                anchors.verticalCenter: parent.verticalCenter
                text: root.displayLabel
                elide: Text.ElideRight
                width: Math.min(implicitWidth, root.maxLabelWidth)
            }
        }

        background: Rectangle {
            color: {
                if (projectButton.pressed) return Theme.surface2;
                if (projectButton.hovered) return Theme.surface1;
                return Theme.mantle;
            }
            radius: Theme.controlRadius
            border.width: 1
            border.color: {
                if (projectButton.activeFocus) return Theme.focusBorder;
                if (projectButton.hovered) return Theme.accentMuted;
                return Theme.border;
            }
            Behavior on color { ColorAnimation { duration: Theme.motionFast } }
        }

        Rectangle {
            objectName: "currentProjectLedgerBadge"
            z: 1
            visible: root.ledgerInboxBadge > 0
            Accessible.ignored: true
            height: 14
            width: Math.max(14, ledgerBadgeText.implicitWidth + 6)
            radius: Theme.chipRadius
            color: Theme.accent
            anchors.top: parent.top
            anchors.right: parent.right
            anchors.topMargin: -3
            anchors.rightMargin: -3
            Text {
                id: ledgerBadgeText
                objectName: "currentProjectLedgerBadgeText"
                anchors.centerIn: parent
                text: String(root.ledgerInboxBadge)
                font.pixelSize: 9
                font.bold: true
                color: Theme.base
                Accessible.ignored: true
            }
        }
    }
}
