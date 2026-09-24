import QtQuick
import "../theme"

// View over the shell-level SystemStats source (S-005a): no Process, no
// Timer. The bar binds `value` (or `statsSource` + `statKey`) and this module
// only formats label + value.
Item {
    id: root
    implicitWidth: layout.implicitWidth
    implicitHeight: layout.implicitHeight
    width: implicitWidth
    height: implicitHeight

    property string label: ""
    // Direct value binding (preferred in Bar: value: statsSource.valueFor("mem")).
    property string value: "..."
    // Alternatively bind a shared source + key; `value` wins when set to a
    // real reading (anything other than the "..." placeholder).
    property var statsSource: null
    property string statKey: ""
    property color textColor: Theme.text
    property bool compact: false

    readonly property string displayValue: {
        if (root.value !== "...") return root.value;
        if (root.statsSource && root.statKey !== "") {
            if (typeof root.statsSource.valueFor === "function") return root.statsSource.valueFor(root.statKey);
            if (root.statKey === "mem" && root.statsSource.memValue !== undefined) return root.statsSource.memValue;
            if (root.statKey === "cpu" && root.statsSource.cpuValue !== undefined) return root.statsSource.cpuValue;
            if (root.statKey === "disk" && root.statsSource.diskValue !== undefined) return root.statsSource.diskValue;
        }
        return "...";
    }

    Row {
        id: layout
        spacing: 5
        anchors.verticalCenter: parent.verticalCenter

        Text {
            visible: !root.compact
            text: root.label
            color: root.textColor
            font.pixelSize: 12
            anchors.verticalCenter: parent.verticalCenter
        }
        Text {
            id: valueText
            text: root.displayValue
            color: root.textColor
            font.pixelSize: 12
            anchors.verticalCenter: parent.verticalCenter
        }
    }
}
