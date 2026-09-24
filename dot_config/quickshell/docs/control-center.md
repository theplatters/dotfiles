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

The legacy `audioPopup` / `networkPopup` / `batteryPopup` fallback props and
their wrapper popups were removed (D1): every bar control routes into the
ControlCenter sections and there is no fallback path. Notifications + clock
stay left of center; screenshot + stats stay right; workspaces + media stay
center.

## Notification / DND / banner ownership

Notification history ownership is documented in the single shell-level
owner section below (S-002, "Notification history: single shell-level
owner"), which supersedes any bell-module note.

DND lives in ControlCenter's global quick controls
(`widgets/ControlCenter.qml`): the DND toggle flips `notifServer.inhibit`.
While inhibited the banner surface hides and clears its rows model-only;
history keeps collecting independently, and approvals are untouched — DND
suppresses banners only.

Banners (`widgets/NotificationPopout.qml`, one `PanelWindow` outside the
per-screen `Variants` in `shell.qml`, so a single surface on the default
screen) are ephemeral toasts, not popouts: no `requestedOpen`/`setOpen`
lifecycle, just `visible` bound to `activeNotifs.count > 0 && !inhibited`
plus a 6 s `Timer` per toast. Expiry and DND-hiding remove the banner row
only (`removeBanner`/`clearBanners` never touch `tracked`); only an explicit
close/activate (`userDismiss`) dismisses the notification object itself,
which then closes history via its `closed` handler.

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
(`showTabSelector: false`); the standalone `NetworkPopup` wrapper was
deleted (D1). `AudioPanel`
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

## Popout grammar (D1; S-006 to S-010)

One rule set for the five anchored bar popouts — media, calendar, project
overview, ControlCenter, Wi-Fi password. Banners are excluded (ephemeral
toasts, not popouts: no `requestedOpen`/`setOpen` lifecycle). The bell
history popup is out of scope (owned by `NotificationModule`, `expanded`
state, not `requestedOpen`).

- Exclusive-open per screen (S-006): opening any of the five closes the
  other four on that screen. Implemented in `shell.qml`'s per-screen
  delegate (`closeOthers(except)` + one `onRequestedOpenChanged`
  `Connections` per popout): media/calendar close via `setOpen(false)`,
  the overview via its full `closePopup()` (aborting in-flight resume
  work), the password via `cancelAndClose()` (single cancel path), and
  the ControlCenter stays visible while the password dialog it opened is
  up (modal dialog over its originator). Each popout keeps its own
  `requestedOpen` + `setOpen(open)` lifecycle with at most one
  `onVisibleChanged`.
- Escape (S-007): every dismissible popout closes on Escape via the same
  `Shortcut` ControlCenter has (media/calendar call `setOpen(false)`,
  the overview calls its full `closePopup()`, the password cancels +
  dismisses, the bell already had one).
- Outside-click (S-008): one transparent input-only layer per screen
  (`popoutCatcher` in `shell.qml`, `screen: barWindow.screen`),
  `margins.top: barWindow.implicitHeight` so the bar area is excluded,
  visible only while an anchored popout is open
  (`requestedOpen || visible` on media/calendar/overview/CC). A click on
  it closes the open popout and is consumed by the dismissal (standard
  popover behavior). Mechanics: `WlrLayer.Top` (above standard windows,
  below the `Overlay` popouts) so popout content receives its clicks
  first; bar controls keep working because the bar area is excluded —
  clicking another bar control switches popouts directly with no dead
  first click; `WlrKeyboardFocus.None` so it never steals keyboard
  focus while merely open. The modal PasswordPopup keeps its own
  fullscreen `Theme.scrim` backdrop and is excluded from the catcher's
  visibility gate.
- Anchor tracking (S-009): item-relative only, no global coords. Two
  flavors of the same grammar: `PopupWindow` popouts (ControlCenter,
  media) anchor `item: root.anchorItem` and recalculate via the verified
  `anchor.updateAnchor()` API on `anchorItem`/`screen` changes, on
  `x/y/width/height` of the anchor, on every open, and via a
  `TransformWatcher` over the Bar-to-anchor path (`anchorScope`, bound
  to the `Bar` in `shell.qml`) that fires on ancestor motion leaving
  the anchor's own geometry untouched. `PanelWindow` popouts
  (calendar, project overview) derive screen-local margins item-relative
  from the anchor (`mapToItem` into the bar window content, never
  `mapToGlobal`) in `refreshPosition()`, with the same tracking signals
  plus an equivalent `TransformWatcher` calling `refreshPosition()`.
- Motion (S-010): every popout uses the ControlCenter single-`reveal`
  pattern — one `reveal` progress drives `opacity` (+ `scale` and a
  short content `Translate`), a single `NumberAnimation` each way
  (`OutCubic` in, `InCubic` out). `setOpen(true)` while steady returns
  early; reopen after an interrupted close restarts from the current
  value — never reset to 0 on open, so mid-close reopens reverse
  mid-fade instead of flashing.
- Password placement (S-010): the Wi-Fi password dialog is per-screen
  (instantiated in the per-screen delegate like the other popouts), so
  it follows the screen whose ControlCenter asked for it instead of
  centering shell-globally. It stays modal: fullscreen `Overlay` with
  its own scrim backdrop.

## Tab routing + entries (S-011, S-012)

One-way routing: ControlCenter owns `selectedTab`; the embedded
`NetworkPanel` is a slave (`activeTab` 0 = Network, 1 = Bluetooth). The
mapping lives in CC's `onSelectedTabChanged` only; the old reverse
`Connections` (panel `activeTab` writing back to `selectedTab`) is
deleted. All four tab buttons route through the single `openSection(tab)`
path (which sets the owned tab and opens; the change handler routes
into the panel, resets `bodyFlick.contentY`, and calls
`syncDiscovery()`). The hidden inner selector stays inert
(`showTabSelector: false`). Discovery invariant preserved:
`syncDiscovery()` runs only when CC is open + visible on the matching
tab (`active && visible && activeTab === N`).

Bar entry points: chevron toggles, audio left = mute / right = section
0, network = section 1, battery = section 3, project button = overview
popup (falls back to the planner list). Bluetooth is intentionally
second-level (default decision): no bar entry — reach it through the
ControlCenter's Bluetooth tab (section 2), which slaves the panel to
its Bluetooth page.

## Notification history: single shell-level owner (S-002, supersedes the bell-module note above)

History lives in one `widgets/NotificationHistory.qml` instance in
`shell.qml` (`id: notifHistory`, bound to the shared `notifServer`). Every
per-bar `NotificationModule` is a view over it (`history` prop): badge count,
popup list, Clear All, and row actions (`dismissAt`/`activateAt` in the
store). Semantics preserved: `tracked = true` on insert, 10-entry cap with
oldest evicted via `dismiss()`, closed-handler removal, no double-remove.
Banners (`NotificationPopout`) stay independent (bare references, never
tracked).

## Bar collapse order (S-015, verified against Bar.qml thresholds)

As `bar.width` shrinks, capsules compact in this order (subject to retune):

1. stats labels (`compactStats`, `width < 1400`): Mem/CPU/Disk values stay,
   labels hide; the capsule never highlights (no click action).
2. network SSID + BT count (`compactNetwork`, `width < 1200`, or earlier via
   `tightSides` when worst-case sides would starve the center below 200px).
3. clock weekday/date (`compactClock`, `width < 1150`): `ddd` and `MM-dd`
   hide, `HH:mm` stays.
4. battery percent (`compactBattery`, `width < 1000`): icon stays.

Media is not width-gated: `compactMedia` compares the measured center
remainder (workspace budgeted first) against bounded needs (full 320px,
compact 140px); when even compact cannot fit, the capsule hides and a 32px
mini entry keeps media reachable through the center scroller.
