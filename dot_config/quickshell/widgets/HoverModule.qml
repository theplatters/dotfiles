import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Services.UPower

 import Quickshell.Wayland 
import "../theme"

PopupWindow{
  id: pill
  anchor.window: bar 
  anchor.rect.x: parentWindow.width / 2 - width / 2
  anchor.rect.y: 0
  visible: true
  color: Theme.bg

      // --- Public API / Configuration ---
      property bool expanded: false
      readonly property int collapsedHeight: 40
      readonly property int expandedHeight: 100
      readonly property int collapsedWidth: 150
      readonly property int expandedWidth:  560   // adjust to fit your tray items

      implicitHeight: expanded ? expandedHeight : collapsedHeight
      implicitWidth: expanded ? expandedWidth : collapsedWidth
      // --- Content Layout ---
      MouseArea {
              anchors.fill: parent
              hoverEnabled: true
              onEntered: pill.expanded = true
              onExited: pill.expanded = false 
      }
      Behavior on implicitWidth {
          NumberAnimation {
              duration: 260
              easing.type: Easing.InOutQuart
          }
      }
      Behavior on implicitHeight {
          NumberAnimation {
              duration: 260
              easing.type: Easing.InOutQuart
          }
      }

  Rectangle {
      id: root
      
      implicitHeight: parent.height
      implicitWidth: parent.width

      anchors.centerIn: parent
      color: Theme.base
      radius: Theme.radius


      RowLayout {
          anchors.centerIn: parent
          spacing: parent.width / 10 

          // Always visible core modules (The "Pill")
          AudioDisplay {
              id: audio 
              Layout.alignment: Qt.AlignVCenter
          }

          ClockModule {
              id: clock
              Layout.alignment: Qt.AlignVCenter
          }


      }
  // --- The Breakthrough Window Layer ---
  }
}
