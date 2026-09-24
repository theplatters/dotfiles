import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "../theme"

// Review card bound to the shared DailyAgenda state object.
// Presentation only: all backend work lives in DailyAgenda (get,
// refresh, prepare-save, apply, scheduleReviewItem).
// Hidden/no-op when agenda is null. No direct file access, no
// pop-up errors; errors/notices are bounded single-line text.
ColumnLayout {
    id: root

    property var agenda: null

    spacing: 8
    visible: !!root.agenda

    onAgendaChanged: {
        if (root.agenda && root.agenda.reloadReview && visible) root.agenda.reloadReview()
    }
    onVisibleChanged: {
        if (visible && root.agenda && root.agenda.reloadReview) root.agenda.reloadReview()
    }
    Component.onCompleted: {
        if (root.agenda && root.agenda.reloadReview && visible) root.agenda.reloadReview()
    }

    function payload() {
        return root.agenda && root.agenda.reviewPayload ? root.agenda.reviewPayload : null
    }

    function review() {
        let entry = root.payload()
        return entry && entry.found && entry.review ? entry.review : null
    }

    function topItems() {
        let entry = root.review()
        if (!entry || !entry.top) return []
        return entry.top.slice(0, 3)
    }

    function topTotal() {
        let entry = root.review()
        if (!entry || !entry.top || !Array.isArray(entry.top)) {
            // finishReview stores the pre-slice count alongside the
            // bounded rows (S-041); older payloads fall back to shown.
            let stored = entry && typeof entry.top_total === "number"
                && isFinite(entry.top_total) && entry.top_total >= 0
                ? Math.floor(entry.top_total) : 0
            return Math.max(stored, root.topItems().length)
        }
        return Math.max(entry.top.length, root.topItems().length)
    }

    // One truncation pattern (S-041): "showing X of Y", silent unless
    // the list was actually capped.
    function truncationText() {
        let shown = root.topItems().length
        let total = root.topTotal()
        if (total > shown) return "showing " + shown + " of " + total
        return ""
    }

    function boundLine(value, limit) {
        let max = Number(limit) > 0 ? Math.floor(Number(limit)) : 120
        let clean = String(value === undefined || value === null ? "" : value)
            .replace(/\s+/g, " ").trim()
        if (clean.length > max) clean = clean.substring(0, max - 1) + "…"
        return clean
    }

    function sectionLines() {
        let entry = root.review()
        if (!entry || !entry.sections) return []
        let sections = entry.sections
        let lines = []
        let projects = Array.isArray(sections.projects) ? sections.projects.slice(0, 5) : []
        for (let i = 0; i < projects.length; i++) {
            let project = projects[i] || {}
            lines.push(root.boundLine(String(project.name || "Project") + " — "
                + String(project.sessions || 0) + " sessions · "
                + String(project.minutes || 0) + "min", 120))
        }
        let changes = Array.isArray(sections.changes) ? sections.changes.slice(0, 5) : []
        for (let j = 0; j < changes.length; j++) {
            let change = changes[j] || {}
            let note = change.note ? " — " + String(change.note) : ""
            lines.push(root.boundLine(String(change.name || "Project") + " · "
                + String(change.commits || "") + " commits" + note, 120))
        }
        if (sections.captures && typeof sections.captures === "object") {
            lines.push(root.boundLine("Captures: " + String(sections.captures.new || 0)
                + " new · " + String(sections.captures.accepted || 0)
                + " accepted · " + String(sections.captures.applied || 0) + " applied", 120))
        }
        if (sections.todos && typeof sections.todos === "object") {
            lines.push(root.boundLine("TODOs: " + String(sections.todos.scheduled || 0)
                + " scheduled · " + String(sections.todos.completed || 0) + " completed", 120))
        }
        if (sections.journal && typeof sections.journal === "object") {
            lines.push(root.boundLine(sections.journal.present ? "Journal ✓" : "Journal —", 120))
        }
        return lines.slice(0, 12)
    }

    // Markdown-preserving bound for generated review markdown (§6.1):
    // normalize line endings, strip C0 controls (except \n, \t) and
    // DEL, trim blank edges, then cap length at a line boundary so no
    // half-cut block + glued "…" breaks the rendered document.
    function boundMarkdown(value, limit) {
        let max = Number(limit) > 0 ? Math.floor(Number(limit)) : 2000
        let clean = String(value === undefined || value === null ? "" : value)
            .replace(/\r\n/g, "\n").replace(/\r/g, "\n")
            .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "")
            .replace(/\t/g, " ")
            .trim()
        if (clean.length > max) {
            let cut = clean.lastIndexOf("\n", max - 2)
            if (cut > 0) clean = clean.substring(0, cut) + "\n…"
            else clean = clean.substring(0, max - 1) + "…"
        }
        return clean
    }

    function markdownText() {
        let entry = root.review()
        if (!entry || !entry.markdown) return ""
        return root.boundMarkdown(entry.markdown, 2000)
    }

    function notDueText() {
        let entry = root.payload()
        let reason = entry && entry.reason ? String(entry.reason) : ""
        if (reason) return root.boundLine("Not due yet — " + reason, 200)
        return "Not due yet — the review appears after 18:00 / 07:00."
    }

    function isSaved() {
        let entry = root.review()
        return !!entry && entry.saved_ms !== undefined && entry.saved_ms !== null
    }

    function addLabel(item) {
        let day = item && item.target_date ? String(item.target_date) : ""
        let selected = root.agenda ? String(root.agenda.selectedDate || "") : ""
        if (day !== "" && day === selected) return "Add to today"
        return "Add to tomorrow"
    }

    // Header: title + Evening/Morning tabs + Refresh. Card header rule
    // (P4): Theme.text bold title with the count in parens where one
    // exists, actions as WidgetButtons showing Loading… while busy.
    RowLayout {
        Layout.fillWidth: true
        spacing: 8
        Text {
            text: "Review"
            color: Theme.text
            font.family: Theme.fontFamily
            font.bold: true
            Layout.fillWidth: true
            elide: Text.ElideRight
        }
        WidgetButton {
            objectName: "reviewTabEvening"
            text: "Evening"
            checkable: true
            checked: !!root.agenda && root.agenda.reviewKind === "evening"
            enabled: !!root.agenda && !root.agenda.reviewBusy
            Accessible.name: "Evening review tab"
            Accessible.description: "Show the evening review for the selected day"
            onClicked: {
                if (root.agenda && root.agenda.setReviewKind) root.agenda.setReviewKind("evening")
            }
        }
        WidgetButton {
            objectName: "reviewTabMorning"
            text: "Morning"
            checkable: true
            checked: !!root.agenda && root.agenda.reviewKind === "morning"
            enabled: !!root.agenda && !root.agenda.reviewBusy
            Accessible.name: "Morning plan tab"
            Accessible.description: "Show the morning plan for the selected day"
            onClicked: {
                if (root.agenda && root.agenda.setReviewKind) root.agenda.setReviewKind("morning")
            }
        }
        WidgetButton {
            objectName: "reviewRefreshButton"
            text: root.agenda && root.agenda.reviewBusy ? "Loading…" : "Refresh"
            enabled: !!root.agenda && !root.agenda.reviewBusy && !root.agenda.reviewRetiring
            Accessible.name: "Refresh review"
            Accessible.description: "Reload the review for the selected day"
            onClicked: {
                if (root.agenda && root.agenda.refreshReview) root.agenda.refreshReview()
            }
        }
    }

    Text {
        visible: !!root.agenda && String(root.agenda.reviewError || "") !== ""
        text: root.agenda ? String(root.agenda.reviewError || "") : ""
        color: Theme.red
        font.family: Theme.fontFamily
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    Text {
        visible: !!root.agenda && String(root.agenda.reviewNotice || "") !== ""
        text: root.agenda ? String(root.agenda.reviewNotice || "") : ""
        color: Theme.subtext1
        font.family: Theme.fontFamily
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    Text {
        visible: !!root.agenda && root.payload() !== null && !root.payload().found
        text: root.notDueText()
        color: Theme.subtext0
        font.family: Theme.fontFamily
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    Text {
        visible: !!root.agenda && root.isSaved()
        text: "Saved to journal."
        color: Theme.subtext1
        font.family: Theme.fontFamily
        Layout.fillWidth: true
    }

    // Bounded section summary lines (never raw JSON).
    ColumnLayout {
        visible: !!root.agenda && root.review() !== null
        Layout.fillWidth: true
        spacing: 2
        Repeater {
            model: root.sectionLines()
            Text {
                text: modelData
                color: Theme.subtext1
                font.family: Theme.fontFamily
                textFormat: Text.PlainText
                elide: Text.ElideRight
                wrapMode: Text.NoWrap
                Layout.fillWidth: true
            }
        }
    }

    // Generated review markdown renders as markdown (§6.1) through
    // the shared MarkdownBody (bounded lines + elide). Task rows below
    // stay PlainText: they are user-authored, untrusted input.
    MarkdownBody {
        visible: !!root.agenda && root.review() !== null && root.markdownText() !== ""
        text: root.markdownText()
        Layout.fillWidth: true
    }

    ListView {
        id: reviewTopList
        objectName: "reviewTopList"
        Layout.fillWidth: true
        Layout.preferredHeight: 132
        clip: true
        visible: !!root.agenda && root.topItems().length > 0
        model: root.topItems()
        spacing: 4
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        delegate: Rectangle {
            width: reviewTopList.width
            height: 40
            radius: Theme.controlRadius
            color: Theme.mantle
            border.color: Theme.border
            border.width: 1
            property int rowIndex: index
            RowLayout {
                anchors.fill: parent
                anchors.margins: 8
                spacing: 8
                Text {
                    text: String(modelData.task || "(empty task)")
                    color: Theme.text
                    font.family: Theme.fontFamily
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                WidgetButton {
                    objectName: "reviewAddButton"
                    text: root.addLabel(modelData)
                    enabled: !!root.agenda && !root.agenda.agendaBusy && !root.agenda.reviewBusy
                    Accessible.name: (root.addLabel(modelData) || "Add") + ": "
                        + String(modelData.task || "task") + " [" + index + "] "
                        + String(modelData.target_date || "")
                    Accessible.description: "Schedule this review item on its target day"
                    onClicked: {
                        if (root.agenda && root.agenda.scheduleReviewItem) root.agenda.scheduleReviewItem(modelData)
                    }
                }
            }
        }
    }

    Text {
        objectName: "reviewTruncation"
        visible: !!root.agenda && root.truncationText() !== ""
        text: root.truncationText()
        color: Theme.subtext0
        font.family: Theme.fontFamily
        font.pixelSize: 11
        elide: Text.ElideRight
        Layout.fillWidth: true
    }

    RowLayout {
        visible: !!root.agenda && root.review() !== null
        Layout.fillWidth: true
        spacing: 8
        Item { Layout.fillWidth: true }
        WidgetButton {
            objectName: "reviewSaveButton"
            visible: !root.isSaved()
            text: "Save to journal"
            enabled: !!root.agenda && !root.agenda.reviewBusy && !root.agenda.reviewApplying && root.review() !== null
            Accessible.name: "Save review to journal"
            Accessible.description: "Stage the review for today's journal with an exact preview"
            onClicked: {
                if (root.agenda && root.agenda.prepareReviewSave) root.agenda.prepareReviewSave()
            }
        }
    }

    // Exact-preview panel: destination + exact addition + Confirm/Cancel
    // (shared PreviewPanel: destination + revision + bounded readonly
    // block + Confirm/Cancel).
    PreviewPanel {
        visible: !!root.agenda && !!root.agenda.reviewPreview
        destinationText: {
            if (!root.agenda || !root.agenda.reviewPreview) return ""
            let preview = root.agenda.reviewPreview
            return "Journal " + String(preview.date || "") + " · "
                + String(preview.path || "") + " · revision "
                + String(preview.revision || "")
        }
        bodyText: root.agenda && root.agenda.reviewPreview ? String(root.agenda.reviewPreview.addition || "") : ""
        bodyAccessibleName: "Review preview"
        applying: !!root.agenda && !!root.agenda.reviewApplying
        applyError: root.agenda ? String(root.agenda.reviewApplyError || "") : ""
        confirmEnabled: !!root.agenda && !!root.agenda.reviewPreview && !!root.agenda.reviewPreviewToken && !root.agenda.reviewApplying && !root.agenda.reviewBusy
        confirmObjectName: "reviewConfirm"
        cancelObjectName: "reviewCancel"
        confirmAccessibleName: "Confirm review save"
        cancelAccessibleName: "Cancel review preview"
        onConfirmRequested: { if (root.agenda && root.agenda.applyReviewSave) root.agenda.applyReviewSave() }
        onCancelRequested: { if (root.agenda && root.agenda.cancelReviewPreview) root.agenda.cancelReviewPreview() }
    }
}
