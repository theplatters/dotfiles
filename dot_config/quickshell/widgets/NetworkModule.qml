import QtQuick
import Quickshell
import Quickshell.Bluetooth
import Quickshell.Networking
import "../theme"

Item {
    id: root
    // Plain Row extent: QQuickRow reports laid-out (actual) widths, so the
    // SSID/count caps below already bound this. No manual correction.
    implicitWidth: layout.implicitWidth
    implicitHeight: layout.implicitHeight
    width: implicitWidth
    height: implicitHeight

    property bool compact: false
    property var networkPopup: null
    property var controlCenter: null
    // Bar label budget: the SSID text never occupies more than this in the
    // bar (elided). The full name stays available in the ControlCenter
    // network list, so nothing is lost.
    property int maxLabelWidth: 160

    readonly property var wifiDevice: Networking.devices.values.find(device => device.type === DeviceType.Wifi) || null
    readonly property var wiredDevice: Networking.devices.values.find(device => device.type === DeviceType.Wired) || null
    readonly property var activeWifi: wifiDevice ? wifiDevice.networks.values.find(network => network.connected) || null : null
    readonly property bool wiredConnected: wiredDevice && wiredDevice.connected
    readonly property bool wifiConnected: wifiDevice && wifiDevice.connected
    readonly property bool connecting: wifiDevice && wifiDevice.state === ConnectionState.Connecting
    readonly property string connectionType: wiredConnected ? "ethernet" : (wifiConnected ? "wifi" : "none")
    readonly property real signalStrength: activeWifi ? activeWifi.signalStrength : 0
    readonly property var bluetoothAdapter: Bluetooth.defaultAdapter
    readonly property int connectedBluetoothDevices: Bluetooth.devices.values.filter(device => device.connected).length
    readonly property bool bluetoothEnabled: bluetoothAdapter && bluetoothAdapter.enabled

    // Full (untruncated) connection label. The bar shows at most
    // maxLabelWidth px of it; ControlCenter lists the full name.
    readonly property string fullLabel: {
        if (root.wiredConnected) return "Ethernet";
        if (root.activeWifi) return root.activeWifi.name;
        if (root.wifiConnected) return "Wi-Fi";
        if (!Networking.wifiHardwareEnabled) return "Wi-Fi blocked";
        if (!Networking.wifiEnabled) return "Wi-Fi off";
        if (root.connecting) return "Connecting";
        return "Offline";
    }
    // Worst-case bar width with everything shown, independent of compact:
    // icon advances + capped label + BT icon + capped count + 3 gaps.
    // Bar uses this for budget math that must not feed back into compact.
    readonly property real fullWidth: wifiIcon.implicitWidth + btIcon.implicitWidth
        + Math.min(labelNaturalWidth, maxLabelWidth) + Math.min(btCountNaturalWidth, 36) + 24
    // Natural (untruncated) label width for bar budgeting.
    readonly property real labelNaturalWidth: ssidText.implicitWidth
    readonly property real btCountNaturalWidth: btCountText.implicitWidth

    function wifiIcon(strength) {
        if (root.connecting) return "󰤫";
        if (!Networking.wifiHardwareEnabled || !Networking.wifiEnabled) return "󰤮";
        if (strength >= 0.8) return "󰤨";
        if (strength >= 0.6) return "󰤥";
        if (strength >= 0.4) return "󰤢";
        if (strength >= 0.2) return "󰤟";
        return "󰤯";
    }

    Row {
        id: layout
        spacing: 8
        anchors.verticalCenter: parent.verticalCenter

        Text {
            id: wifiIcon
            color: Theme.subtext1
            font.pixelSize: 14
            anchors.verticalCenter: parent.verticalCenter
            text: root.connectionType === "ethernet" ? "󰈀" : root.wifiIcon(root.signalStrength)
        }

        Text {
            id: ssidText
            visible: !root.compact
            color: Theme.text
            font.pixelSize: 12
            anchors.verticalCenter: parent.verticalCenter
            text: root.fullLabel
            elide: Text.ElideRight
            width: Math.min(implicitWidth, root.maxLabelWidth)
        }

        Text {
            id: btIcon
            color: Theme.subtext1
            font.pixelSize: 13
            anchors.verticalCenter: parent.verticalCenter
            text: root.bluetoothEnabled ? "󰂯" : "󰂲"
        }

        Text {
            id: btCountText
            visible: !root.compact && root.connectedBluetoothDevices > 0
            color: Theme.subtext1
            font.pixelSize: 12
            anchors.verticalCenter: parent.verticalCenter
            text: root.connectedBluetoothDevices.toString()
            elide: Text.ElideRight
            width: Math.min(implicitWidth, 36)
        }
    }

    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton
        cursorShape: Qt.PointingHandCursor
        onClicked: {
            if (root.controlCenter) {
                root.controlCenter.toggleSection(1);
            } else if (root.networkPopup) {
                root.networkPopup.toggle(root.parent);
            }
        }
    }
}
