import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "../theme"

// Session card (§4.5) bound to the shared DailyAgenda state object.
// Presentation only: all backend work lives in DailyAgenda (list,
// capture prepare/apply/dismiss, thought save/organise, dismiss,
// unmapped). Hidden/no-op when agenda is null.
//
// Lists EVERY session for the selected day (pending first, then time
// descending); `attended` drives the row marker only, never
// visibility. Expanding a row never triggers a write or a model call:
// Accept/Dismiss stage through the shared capture ladder with an
// exact preview + Confirm, Save thought stages through the journal
// ladder with an exact preview + Confirm, and Organise stages through
// thought_organise with its own preview + Confirm before the text
// ever replaces the editor. Errors/notices are bounded single-line
// text. Inner lists keep their own scrollbars (P4 card convention).
ColumnLayout {
    id: root

    property var agenda: null

    signal openProjectRequested(string projectId)

    // Ephemeral per-session UI state (never durable content):
    // unsaved thought drafts and explicit project attribution picks.
    // activeEditor tracks the one expanded thought editor so an
    // applied Organise result can replace its text; the draft map is
    // the source of truth and the editor only mirrors it on expand.
    property var thoughtDrafts: ({})
    property var attributionOverrides: ({})
    property var activeEditor: null
    property string activeSession: ""

    spacing: 8
    visible: !!root.agenda

    onAgendaChanged: {
        if (root.agenda && root.agenda.reloadLedger && visible) root.agenda.reloadLedger()
        if (root.agenda && root.agenda.reloadCaptures && visible) root.agenda.reloadCaptures()
    }
    onVisibleChanged: {
        if (visible && root.agenda && root.agenda.reloadLedger) root.agenda.reloadLedger()
        if (visible && root.agenda && root.agenda.reloadCaptures) root.agenda.reloadCaptures()
    }
    Component.onCompleted: {
        if (root.agenda && root.agenda.reloadLedger && visible) root.agenda.reloadLedger()
        if (root.agenda && root.agenda.reloadCaptures && visible) root.agenda.reloadCaptures()
    }

    Connections {
        target: root.agenda
        enabled: !!root.agenda
        ignoreUnknownSignals: true
        function onThoughtSaved(sessionId) { root.clearDraft(String(sessionId)) }
        function onOrganiseDone(sessionId, text) { root.applyOrganisedText(String(sessionId), String(text)) }
    }

    Shortcut {
        sequence: "Escape"
        enabled: !!root.agenda && !root.applyBusyAny()
        onActivated: {
            if (!root.agenda) return
            if (root.agenda.capturePreview && root.agenda.cancelCapturePreview) root.agenda.cancelCapturePreview()
            else if (root.agenda.thoughtPreview && root.agenda.cancelThoughtPreview) root.agenda.cancelThoughtPreview()
            else if (root.agenda.organisePreview && root.agenda.cancelOrganisePreview) root.agenda.cancelOrganisePreview()
            else if (root.agenda.ledgerPreview && root.agenda.cancelLedgerPreview) root.agenda.cancelLedgerPreview()
        }
    }

    function applyBusyAny() {
        if (!root.agenda) return false
        return !!(root.agenda.captureApplying || root.agenda.thoughtApplying
            || root.agenda.organiseApplying || root.agenda.ledgerApplying)
    }

    function boundLine(value, limit) {
        let max = Number(limit) > 0 ? Math.floor(Number(limit)) : 120
        let clean = String(value === undefined || value === null ? "" : value)
            .replace(/\s+/g, " ").trim()
        if (clean.length > max) clean = clean.substring(0, max - 1) + "…"
        return clean
    }

    function entrySession(entry) {
        if (!entry || typeof entry !== "object" || Array.isArray(entry)) return {}
        let session = entry.session
        if (!session || typeof session !== "object" || Array.isArray(session)) return {}
        return session
    }

    function sessionId(entry) {
        let session = root.entrySession(entry)
        return typeof session.session_id === "string" ? session.session_id : ""
    }

    function entryMeta(entry) {
        if (!entry || typeof entry !== "object" || Array.isArray(entry)) return {}
        let meta = entry.meta
        if (!meta || typeof meta !== "object" || Array.isArray(meta)) return {}
        return meta
    }

    function isAttended(entry) {
        return entry && (entry.attended === true || entry.attended === 1)
    }

    function isPending(entry) {
        return !!(entry && entry.pending === true)
    }

    function rowTime(entry) {
        let session = root.entrySession(entry)
        function validMs(raw) {
            if (raw === undefined || raw === null || raw === "" || typeof raw === "boolean") return false
            let n = Number(raw)
            return typeof n === "number" && isFinite(n) && Math.floor(n) === n && n >= 0
        }
        function fmt(ms) {
            let d = new Date(Number(ms))
            let h = d.getHours()
            let m = d.getMinutes()
            return (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m
        }
        let hasStart = validMs(session.start_ms)
        let hasEnd = validMs(session.end_ms)
        if (hasStart && hasEnd) {
            let a = fmt(session.start_ms)
            let b = fmt(session.end_ms)
            if (a === b) return a
            return a + "–" + b
        }
        if (hasStart) return fmt(session.start_ms)
        if (hasEnd) return fmt(session.end_ms)
        return ""
    }

    function startMs(entry) {
        let session = root.entrySession(entry)
        let n = Number(session.start_ms)
        if (typeof n === "number" && isFinite(n) && n >= 0) return n
        return -1
    }

    // Explicit attribution pick wins for display + Open project;
    // otherwise the session's resolved project. No durable
    // per-session override store exists (project_overrides was
    // deleted); the durable fix for unmapped folders is the
    // attribution card (§4.4).
    function effectiveProject(entry) {
        let sid = root.sessionId(entry)
        if (sid !== "" && root.attributionOverrides
                && Object.prototype.hasOwnProperty.call(root.attributionOverrides, sid)) {
            let picked = root.attributionOverrides[sid]
            if (picked && typeof picked === "object" && !Array.isArray(picked)) return picked
        }
        let session = root.entrySession(entry)
        let project = session.project
        if (!project || typeof project !== "object" || Array.isArray(project)) return {}
        return project
    }

    function rowProjectName(entry) {
        let project = root.effectiveProject(entry)
        let name = project && typeof project.name === "string" ? project.name.trim() : ""
        if (name !== "") return root.boundLine(name, 80)
        return "(unmapped)"
    }

    function projectIdOf(entry) {
        let project = root.effectiveProject(entry)
        let id = project && typeof project.id === "string" ? project.id.trim() : ""
        if (/^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(id))
            return id.toLowerCase()
        return ""
    }

    function todoCount(entry) {
        if (!root.agenda || !root.agenda.sessionTodoCount) return 0
        try { return Number(root.agenda.sessionTodoCount(root.sessionId(entry))) || 0 } catch (error) { return 0 }
    }

    function sessionCaptures(entry) {
        if (!root.agenda || !root.agenda.sessionCapturesFor) return []
        try {
            let rows = root.agenda.sessionCapturesFor(root.sessionId(entry))
            return Array.isArray(rows) ? rows : []
        } catch (error) { return [] }
    }

    // Every session for the day: pending first, then time descending.
    function sortedEntries() {
        let rows = root.agenda && Array.isArray(root.agenda.ledgerEntries)
            ? root.agenda.ledgerEntries.slice() : []
        rows.sort((a, b) => {
            let pa = root.isPending(a) ? 0 : 1
            let pb = root.isPending(b) ? 0 : 1
            if (pa !== pb) return pa - pb
            return root.startMs(b) - root.startMs(a)
        })
        return rows
    }

    function dayLabel() {
        if (!root.agenda || !root.agenda.selectedDate) return ""
        let iso = String(root.agenda.selectedDate)
        let match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso)
        if (!match) return iso
        let months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        let days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        let d = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
        if (isNaN(d.getTime())) return iso
        return days[d.getDay()] + " " + Number(match[3]) + " " + months[Number(match[2]) - 1]
    }

    function projectOptions() {
        if (!root.agenda || !root.agenda.sessionProjects) return []
        try {
            let rows = root.agenda.sessionProjects()
            return Array.isArray(rows) ? rows : []
        } catch (error) { return [] }
    }

    function optionIndexFor(entry) {
        let pid = root.projectIdOf(entry)
        if (pid === "") return -1
        let options = root.projectOptions()
        for (let i = 0; i < options.length; i++) {
            if (options[i] && String(options[i].id || "").toLowerCase() === pid) return i
        }
        return -1
    }

    function draftFor(sessionId) {
        let sid = String(sessionId || "")
        if (sid === "" || !root.thoughtDrafts) return ""
        let value = root.thoughtDrafts[sid]
        return typeof value === "string" ? value : ""
    }

    function setDraft(sessionId, text) {
        let sid = String(sessionId || "")
        if (sid === "") return
        let next = Object.assign({}, root.thoughtDrafts)
        next[sid] = String(text === undefined || text === null ? "" : text)
        root.thoughtDrafts = next
    }

    function clearDraft(sessionId) {
        let sid = String(sessionId || "")
        if (sid === "" || !root.thoughtDrafts) return
        if (!Object.prototype.hasOwnProperty.call(root.thoughtDrafts, sid)) return
        let next = Object.assign({}, root.thoughtDrafts)
        delete next[sid]
        root.thoughtDrafts = next
    }

    function applyOrganisedText(sessionId, text) {
        let sid = String(sessionId || "")
        if (sid === "") return
        root.setDraft(sid, text)
        if (root.activeSession === sid && root.activeEditor) {
            try {
                root.activeEditor.suppressStore = true
                root.activeEditor.text = String(text)
                root.activeEditor.suppressStore = false
            } catch (error) {}
        }
    }

    function setAttribution(sessionId, projectId) {
        let sid = String(sessionId || "")
        let pid = String(projectId || "").toLowerCase()
        if (sid === "" || pid === "") return
        let options = root.projectOptions()
        for (let i = 0; i < options.length; i++) {
            if (options[i] && String(options[i].id || "").toLowerCase() === pid) {
                let next = Object.assign({}, root.attributionOverrides)
                next[sid] = { id: String(options[i].id), name: String(options[i].name || "") }
                root.attributionOverrides = next
                return
            }
        }
    }

    function isSelected(entry) {
        if (!root.agenda || !entry || typeof entry !== "object" || Array.isArray(entry)) return false
        return String(root.sessionId(entry)) !== ""
            && String(root.sessionId(entry)) === String(root.agenda.ledgerSelectedId)
    }

    function toggleEntry(entry) {
        if (!root.agenda || !root.agenda.selectLedgerEntry) return
        let sid = root.sessionId(entry)
        if (sid === "") return
        // Expanding is read-only: selecting never writes or model-calls.
        // Ensure the day's captures are loaded so the TODO list is fresh.
        root.agenda.selectLedgerEntry(sid)
        if (root.agenda.reloadCaptures
                && (!root.agenda.captureItems || root.agenda.captureItems.length === 0))
            root.agenda.reloadCaptures()
    }

    function statusText() {
        if (!root.agenda) return ""
        let parts = []
        let err = String(root.agenda.ledgerError || "")
        if (err !== "") return err
        if (String(root.agenda.captureError || "") !== "") parts.push(String(root.agenda.captureError))
        if (String(root.agenda.thoughtError || "") !== "") parts.push(String(root.agenda.thoughtError))
        if (String(root.agenda.organiseError || "") !== "") parts.push(String(root.agenda.organiseError))
        if (parts.length > 0) return parts.join(" ")
        let notes = []
        if (String(root.agenda.captureNotice || "") !== "") notes.push(String(root.agenda.captureNotice))
        if (String(root.agenda.thoughtNotice || "") !== "") notes.push(String(root.agenda.thoughtNotice))
        if (String(root.agenda.ledgerNotice || "") !== "") notes.push(String(root.agenda.ledgerNotice))
        return notes.join(" ")
    }

    function statusIsError() {
        if (!root.agenda) return false
        if (String(root.agenda.ledgerError || "") !== "") return true
        if (String(root.agenda.captureError || "") !== "") return true
        if (String(root.agenda.thoughtError || "") !== "") return true
        if (String(root.agenda.organiseError || "") !== "") return true
        return false
    }

    function thoughtPreviewFor(sessionId) {
        if (!root.agenda || !root.agenda.thoughtPreview) return null
        if (String(root.agenda.thoughtSessionId || "") !== String(sessionId || "")) return null
        return root.agenda.thoughtPreview
    }

    function organisePreviewFor(sessionId) {
        if (!root.agenda || !root.agenda.organisePreview) return null
        if (String(root.agenda.organiseSession || "") !== String(sessionId || "")) return null
        return root.agenda.organisePreview
    }

    function capturePreviewFor(sessionId) {
        if (!root.agenda || !root.agenda.capturePreview) return null
        if (String(root.agenda.capturePreviewSession || "") !== String(sessionId || "")) return null
        return root.agenda.capturePreview
    }

    // One truncation pattern (S-041): "showing X of Y", silent unless
    // the day list was actually capped (backend total > shown rows).
    function truncationText() {
        let shown = root.sortedEntries().length
        let total = 0
        try {
            total = Number(root.agenda ? root.agenda.ledgerTotal : 0)
        } catch (error) {
            total = 0
        }
        if (!(typeof total === "number" && isFinite(total) && total > 0))
            total = shown
        if (total > shown) return "showing " + shown + " of " + Math.floor(total)
        return ""
    }

    // Header Resume target (§6.4): the selected entry first, else the
    // first listed entry with a resolved project. Navigation only —
    // routes the existing openProjectRequested path, never a write.
    function headerResumeProject() {
        if (!root.agenda || !root.agenda.openSessionProject) return ""
        let selected = String(root.agenda.ledgerSelectedId || "")
        if (selected !== "") {
            try {
                let pid = String(root.agenda.openSessionProject(selected) || "")
                if (pid !== "") return pid
            } catch (error) {}
        }
        let rows = root.sortedEntries()
        for (let i = 0; i < rows.length; i++) {
            let sid = root.sessionId(rows[i])
            if (sid === "") continue
            try {
                let pid = String(root.agenda.openSessionProject(sid) || "")
                if (pid !== "") return pid
            } catch (error) {}
        }
        return ""
    }

    // Header: title + day + always-on count + Resume + Refresh. Card
    // header rule (P4): Theme.text bold title with the count in
    // parens, actions as WidgetButtons showing Loading… while busy.
    // Resume lives here (§6.4), never in rows: it routes the existing
    // openProjectRequested path for the selected (else first) entry.
    RowLayout {
        Layout.fillWidth: true
        spacing: 8
        Text {
            text: "Sessions · " + root.dayLabel()
                + " (" + root.sortedEntries().length + ")"
            color: Theme.text
            font.family: Theme.fontFamily
            font.bold: true
            Layout.fillWidth: true
            elide: Text.ElideRight
        }
        WidgetButton {
            objectName: "sessionResumeButton"
            text: "Resume"
            enabled: !!root.agenda && root.headerResumeProject() !== ""
            Accessible.name: "Resume session project"
            Accessible.description: "Open the selected session's project in the planner"
            onClicked: {
                let pid = root.headerResumeProject()
                if (pid !== "") root.openProjectRequested(pid)
            }
        }
        WidgetButton {
            objectName: "sessionRefreshButton"
            text: root.agenda && root.agenda.ledgerBusy ? "Loading…" : "Refresh"
            enabled: !!root.agenda && !root.agenda.ledgerBusy && !root.agenda.ledgerRetiring
            Accessible.name: "Refresh sessions"
            Accessible.description: "Reload the sessions and captures for the selected day"
            onClicked: {
                if (root.agenda && root.agenda.refreshLedger) root.agenda.refreshLedger()
                if (root.agenda && root.agenda.reloadCaptures) root.agenda.reloadCaptures()
            }
        }
    }

    Text {
        objectName: "sessionStatus"
        visible: !!root.agenda && root.statusText() !== ""
        text: root.statusText()
        color: root.statusIsError() ? Theme.red : Theme.subtext1
        font.family: Theme.fontFamily
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    Text {
        objectName: "sessionEmpty"
        visible: !!root.agenda && root.sortedEntries().length === 0 && !root.agenda.ledgerBusy
        text: "No sessions for this day yet. Sessions appear here after the collector closes them."
        color: Theme.subtext0
        font.family: Theme.fontFamily
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    ListView {
        id: sessionList
        objectName: "sessionList"
        Layout.fillWidth: true
        Layout.preferredHeight: 280
        clip: true
        model: root.sortedEntries()
        spacing: 4
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        delegate: Rectangle {
            width: sessionList.width
            implicitHeight: entryCol.implicitHeight + 16
            radius: Theme.controlRadius
            color: root.isSelected(modelData) ? Theme.surface1 : Theme.mantle
            border.color: root.isSelected(modelData) ? Theme.focusBorder : Theme.border
            border.width: 1
            MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: root.toggleEntry(modelData)
            }
            ColumnLayout {
                id: entryCol
                anchors.fill: parent
                anchors.margins: 8
                spacing: 4
                // Collapsed row: time range · project · N TODOs · done marker.
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    Text {
                        text: root.isSelected(modelData) ? "▼" : "▶"
                        color: Theme.subtext0
                        font.family: Theme.fontFamily
                    }
                    Text {
                        text: {
                            let parts = []
                            let time = root.rowTime(modelData)
                            if (time !== "") parts.push(time)
                            parts.push(root.rowProjectName(modelData))
                            let n = root.todoCount(modelData)
                            parts.push(n === 1 ? "1 TODO" : n + " TODOs")
                            if (root.isAttended(modelData)) parts.push("✓")
                            return parts.join(" · ")
                        }
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.bold: true
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                }
                // Expanded editor: same shape for every session, including
                // ones with no captures and no thought.
                ColumnLayout {
                    visible: root.isSelected(modelData)
                    Layout.fillWidth: true
                    spacing: 6
                    onVisibleChanged: {
                        if (visible) {
                            root.activeEditor = thoughtEditor
                            root.activeSession = root.sessionId(modelData)
                        } else if (root.activeSession === root.sessionId(modelData)) {
                            root.activeEditor = null
                            root.activeSession = ""
                        }
                    }
                    Text {
                        text: "Captured from your agent session"
                        color: Theme.subtext0
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                        Layout.fillWidth: true
                    }
                    Text {
                        visible: root.sessionCaptures(modelData).length === 0
                        text: "No captured TODOs for this session."
                        color: Theme.subtext0
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                        Layout.fillWidth: true
                    }
                    ListView {
                        visible: root.sessionCaptures(modelData).length > 0
                        Layout.fillWidth: true
                        Layout.preferredHeight: Math.min(148, root.sessionCaptures(modelData).length * 40)
                        clip: true
                        interactive: root.sessionCaptures(modelData).length * 40 > 148
                        model: root.sessionCaptures(modelData)
                        spacing: 4
                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                        delegate: Rectangle {
                            width: parent ? parent.width : 0
                            height: 36
                            radius: Theme.controlRadius
                            color: Theme.base
                            border.color: Theme.border
                            border.width: 1
                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: 6
                                spacing: 8
                                Text {
                                    text: "☐"
                                    color: Theme.subtext0
                                    font.family: Theme.fontFamily
                                }
                                Text {
                                    text: root.boundLine(modelData.text, 160)
                                    color: Theme.text
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 12
                                    textFormat: Text.PlainText
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                                WidgetButton {
                                    objectName: "sessionAcceptTodo"
                                    text: "Accept"
                                    enabled: !!root.agenda && !root.agenda.captureBusy && !root.agenda.captureApplying
                                    Accessible.name: "Accept TODO: " + root.boundLine(modelData.text, 80)
                                    Accessible.description: "File this TODO to the project page with an exact preview"
                                    onClicked: {
                                        if (!root.agenda || !root.agenda.prepareCapture) return
                                        root.agenda.prepareCapture(modelData.id, root.sessionId(modelData), "")
                                    }
                                }
                                WidgetButton {
                                    objectName: "sessionDismissTodo"
                                    text: "Dismiss"
                                    enabled: !!root.agenda && !root.agenda.captureBusy && !root.agenda.captureApplying
                                    Accessible.name: "Dismiss TODO: " + root.boundLine(modelData.text, 80)
                                    Accessible.description: "Dismiss this TODO without writing"
                                    onClicked: {
                                        if (!root.agenda || !root.agenda.dismissCapture) return
                                        root.agenda.dismissCapture(modelData.id)
                                    }
                                }
                            }
                        }
                    }
                    // Exact preview for an Accepted TODO from this session.
                    PreviewPanel {
                        visible: root.capturePreviewFor(root.sessionId(modelData)) !== null
                        destinationText: {
                            let preview = root.capturePreviewFor(root.sessionId(modelData))
                            if (!preview) return ""
                            return String(preview.page || "") + " · " + String(preview.path || "")
                        }
                        bodyText: {
                            let preview = root.capturePreviewFor(root.sessionId(modelData))
                            return preview ? String(preview.block || "") : ""
                        }
                        bodyAccessibleName: "Session capture preview"
                        bodyObjectName: "sessionCapturePreview"
                        applying: !!root.agenda && !!root.agenda.captureApplying
                        applyError: root.agenda ? String(root.agenda.captureApplyError || "") : ""
                        confirmEnabled: root.capturePreviewFor(root.sessionId(modelData)) !== null
                            && !!root.agenda && !!root.agenda.capturePreviewToken
                            && !root.agenda.captureApplying && !root.agenda.captureBusy
                        confirmObjectName: "sessionCaptureConfirm"
                        cancelObjectName: "sessionCaptureCancel"
                        confirmAccessibleName: "Confirm TODO save"
                        cancelAccessibleName: "Cancel TODO preview"
                        // §6.4 file verb: this Confirm files to the
                        // project page (single target, L1).
                        confirmText: "Save to project"
                        onConfirmRequested: { if (root.agenda && root.agenda.applyCapture) root.agenda.applyCapture() }
                        onCancelRequested: { if (root.agenda && root.agenda.cancelCapturePreview) root.agenda.cancelCapturePreview() }
                    }
                    Text {
                        text: {
                            let meta = root.entryMeta(modelData)
                            let ref = typeof meta.thought_ref === "string" ? meta.thought_ref : ""
                            return ref !== "" ? "Thought saved ✓ " + ref : "Thought"
                        }
                        color: Theme.subtext0
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                        Layout.fillWidth: true
                    }
                    TextArea {
                        id: thoughtEditor
                        property bool suppressStore: false
                        objectName: "sessionThoughtEditor"
                        Layout.fillWidth: true
                        Layout.preferredHeight: 96
                        text: root.draftFor(root.sessionId(modelData))
                        placeholderText: "Write a rough thought — saved to today's journal on Confirm."
                        color: Theme.text
                        font.family: Theme.fontFamily
                        textFormat: TextEdit.PlainText
                        wrapMode: TextArea.Wrap
                        selectByMouse: true
                        Accessible.name: "Thought for session " + root.sessionId(modelData).substring(0, 8)
                        Accessible.description: "Unsaved draft; Save thought stages an exact journal preview"
                        background: Rectangle { color: Theme.base; radius: Theme.controlRadius; border.color: thoughtEditor.activeFocus ? Theme.focusBorder : Theme.border }
                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                        onTextChanged: {
                            if (suppressStore) return
                            root.setDraft(root.sessionId(modelData), text)
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        WidgetButton {
                            objectName: "sessionOrganise"
                            text: root.agenda && root.agenda.organiseBusy ? "Organising…" : "Organise"
                            enabled: !!root.agenda && !root.agenda.organiseBusy && !root.agenda.organiseApplying
                                && !root.agenda.thoughtBusy && !root.agenda.thoughtApplying
                                && root.draftFor(root.sessionId(modelData)).trim() !== ""
                            Accessible.name: "Organise thought"
                            Accessible.description: "Rewrite this thought for readability with an exact preview first"
                            onClicked: {
                                if (!root.agenda || !root.agenda.prepareOrganise) return
                                root.agenda.prepareOrganise(root.sessionId(modelData),
                                    root.draftFor(root.sessionId(modelData)))
                            }
                        }
                        Item { Layout.fillWidth: true }
                        WidgetButton {
                            objectName: "sessionSaveThought"
                            text: root.agenda && root.agenda.thoughtBusy ? "Loading…" : "Save thought"
                            enabled: !!root.agenda && !root.agenda.thoughtBusy && !root.agenda.thoughtApplying
                                && !root.agenda.organiseBusy && !root.agenda.organiseApplying
                                && root.draftFor(root.sessionId(modelData)).trim() !== ""
                            Accessible.name: "Save thought"
                            Accessible.description: "Stage this thought for today's journal with an exact preview"
                            onClicked: {
                                if (!root.agenda || !root.agenda.saveThought) return
                                root.agenda.saveThought(root.sessionId(modelData),
                                    root.draftFor(root.sessionId(modelData)))
                            }
                        }
                    }
                    Text {
                        visible: !!root.agenda && String(root.agenda.organiseApplyError || "") !== ""
                            && String(root.agenda.organiseSession || "") === root.sessionId(modelData)
                        text: root.agenda ? String(root.agenda.organiseApplyError || "") : ""
                        color: Theme.red
                        font.family: Theme.fontFamily
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                    // Organise preview: exact rewritten text; Confirm
                    // replaces the editor draft (no write, no journal).
                    PreviewPanel {
                        visible: root.organisePreviewFor(root.sessionId(modelData)) !== null
                        destinationText: "Organised thought · replaces the draft above"
                        bodyText: {
                            let preview = root.organisePreviewFor(root.sessionId(modelData))
                            return preview ? String(preview.text || "") : ""
                        }
                        bodyAccessibleName: "Organised thought preview"
                        bodyObjectName: "sessionOrganisePreview"
                        applying: !!root.agenda && !!root.agenda.organiseApplying
                        applyError: root.agenda ? String(root.agenda.organiseApplyError || "") : ""
                        confirmEnabled: root.organisePreviewFor(root.sessionId(modelData)) !== null
                            && !!root.agenda && !!root.agenda.organiseToken
                            && !root.agenda.organiseApplying && !root.agenda.organiseBusy
                        confirmObjectName: "sessionOrganiseConfirm"
                        cancelObjectName: "sessionOrganiseCancel"
                        confirmAccessibleName: "Use organised text"
                        cancelAccessibleName: "Cancel organise preview"
                        onConfirmRequested: { if (root.agenda && root.agenda.applyOrganise) root.agenda.applyOrganise() }
                        onCancelRequested: { if (root.agenda && root.agenda.cancelOrganisePreview) root.agenda.cancelOrganisePreview() }
                    }
                    // Journal exact preview: destination + exact addition +
                    // Confirm/Cancel (shared PreviewPanel).
                    PreviewPanel {
                        visible: root.thoughtPreviewFor(root.sessionId(modelData)) !== null
                        destinationText: {
                            let preview = root.thoughtPreviewFor(root.sessionId(modelData))
                            if (!preview) return ""
                            return "Journal · " + String(preview.path || "")
                        }
                        bodyText: {
                            let preview = root.thoughtPreviewFor(root.sessionId(modelData))
                            return preview ? String(preview.addition || "") : ""
                        }
                        bodyAccessibleName: "Thought preview"
                        bodyObjectName: "sessionThoughtPreview"
                        applying: !!root.agenda && !!root.agenda.thoughtApplying
                        applyError: root.agenda ? String(root.agenda.thoughtApplyError || "") : ""
                        confirmEnabled: root.thoughtPreviewFor(root.sessionId(modelData)) !== null
                            && !!root.agenda && !root.agenda.thoughtApplying && !root.agenda.thoughtBusy
                        confirmObjectName: "sessionThoughtConfirm"
                        cancelObjectName: "sessionThoughtCancel"
                        confirmAccessibleName: "Confirm thought save"
                        cancelAccessibleName: "Cancel thought preview"
                        onConfirmRequested: { if (root.agenda && root.agenda.applyThought) root.agenda.applyThought() }
                        onCancelRequested: { if (root.agenda && root.agenda.cancelThoughtPreview) root.agenda.cancelThoughtPreview() }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        Text {
                            text: "Project:"
                            color: Theme.subtext0
                            font.family: Theme.fontFamily
                            font.pixelSize: 12
                        }
                        ComboBox {
                            objectName: "sessionProjectPicker"
                            Layout.fillWidth: true
                            model: root.projectOptions().map(option => String(option.name || ""))
                            currentIndex: root.optionIndexFor(modelData)
                            enabled: root.projectOptions().length > 0
                            Accessible.name: "Project for session " + root.sessionId(modelData).substring(0, 8)
                            Accessible.description: "Explicit attribution for this session"
                            onActivated: (index) => {
                                let options = root.projectOptions()
                                if (index >= 0 && index < options.length && options[index])
                                    root.setAttribution(root.sessionId(modelData), String(options[index].id || ""))
                            }
                        }
                    }
                    Text {
                        visible: root.projectIdOf(modelData) === ""
                        text: "No project claims this session. Claim its folder in the Projects tab to fix attribution."
                        color: Theme.subtext0
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        WidgetButton {
                            objectName: "sessionDismiss"
                            text: "Dismiss session"
                            enabled: !!root.agenda && !root.agenda.dismissBusy && !root.isAttended(modelData)
                            Accessible.name: "Dismiss session " + root.sessionId(modelData).substring(0, 8)
                            Accessible.description: "Mark this session attended; it stays visible"
                            onClicked: {
                                if (!root.agenda || !root.agenda.dismissSession) return
                                root.agenda.dismissSession(root.sessionId(modelData))
                            }
                        }
                        Item { Layout.fillWidth: true }
                        WidgetButton {
                            objectName: "sessionOpenProject"
                            text: "Open project"
                            enabled: root.projectIdOf(modelData) !== ""
                            Accessible.name: "Open project " + root.rowProjectName(modelData)
                            Accessible.description: "Open this session's project in the planner"
                            onClicked: {
                                let pid = root.projectIdOf(modelData)
                                if (pid !== "") root.openProjectRequested(pid)
                            }
                        }
                    }
                    Text {
                        visible: !!root.agenda && String(root.agenda.dismissError || "") !== ""
                            && String(root.agenda.dismissSessionId || "") === root.sessionId(modelData)
                        text: root.agenda ? String(root.agenda.dismissError || "") : ""
                        color: Theme.red
                        font.family: Theme.fontFamily
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                }
            }
        }
    }

    Text {
        objectName: "sessionTruncation"
        visible: !!root.agenda && root.truncationText() !== ""
        text: root.truncationText()
        color: Theme.subtext0
        font.family: Theme.fontFamily
        font.pixelSize: 11
        elide: Text.ElideRight
        Layout.fillWidth: true
    }

    // No legacy project-page filing path remains: TODOs file through
    // the capture ladder and thoughts through the journal ladder, each
    // with their own exact preview above.
}
