# Control Center + unified tray

## Unified tray (Bar left, first capsule)

One shared `Theme.mantle` capsule (`id: unifiedTray`, alias `trayAnchor`)
holds, in order: chevron toggle, audio, network + Bluetooth, battery (only
when `UPower.displayDevice.type === 2`), tray icons in a capped viewport
(hides when `trayModule.implicitWidth` is 0). Subtle 1px `Theme.border`
separators, no per-status pills. Active state (popup `requestedOpen ||
visible`) drives capsule `surface0` + `accentMuted` border on
`Theme.motionFast`.

No click overlay covers the modules (the only `MouseArea`s in the capsule
are wheel-only scrollers with `acceptedButtons: Qt.NoButton`):

- chevron (`trayToggle`, 32px, `chevron-down.svg`, glyph `iconRotation`
  180 when open, Button `rotation` stays 0 — only the inner `iconImage`
  rotates — with zero style insets so the Button background stays contained
  at intermediate glyph angles (45/90/135deg)):
  `controlCenter.toggle()`
- audio left = mute, right = section 0, wheel = volume (in `AudioModule`)
- network = section 1 (in `NetworkModule`, Bluetooth glyph included;
  SSID label capped at 160px elided, full name in ControlCenter)
- battery = section 3 (in `BatteryModule`)
- tray icons = own activate / middle / right (in `TrayModule`); tray icons
  have no wheel action, so the viewport wheel scroller cannot shadow one

Fallback `audioPopup` / `networkPopup` / `batteryPopup` props are still
accepted when no ControlCenter is wired. Notifications + clock stay left of
center; screenshot + stats stay right; workspaces + media stay center.

## Sizing (validated by live geometry harness, real child bounds)

Side slots are fixed to `leftRow` / `rightRow.implicitWidth`; the center
slot takes the remainder. The center row centers when it fits and
left-aligns inside a horizontal `Flickable` (wheel/drag) on overflow, so no
control is ever clipped away: `clip: true` on the slot is a paint backstop
only, never the access path.

- Workspace is budgeted first (`workspaceNeed` = natural width + 20px
  capsule + 8px spacing); media gets the remainder: full needs 320px
  (title hard-capped at 200px in `MediaModule`), compact needs 140px.
  `compactMedia` never reads `mediaModule.implicitWidth` (cyclic). When
  even compact cannot fit, the capsule hides and a 32px mini entry
  (`mediaMini`) keeps media reachable through the scroller. Verified live:
  a real `Row` of a 160px-capped `Text` reports `implicitWidth` 188
  (laid-out widths, invisible children excluded), so the paint caps already
  bound the modules, which report the plain `Row` extent.
- SSID text is capped at 160px elided (`maxLabelWidth`); the connected-BT
  count at 36px. The full name is preserved in ControlCenter. The module
  also exposes compact-independent `fullWidth` (icons + capped label +
  capped count + all gaps, as if shown).
- Tray viewport capped at 148px (~5 icons), wheel/drag scrollable.
- `tightSides` drops the SSID text when worst-case sides would starve the
  center (< 200px left). Worst case = measured reserve minus the network
  module's actual width plus its `fullWidth`: toggling compact moves both
  terms equally, so the result is exactly invariant and cannot oscillate.
  Proven live on a real module instance: `fullWidth` bitwise identical
  across a compact toggle, sizes non-negative, no binding-loop warnings.

Measured live (real bar, this machine): 800 reserve 505 / avail 295,
1024 reserve 543 / avail 481, 1440/1920 reserve 800 / avail 640/1120 — all
child bounds inside the bar at every width, supported floor 800/1024+.
Adversarial mocks (150-char label, 12 tray icons, 800px center content):
caps hold (160 / 148), every tail scroll-reachable, row re-centers when
space frees.

`compactClock` (`width < 1150`) hides the weekday/date but keeps `HH:mm`.

## ControlCenter anchor tracking

Column order is header, global quick controls (brightness/DND/night/awake),
divider, section tabs (`Audio/Network/Bluetooth/Power`), body content: tabs
sit directly above the body with no extra spacer. The embedded
`NetworkPanel` hides its duplicate internal Network/Bluetooth selector
(`showTabSelector: false`); standalone `NetworkPopup` keeps it. `AudioPanel`
keeps its own Output/Input/Streams tabs (not a duplicate).

Anchored left-aligned under the whole tray: `edges Bottom|Left`,
`gravity Bottom|Right` (gravity = expansion direction: down + right from
the tray's bottom-left point), `margins.top: 4` (height budget subtracts
`40 + 4 + 16`). Quickshell computes the item-relative anchor rect only at
show time, so every geometry path calls the verified
`anchor.updateAnchor()` API (re-assigning the same item would be a no-op
for motion). Tracked: tray item x/y/width/height, a `TransformWatcher`
over the Bar-to-tray path (fires on ancestor motion that leaves the tray's
own geometry untouched), `screenChanged`, and a fresh recalc on every open.
Item-relative only, no global coords.

Live open-move harness: with the popup open, moving only an ancestor by
120px advances the recalc counter exactly once, the tray rect (mapped
item-relative to the window content item) shifts by exactly 120, and the
popup stays open at full reveal on the same anchor item.

## ControlCenter motion

Single `reveal` progress drives `opacity`, `0.96 + 0.04 * reveal` scale with
`transformOrigin: Item.TopLeft`, and a `-8 * (1 - reveal)` content
`Translate` (panel `clip: true`, no invalid `y` animation on
`anchors.fill`, no spring/overshoot). `setOpen(true)` while already open
returns early (section switches change `selectedTab` only); reopen after an
interrupted close restarts the same `NumberAnimation` from its current
value. Explicit close: header `x.svg` button + `Escape`.
