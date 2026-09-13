import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Bluetooth
import Quickshell.Io
import Quickshell.Networking
import "../theme"

// Embeddable network + bluetooth controls extracted from NetworkPopup.
// Item root so it can live persistently inside ControlCenter.
// NetworkPopup.qml remains as a thin PopupWindow wrapper for backcompat.
//
// Discovery contract:
// - `active` gates all scanning: Wi-Fi scanner and Bluetooth discovery run
//   only when active && visible && the matching tab is selected.
// - activeTab: 0 = Network (Wi-Fi), 1 = Bluetooth.
// - Never scan hidden tabs.
Item {
    id: root

    property var passwordPopup: null
    property var pendingNetwork: null
    property var pendingSettings: null
    property string pendingPassword: ""
    property int activeTab: 0
    property string statusMessage: ""
    property bool active: false
    // Standalone popups need the internal Network/Bluetooth tabs; the
    // embedded ControlCenter already has its own Audio/Network/Bluetooth/
    // Power tabs, so it hides this duplicate selector (visible false excludes
    // it from the ColumnLayout with no reserved height).
    property bool showTabSelector: true

    readonly property var wifiDevice: Networking.devices.values.find(device => device.type === DeviceType.Wifi) || null
    readonly property var wiredDevice: Networking.devices.values.find(device => device.type === DeviceType.Wired) || null
    readonly property bool hasWifi: !!wifiDevice
    readonly property bool hasWired: !!wiredDevice
    readonly property bool isScanning: hasWifi && wifiDevice.scannerEnabled
    readonly property bool wifiAvailable: hasWifi && Networking.wifiHardwareEnabled
    readonly property int networkCount: hasWifi ? wifiDevice.networks.values.length : 0

    readonly property var bluetoothAdapter: Bluetooth.defaultAdapter
    readonly property bool hasBluetooth: !!bluetoothAdapter
    readonly property int bluetoothCount: hasBluetooth ? bluetoothAdapter.devices.values.length : 0
    readonly property int connectedBluetoothCount: Bluetooth.devices.values.filter(device => device.connected).length

    implicitWidth: 380
    // Natural height from content so the outer CC Flickable (bound to the
    // anchor screen) is the single responsive scroller. The content layout
    // is top-anchored (not fill) so this binding is not circular:
    // layout width follows root, layout implicit height drives root.
    implicitHeight: contentLayout.implicitHeight
    property int maxListHeight: 260

    // Last adapters we actually drove: replaced hardware must be stopped
    // before enabling its replacement (never leak scans on stale objects).
    property var _lastWifiDevice: null
    property var _lastBtAdapter: null

    onVisibleChanged: {
        syncDiscovery();
        if (visible && active) {
            root.statusMessage = "";
            if (Networking.canCheckConnectivity) Networking.checkConnectivity();
        }
    }

    onActiveChanged: {
        syncDiscovery();
        if (active && visible) {
            root.statusMessage = "";
            if (Networking.canCheckConnectivity) Networking.checkConnectivity();
        }
    }

    onActiveTabChanged: {
        syncDiscovery();
    }

    onHasWifiChanged: syncDiscovery()
    onHasBluetoothChanged: syncDiscovery()

    onWifiDeviceChanged: {
        root.stopStaleAdapters();
        syncDiscovery();
    }

    onBluetoothAdapterChanged: {
        root.stopStaleAdapters();
        syncDiscovery();
    }

    Connections {
        target: Networking
        enabled: root.active && root.visible
        function onWifiEnabledChanged() { root.syncDiscovery(); }
        function onWifiHardwareEnabledChanged() { root.syncDiscovery(); }
    }

    Connections {
        target: Bluetooth
        enabled: root.active && root.visible
        function onDefaultAdapterChanged() {
            root.stopStaleAdapters();
            root.syncDiscovery();
        }
    }

    Connections {
        target: root.bluetoothAdapter
        enabled: !!root.bluetoothAdapter && root.active && root.visible
        function onEnabledChanged() { root.syncDiscovery(); }
    }

    function stopStaleAdapters() {
        if (root._lastWifiDevice && root._lastWifiDevice !== root.wifiDevice) {
            try { root._lastWifiDevice.scannerEnabled = false; } catch (e) {}
        }
        if (root._lastBtAdapter && root._lastBtAdapter !== root.bluetoothAdapter) {
            try { root._lastBtAdapter.discovering = false; } catch (e) {}
        }
    }

    function syncDiscovery() {
        // Stop previously driven hardware that has since been replaced
        // before touching the current objects.
        root.stopStaleAdapters();
        var wantWifi = root.active && root.visible
            && Networking.wifiEnabled && Networking.wifiHardwareEnabled && root.activeTab === 0;
        var wantBt = root.active && root.visible
            && root.hasBluetooth && root.bluetoothAdapter.enabled && root.activeTab === 1;
        if (root.hasWifi) {
            // Never scan hidden tabs: force off unless this panel is active
            // on the network tab.
            root.wifiDevice.scannerEnabled = wantWifi;
        }
        if (root.hasBluetooth) {
            root.bluetoothAdapter.discovering = wantBt;
        }
        root._lastWifiDevice = root.wifiDevice;
        root._lastBtAdapter = root.bluetoothAdapter;
    }

    Connections {
        target: root.passwordPopup
        enabled: !!root.passwordPopup

        function onPasswordEntered(password) {
            if (!password || wifiConnectProc.running || !root.pendingNetwork) return;
            if (!root.wifiDevice) {
                root.statusMessage = "Wi-Fi adapter unavailable";
                root.pendingNetwork = null;
                root.pendingSettings = null;
                return;
            }
            if (root.passwordPopup.ssid === root.pendingNetwork.name) {
                root.statusMessage = "Connecting to " + root.pendingNetwork.name;
                if (root.pendingSettings) {
                    root.runWifiCommand([
                        "nmcli", "--ask", "connection", "up", "uuid", root.pendingSettings.uuid,
                        "ifname", root.wifiDevice.name
                    ], password);
                } else {
                    root.runWifiCommand([
                        "nmcli", "--ask", "device", "wifi", "connect", root.pendingNetwork.name,
                        "ifname", root.wifiDevice.name
                    ], password);
                }
            }
        }

        function onCanceled() {
            root.pendingNetwork = null;
            root.pendingSettings = null;
        }
    }

    Connections {
        target: root.pendingNetwork
        enabled: !!root.pendingNetwork

        function onConnectionFailed(reason) {
            if (wifiConnectProc.running) return;
            if (reason === ConnectionFailReason.NoSecrets && root.canUsePsk(root.pendingNetwork)) {
                if (root.passwordPopup) root.passwordPopup.show(root.pendingNetwork.name);
                root.statusMessage = "Password required for " + root.pendingNetwork.name;
                return;
            }
            root.statusMessage = "Connection failed: " + ConnectionFailReason.toString(reason);
            root.pendingNetwork = null;
            root.pendingSettings = null;
        }

        function onConnectedChanged() {
            if (root.pendingNetwork && root.pendingNetwork.connected) {
                root.statusMessage = "Connected to " + root.pendingNetwork.name;
                if (root.passwordPopup) root.passwordPopup.dismiss();
                root.pendingNetwork = null;
                root.pendingSettings = null;
            }
        }
    }

    Process {
        id: wifiConnectProc

        property string errorText: ""
        property bool suppliedPassword: false
        property string targetName: ""
        property bool targetCanUsePsk: false
        // Launch tracking: a helper that never starts (missing binary)
        // emits no exited in this Quickshell version, so detect it via
        // running going false without started (verified ScopedAgent pattern).
        property bool _launched: false
        property bool _started: false

        stdinEnabled: true
        environment: ({ "LC_ALL": "C" })

        stderr: StdioCollector {
            onTextChanged: wifiConnectProc.errorText = text.trim()
        }

        onStarted: {
            wifiConnectProc._started = true;
            if (root.pendingPassword !== "") {
                wifiConnectProc.write(root.pendingPassword + "\n");
                root.pendingPassword = "";
            }
        }

        onRunningChanged: {
            if (!wifiConnectProc.running && wifiConnectProc._launched && !wifiConnectProc._started) {
                wifiConnectProc._launched = false;
                root.failWifiStart();
            }
        }

        onExited: (exitCode) => {
            wifiConnectProc._launched = false;
            if (exitCode === 0) {
                root.statusMessage = "Connected to " + wifiConnectProc.targetName;
                if (root.passwordPopup) root.passwordPopup.dismiss();
                root.pendingNetwork = null;
                root.pendingSettings = null;
                return;
            }

            const needsPassword = wifiConnectProc.errorText.indexOf("Secrets were required") !== -1;
            if (!wifiConnectProc.suppliedPassword && needsPassword && wifiConnectProc.targetCanUsePsk && root.pendingNetwork) {
                if (root.passwordPopup) root.passwordPopup.show(wifiConnectProc.targetName);
                root.statusMessage = "Password required for " + wifiConnectProc.targetName;
                return;
            }

            root.statusMessage = wifiConnectProc.errorText
                ? wifiConnectProc.errorText.replace(/\s+/g, " ")
                : "Connection failed";
            root.pendingNetwork = null;
            root.pendingSettings = null;
        }
    }

    function signalIcon(strength) {
        if (strength >= 0.8) return "󰤨";
        if (strength >= 0.6) return "󰤥";
        if (strength >= 0.4) return "󰤢";
        if (strength >= 0.2) return "󰤟";
        return "󰤯";
    }

    function securityLabel(security) {
        if (security === WifiSecurityType.Open) return "Open";
        if (security === WifiSecurityType.Owe) return "Enhanced open";
        if (security === WifiSecurityType.Unknown) return "Unknown security";
        return WifiSecurityType.toString(security).replace(/([a-z0-9])([A-Z])/g, "$1 $2");
    }

    function canUsePsk(network) {
        return network && (
            network.security === WifiSecurityType.WpaPsk
            || network.security === WifiSecurityType.Wpa2Psk
            || network.security === WifiSecurityType.Sae
        );
    }

    function preferredSettings(network) {
        if (!network || !network.nmSettings || network.nmSettings.length === 0) return null;

        let selected = network.nmSettings[0];
        let newestTimestamp = Number(selected.read().connection.timestamp || 0);
        for (let i = 1; i < network.nmSettings.length; ++i) {
            const settings = network.nmSettings[i];
            const timestamp = Number(settings.read().connection.timestamp || 0);
            if (timestamp > newestTimestamp) {
                selected = settings;
                newestTimestamp = timestamp;
            }
        }
        return selected;
    }

    // Helper never started (FailedToStart emits no exited): report, clear
    // pending state, and drop the retained secret instead of hanging.
    function failWifiStart() {
        var name = wifiConnectProc.targetName !== "" ? wifiConnectProc.targetName : (root.pendingNetwork ? root.pendingNetwork.name : "");
        root.statusMessage = name !== "" ? "Connection failed (could not start helper) for " + name : "Connection failed (could not start helper)";
        root.pendingPassword = "";
        wifiConnectProc.errorText = "";
        root.pendingNetwork = null;
        root.pendingSettings = null;
        wifiConnectProc.targetName = "";
        wifiConnectProc.targetCanUsePsk = false;
    }

    function runWifiCommand(command, password) {
        if (wifiConnectProc.running) return;
        root.pendingPassword = password || "";
        wifiConnectProc.errorText = "";
        wifiConnectProc.suppliedPassword = root.pendingPassword !== "";
        wifiConnectProc.targetName = root.pendingNetwork.name;
        wifiConnectProc.targetCanUsePsk = root.canUsePsk(root.pendingNetwork);
        wifiConnectProc.command = command;
        wifiConnectProc._launched = true;
        wifiConnectProc._started = false;
        wifiConnectProc.running = true;
    }

    function connectNetwork(network) {
        if (wifiConnectProc.running || network.stateChanging) {
            root.statusMessage = "A connection attempt is already in progress";
            return;
        }

        root.pendingNetwork = network;
        root.pendingSettings = root.preferredSettings(network);
        root.statusMessage = "Connecting to " + network.name;

        if (root.pendingSettings) {
            root.runWifiCommand([
                "nmcli", "connection", "up", "uuid", root.pendingSettings.uuid,
                "ifname", root.wifiDevice.name
            ], "");
            return;
        }

        if (root.canUsePsk(network)) {
            if (root.passwordPopup) root.passwordPopup.show(network.name);
            root.statusMessage = "Password required for " + network.name;
            return;
        }

        network.connect();
    }

    function disconnectNetwork(network) {
        root.pendingNetwork = null;
        root.pendingSettings = null;
        root.statusMessage = "Disconnecting " + network.name;
        network.disconnect();
    }

    function forgetNetwork(network) {
        root.pendingNetwork = null;
        root.pendingSettings = null;
        root.statusMessage = "Forgot " + network.name;
        network.forget();
    }

    function bluetoothName(device) {
        return device.name || device.deviceName || device.address || "Bluetooth device";
    }

    function bluetoothIcon(device) {
        if (device.icon.indexOf("audio") !== -1 || device.icon.indexOf("headset") !== -1 || device.icon.indexOf("headphones") !== -1) return "󰋋";
        if (device.icon.indexOf("input") !== -1 || device.icon.indexOf("keyboard") !== -1) return "󰌌";
        if (device.icon.indexOf("mouse") !== -1) return "󰍽";
        if (device.icon.indexOf("phone") !== -1) return "󰄜";
        return "󰂯";
    }

    function toggleBluetoothDevice(device) {
        if (device.connected) {
            root.statusMessage = "Disconnecting " + root.bluetoothName(device);
            device.disconnect();
        } else if (device.paired || device.bonded) {
            root.statusMessage = "Connecting to " + root.bluetoothName(device);
            device.connect();
        } else if (device.pairing) {
            root.statusMessage = "Canceling pair request";
            device.cancelPair();
        } else {
            root.statusMessage = "Pairing " + root.bluetoothName(device);
            device.pair();
        }
    }

    ColumnLayout {
        id: contentLayout
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: 10

        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2

                Text {
                    text: "Connections"
                    color: Theme.text
                    font.pixelSize: 16
                    font.bold: true
                }

                Text {
                    text: root.activeTab === 0
                        ? (Networking.canCheckConnectivity ? NetworkConnectivity.toString(Networking.connectivity) : "Network")
                        : (root.hasBluetooth ? BluetoothAdapterState.toString(root.bluetoothAdapter.state) : "No Bluetooth adapter")
                    color: Theme.subtext0
                    font.pixelSize: 11
                }
            }

            Rectangle {
                width: 40
                height: 20
                radius: Theme.controlRadius
                color: {
                    if (root.activeTab === 0) return Networking.wifiEnabled && root.wifiAvailable ? Theme.surface2 : Theme.surface0;
                    return root.hasBluetooth && root.bluetoothAdapter.enabled ? Theme.surface2 : Theme.surface0;
                }
                opacity: root.activeTab === 0 ? (root.wifiAvailable ? 1 : 0.45) : (root.hasBluetooth ? 1 : 0.45)

                Rectangle {
                    width: 16
                    height: 16
                    radius: Theme.controlRadius
                    color: Theme.text
                    x: {
                        if (root.activeTab === 0) return Networking.wifiEnabled && root.wifiAvailable ? 22 : 2;
                        return root.hasBluetooth && root.bluetoothAdapter.enabled ? 22 : 2;
                    }
                    anchors.verticalCenter: parent.verticalCenter

                    Behavior on x {
                        NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
                    }
                }

                MouseArea {
                    anchors.fill: parent
                    enabled: root.activeTab === 0 ? root.wifiAvailable : root.hasBluetooth
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        if (root.activeTab === 0) {
                            Networking.wifiEnabled = !Networking.wifiEnabled;
                            if (root.hasWifi) root.wifiDevice.scannerEnabled = Networking.wifiEnabled && root.active && root.visible;
                        } else if (root.hasBluetooth) {
                            root.bluetoothAdapter.enabled = !root.bluetoothAdapter.enabled;
                            root.bluetoothAdapter.discovering = root.bluetoothAdapter.enabled && root.active && root.visible;
                        }
                    }
                }
            }
        }

        RowLayout {
            id: tabSelectorRow
            objectName: "tabSelectorRow"
            Layout.fillWidth: true
            visible: root.showTabSelector
            Layout.preferredHeight: root.showTabSelector ? 30 : 0
            Layout.minimumHeight: 0
            Layout.maximumHeight: root.showTabSelector ? 30 : 0
            spacing: 6

            Repeater {
                model: ["Network", "Bluetooth"]

                Rectangle {
                    Layout.fillWidth: true
                    height: 30
                    radius: Theme.controlRadius
                    color: root.activeTab === index ? Theme.surface2 : Theme.mantle
                    border.color: Theme.border
                    border.width: 1

                    Text {
                        anchors.centerIn: parent
                        text: modelData
                        color: Theme.text
                        font.pixelSize: 12
                        font.bold: root.activeTab === index
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.activeTab = index
                    }
                }
            }
        }

        Rectangle {
            id: tabSelectorDivider
            objectName: "tabSelectorDivider"
            Layout.fillWidth: true
            visible: root.showTabSelector
            height: root.showTabSelector ? 1 : 0
            Layout.preferredHeight: root.showTabSelector ? 1 : 0
            Layout.minimumHeight: 0
            color: Theme.surface0
            opacity: 0.6
        }

        Rectangle {
            Layout.fillWidth: true
            height: root.activeTab === 0 && root.hasWired ? 58 : 0
            visible: root.activeTab === 0 && root.hasWired
            radius: Theme.controlRadius
            color: Theme.mantle

            RowLayout {
                anchors.fill: parent
                anchors.margins: 10
                spacing: 12

                Text {
                    text: "󰈀"
                    color: root.wiredDevice && root.wiredDevice.connected ? Theme.green : Theme.subtext0
                    font.family: Theme.iconFontFamily
                    font.pixelSize: 17
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 2

                    Text {
                        text: root.wiredDevice ? root.wiredDevice.name : "Ethernet"
                        color: Theme.text
                        font.pixelSize: 13
                        font.bold: true
                    }

                    Text {
                        text: {
                            if (!root.wiredDevice) return "";
                            if (!root.wiredDevice.hasLink) return "Cable unplugged";
                            if (root.wiredDevice.connected) return root.wiredDevice.linkSpeed > 0 ? root.wiredDevice.linkSpeed + " Mb/s" : "Connected";
                            return ConnectionState.toString(root.wiredDevice.state);
                        }
                        color: Theme.subtext0
                        font.pixelSize: 10
                    }
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            Text {
                text: root.activeTab === 0 ? "Wi-Fi" : "Devices"
                color: Theme.text
                font.pixelSize: 13
                font.bold: true
                Layout.fillWidth: true
            }

            Text {
                text: root.activeTab === 0
                    ? (root.isScanning ? "Scanning" : root.networkCount + " found")
                    : (root.hasBluetooth && root.bluetoothAdapter.discovering ? "Discovering" : root.connectedBluetoothCount + " connected")
                color: Theme.subtext0
                font.pixelSize: 10
                visible: root.activeTab === 0
                    ? root.hasWifi && Networking.wifiEnabled && Networking.wifiHardwareEnabled
                    : root.hasBluetooth && root.bluetoothAdapter.enabled
            }
        }

        ListView {
            id: wifiListView
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? Math.min(root.maxListHeight, Math.max(120, wifiListView.contentHeight)) : 0
            Layout.minimumHeight: visible ? 120 : 0
            Layout.maximumHeight: root.maxListHeight
            spacing: 4
            clip: true
            model: root.hasWifi ? root.wifiDevice.networks : null
            visible: root.activeTab === 0 && root.hasWifi && Networking.wifiEnabled && Networking.wifiHardwareEnabled

            delegate: Rectangle {
                id: networkRow
                width: wifiListView.width
                height: 58
                radius: Theme.controlRadius
                color: modelData.connected ? Theme.surface2 : (wifiMouse.containsMouse ? Theme.surface0 : "transparent")
                border.color: modelData.connected ? Theme.border : "transparent"
                border.width: 1

                readonly property bool active: modelData.connected
                readonly property bool busy: modelData.stateChanging
                readonly property bool saved: modelData.known
                readonly property bool protectedNetwork: modelData.security !== WifiSecurityType.Open && modelData.security !== WifiSecurityType.Owe

                RowLayout {
                    anchors.fill: parent
                    anchors.margins: 10
                    spacing: 10

                    Text {
                        text: networkRow.busy ? "󰤫" : root.signalIcon(modelData?.signalStrength ?? 0)
                        color: networkRow.active ? Theme.text : Theme.subtext1
                        font.family: Theme.iconFontFamily
                        font.pixelSize: 16
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 2

                        Text {
                            Layout.fillWidth: true
                            text: modelData.name
                            color: Theme.text
                            font.pixelSize: 13
                            font.bold: networkRow.active
                            elide: Text.ElideRight
                        }

                        Text {
                            text: networkRow.busy
                                ? ConnectionState.toString(modelData.state)
                                : (networkRow.saved ? "Saved - " : "") + root.securityLabel(modelData.security)
                            color: Theme.subtext0
                            font.pixelSize: 10
                            opacity: 0.85
                        }
                    }

                    Text {
                        text: networkRow.protectedNetwork ? "󰌾" : ""
                        color: Theme.subtext1
                        font.family: Theme.iconFontFamily
                        font.pixelSize: 12
                        visible: networkRow.protectedNetwork
                    }

                    ColumnLayout {
                        spacing: 2

                        Text {
                            text: networkRow.active ? "Disconnect" : "Connect"
                            color: networkRow.active ? Theme.text : Theme.subtext1
                            font.pixelSize: 11
                            font.bold: true
                            horizontalAlignment: Text.AlignRight
                            Layout.fillWidth: true
                        }

                        Text {
                            text: "Forget"
                            color: Theme.red
                            font.pixelSize: 10
                            horizontalAlignment: Text.AlignRight
                            Layout.fillWidth: true
                            visible: networkRow.saved && !networkRow.active
                        }
                    }
                }

                MouseArea {
                    id: wifiMouse
                    anchors.fill: parent
                    enabled: !wifiConnectProc.running && !networkRow.busy
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: networkRow.active ? root.disconnectNetwork(modelData) : root.connectNetwork(modelData)
                }

                MouseArea {
                    width: 64
                    height: 24
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    enabled: networkRow.saved && !networkRow.active && !wifiConnectProc.running && !networkRow.busy
                    cursorShape: Qt.PointingHandCursor
                    z: 2
                    onClicked: root.forgetNetwork(modelData)
                }
            }
        }

        ListView {
            id: bluetoothListView
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? Math.min(root.maxListHeight, Math.max(120, bluetoothListView.contentHeight)) : 0
            Layout.minimumHeight: visible ? 120 : 0
            Layout.maximumHeight: root.maxListHeight
            spacing: 4
            clip: true
            model: root.hasBluetooth ? root.bluetoothAdapter.devices : null
            visible: root.activeTab === 1 && root.hasBluetooth && root.bluetoothAdapter.enabled

            delegate: Rectangle {
                id: bluetoothRow
                width: bluetoothListView.width
                height: 62
                radius: Theme.controlRadius
                color: modelData.connected ? Theme.surface2 : (btMouse.containsMouse ? Theme.surface0 : "transparent")
                border.color: modelData.connected ? Theme.border : "transparent"
                border.width: 1

                readonly property bool busy: modelData.state === BluetoothDeviceState.Connecting || modelData.state === BluetoothDeviceState.Disconnecting || modelData.pairing

                RowLayout {
                    anchors.fill: parent
                    anchors.margins: 10
                    spacing: 10

                    Text {
                        text: bluetoothRow.busy ? "󰂱" : root.bluetoothIcon(modelData)
                        color: modelData.connected ? Theme.text : Theme.subtext1
                        font.family: Theme.iconFontFamily
                        font.pixelSize: 16
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 2

                        Text {
                            Layout.fillWidth: true
                            text: root.bluetoothName(modelData)
                            color: Theme.text
                            font.pixelSize: 13
                            font.bold: modelData.connected
                            elide: Text.ElideRight
                        }

                        Text {
                            text: {
                                if (modelData.pairing) return "Pairing";
                                if (modelData.connected) return modelData.batteryAvailable ? "Connected - " + Math.round(modelData.battery * 100) + "%" : "Connected";
                                if (modelData.paired || modelData.bonded) return BluetoothDeviceState.toString(modelData.state);
                                return "Not paired";
                            }
                            color: Theme.subtext0
                            font.pixelSize: 10
                            opacity: 0.85
                        }
                    }

                    Text {
                        text: modelData.trusted ? "󰓎" : ""
                        color: Theme.subtext1
                        font.family: Theme.iconFontFamily
                        font.pixelSize: 12
                        visible: modelData.trusted
                    }

                    ColumnLayout {
                        spacing: 2

                        Text {
                            text: modelData.connected ? "Disconnect" : (modelData.paired || modelData.bonded ? "Connect" : "Pair")
                            color: modelData.connected ? Theme.text : Theme.subtext1
                            font.pixelSize: 11
                            font.bold: true
                            horizontalAlignment: Text.AlignRight
                            Layout.fillWidth: true
                        }

                        Text {
                            text: modelData.paired || modelData.bonded ? "Forget" : ""
                            color: Theme.red
                            font.pixelSize: 10
                            horizontalAlignment: Text.AlignRight
                            Layout.fillWidth: true
                            visible: modelData.paired || modelData.bonded
                        }
                    }
                }

                MouseArea {
                    id: btMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.toggleBluetoothDevice(modelData)
                }

                MouseArea {
                    width: 64
                    height: 24
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    enabled: modelData.paired || modelData.bonded
                    cursorShape: Qt.PointingHandCursor
                    z: 2
                    onClicked: {
                        root.statusMessage = "Forgot " + root.bluetoothName(modelData);
                        modelData.forget();
                    }
                }
            }
        }

        Text {
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            color: Theme.subtext0
            font.pixelSize: 12
            visible: {
                if (root.activeTab === 0) return !root.hasWifi || !Networking.wifiHardwareEnabled || !Networking.wifiEnabled || (root.networkCount === 0 && !root.isScanning);
                return !root.hasBluetooth || !root.bluetoothAdapter.enabled || (root.bluetoothCount === 0 && !root.bluetoothAdapter.discovering);
            }
            text: {
                if (root.activeTab === 0) {
                    if (!root.hasWifi) return "No Wi-Fi adapter";
                    if (!Networking.wifiHardwareEnabled) return "Wi-Fi hardware switch is off";
                    if (!Networking.wifiEnabled) return "Wi-Fi is disabled";
                    return "No networks found";
                }
                if (!root.hasBluetooth) return "No Bluetooth adapter";
                if (!root.bluetoothAdapter.enabled) return "Bluetooth is disabled";
                return "No Bluetooth devices found";
            }
        }

        Text {
            Layout.fillWidth: true
            visible: root.statusMessage !== ""
            text: root.statusMessage
            color: Theme.subtext0
            font.pixelSize: 10
            elide: Text.ElideRight
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            WidgetButton {
                text: root.activeTab === 0 ? (root.isScanning ? "Scanning..." : "Scan") : (root.hasBluetooth && root.bluetoothAdapter.discovering ? "Discovering..." : "Discover")
                Layout.fillWidth: true
                enabled: root.activeTab === 0
                    ? root.hasWifi && Networking.wifiEnabled && Networking.wifiHardwareEnabled
                    : root.hasBluetooth && root.bluetoothAdapter.enabled
                onClicked: {
                    if (root.activeTab === 0) {
                        root.wifiDevice.scannerEnabled = false;
                        root.wifiDevice.scannerEnabled = true;
                        root.statusMessage = "Scanning for Wi-Fi networks";
                    } else {
                        root.bluetoothAdapter.discovering = !root.bluetoothAdapter.discovering;
                        root.statusMessage = root.bluetoothAdapter.discovering ? "Discovering Bluetooth devices" : "Stopped Bluetooth discovery";
                    }
                }
            }

            WidgetButton {
                text: root.activeTab === 0
                    ? (root.hasWifi && root.wifiDevice.autoconnect ? "Auto On" : "Auto Off")
                    : (root.hasBluetooth && root.bluetoothAdapter.discoverable ? "Visible" : "Hidden")
                Layout.fillWidth: true
                enabled: root.activeTab === 0 ? root.hasWifi : root.hasBluetooth && root.bluetoothAdapter.enabled
                onClicked: {
                    if (root.activeTab === 0) {
                        root.wifiDevice.autoconnect = !root.wifiDevice.autoconnect;
                    } else {
                        root.bluetoothAdapter.discoverable = !root.bluetoothAdapter.discoverable;
                    }
                }
            }
        }
    }
}
