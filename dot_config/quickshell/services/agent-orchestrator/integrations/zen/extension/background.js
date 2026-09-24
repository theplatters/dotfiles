/* QS Zen Context — persistent MV2 background (fail-closed, local only).
 *
 * What it does:
 * - Assigns each non-private Zen window a random per-window marker
 *   `[qs-zen-<8 lowercase hex>] ` via `browser.windows.update({titlePreface})`.
 *   The native host (`qs-zen-native-host.py`) maps the focused tab to the
 *   focused compositor window ONLY when the Hyprland active window class is
 *   Zen and its title starts with the exact marker carried by this message.
 *   Page-title heuristics are never used; until the marker appears in the
 *   compositor title the host fails closed (writes `{}`).
 * - Publishes the active tab of the LAST-FOCUSED window only, on tab
 *   activation / tab update / window focus change / window or tab removal,
 *   plus a <=10s heartbeat. Requests are serialized and coalesced: a
 *   monotonically increasing generation is captured at request time and
 *   re-checked after every await, so an older async tab query can never
 *   overwrite (or send after) a newer focus state.
 * - Skips private/incognito windows and tabs and non-http(s) URLs: those
 *   states post an explicit `{type:"invalidate"}` message so the host
 *   immediately writes `{}` (collector evicts to the honest `zen-title`
 *   fallback) instead of leaving a stale URL. Focus loss
 *   (`WINDOW_ID_NONE`) posts invalidate the same way. On port disconnect
 *   the host sees EOF and clears to `{}` on its own; the next event
 *   reconnects.
 * - No clipboard, no key injection, no network, no profile-database reads.
 *   Only `tabs` + `windows` reads, one `windows.update` per window, and
 *   `runtime.connectNative` to `local.quickshell.zen_context`.
 */

(function () {
  "use strict";

  var HOST_NAME = "local.quickshell.zen_context";
  var HEARTBEAT_MS = 10000;
  var MARKER_RE = /^\[qs-zen-[0-9a-f]{8}\] $/;
  var HTTP_RE = /^https?:\/\//;
  var MAX_URL_CHARS = 2048;

  function getApi() {
    if (typeof browser !== "undefined" && browser && browser.tabs && browser.runtime) {
      return browser;
    }
    if (typeof chrome !== "undefined" && chrome && chrome.tabs && chrome.runtime) {
      return chrome;
    }
    return null;
  }

  function newMarker(rng) {
    // 32-bit random hex, lowercase. `rng` is an injectable byte source for tests.
    var bytes;
    if (rng) {
      bytes = rng();
    } else if (
      typeof crypto !== "undefined" &&
      crypto &&
      typeof crypto.getRandomValues === "function"
    ) {
      bytes = crypto.getRandomValues(new Uint8Array(4));
    } else {
      // No crypto: fail closed (no marker, no publish). Callers treat null
      // as "cannot mark this window".
      return null;
    }
    var hex = "";
    for (var i = 0; i < 4; i++) {
      var b = bytes[i] & 0xff;
      hex += (b < 16 ? "0" : "") + b.toString(16);
    }
    return "[qs-zen-" + hex + "] ";
  }

  function isSupportedUrl(url) {
    if (typeof url !== "string") return false;
    // Align the native host (`_validate_url`): strip, bound to
    // MAX_URL_CHARS, require http(s), reject control chars (< 0x20).
    // Oversized/unsupported callers take the invalidate route; never
    // truncate the URL here.
    var text = url.trim();
    if (!text || text.length > MAX_URL_CHARS) return false;
    if (!HTTP_RE.test(text)) return false;
    if (/[\x00-\x1F]/.test(text)) return false;
    return true;
  }

  // State: windowId -> marker. Markers are never derived from page content.
  var markers = new Map();
  var port = null;
  var seq = 0; // publication generation (coalescing).
  var inFlight = false;
  var pendingReason = null;
  var heartbeatTimer = null;

  function api() {
    return getApi();
  }

  function connect() {
    var a = api();
    if (!a) return null;
    if (port) return port;
    try {
      var p = a.runtime.connectNative(HOST_NAME);
      port = p;
      var onDisconnect = function () {
        port = null;
      };
      if (p.onDisconnect && typeof p.onDisconnect.addListener === "function") {
        p.onDisconnect.addListener(onDisconnect);
      }
      return port;
    } catch (e) {
      port = null;
      return null;
    }
  }

  function postMessage(msg) {
    var p = port || connect();
    if (!p) return false;
    try {
      p.postMessage(msg);
      return true;
    } catch (e) {
      try {
        if (p.disconnect) p.disconnect();
      } catch (ignored) {}
      port = null;
      return false;
    }
  }

  function postInvalidate(reason, marker) {
    var msg = { type: "invalidate", reason: String(reason || "unsupported") };
    if (typeof marker === "string" && MARKER_RE.test(marker)) {
      msg.marker = marker;
    }
    try {
      msg.updatedAtMs = Date.now();
    } catch (e) {
      msg.updatedAtMs = 0;
    }
    postMessage(msg);
  }

  function ensureMarker(windowId, incognito, updateFn) {
    // Returns a promise of the marker string, or null for private windows /
    // failures (fail closed: callers invalidate instead of publishing).
    if (incognito) return Promise.resolve(null);
    var existing = markers.get(windowId);
    if (existing && MARKER_RE.test(existing)) return Promise.resolve(existing);
    var marker = newMarker();
    if (!marker) return Promise.resolve(null);
    markers.set(windowId, marker);
    var a = api();
    var doUpdate =
      updateFn ||
      (a && a.windows && typeof a.windows.update === "function"
        ? function (id, info) {
            return a.windows.update(id, info);
          }
        : null);
    if (!doUpdate) {
      // Cannot set the preface (e.g. no windows API in a test): keep the
      // marker locally so message flow stays testable; the real host still
      // fails closed until the compositor title actually shows it.
      return Promise.resolve(marker);
    }
    return Promise.resolve()
      .then(function () {
        return doUpdate(windowId, { titlePreface: marker });
      })
      .then(function () {
        return marker;
      })
      .catch(function () {
        markers.delete(windowId);
        return null;
      });
  }

  function getLastFocusedWindow() {
    var a = api();
    if (!a || !a.windows) return Promise.resolve(null);
    // `getLastFocused` with populate:false keeps the query cheap.
    if (typeof a.windows.getLastFocused === "function") {
      return Promise.resolve(a.windows.getLastFocused({})).catch(function () {
        return null;
      });
    }
    if (typeof a.windows.getCurrent === "function") {
      return Promise.resolve(a.windows.getCurrent({})).catch(function () {
        return null;
      });
    }
    return Promise.resolve(null);
  }

  function getActiveTab(windowId) {
    var a = api();
    if (!a || !a.tabs || typeof a.tabs.query !== "function") {
      return Promise.resolve(null);
    }
    return Promise.resolve(a.tabs.query({ active: true, windowId: windowId }))
      .then(function (tabs) {
        if (!tabs || tabs.length !== 1) return null;
        return tabs[0];
      })
      .catch(function () {
        return null;
      });
  }

  function doPublish(gen, reason) {
    // One publication attempt for generation `gen`. After every await the
    // generation AND the live focus are re-checked; a superseded generation
    // returns silently without sending (never republishes stale tab data).
    var focusedWin = null;
    var marker = null;
    return getLastFocusedWindow()
      .then(function (win) {
        if (gen !== seq) return "stale";
        if (!win || typeof win.id === "undefined") {
          postInvalidate("no-focused-window");
          return "done";
        }
        if (win.incognito) {
          postInvalidate("incognito-window");
          return "done";
        }
        // A window that is known-unfocused is focus loss, not a URL.
        if (win.focused === false) {
          postInvalidate("window-unfocused");
          return "done";
        }
        focusedWin = win;
        return ensureMarker(win.id, !!win.incognito).then(function (m) {
          marker = m;
          if (gen !== seq) return "stale";
          if (!marker) {
            // Private window or titlePreface unavailable: fail closed.
            postInvalidate("no-marker");
            return "done";
          }
          return getActiveTab(win.id).then(function (tab) {
            if (gen !== seq) return "stale";
            // Re-check focus after the awaits: the window must still be the
            // last-focused window and the tab must still be its active tab.
            return getLastFocusedWindow().then(function (fresh) {
              if (gen !== seq) return "stale";
              if (
                !fresh ||
                fresh.id !== focusedWin.id ||
                fresh.incognito ||
                fresh.focused === false
              ) {
                // Focus moved during the awaits: do not send the old tab.
                // The newer event's generation owns the next send.
                return "stale";
              }
              if (!tab || tab.windowId !== focusedWin.id || tab.active === false) {
                return "stale";
              }
              if (tab.incognito) {
                postInvalidate("incognito-tab", marker);
                return "done";
              }
              if (!isSupportedUrl(tab.url)) {
                postInvalidate("unsupported-url", marker);
                return "done";
              }
              var nowMs = Date.now();
              var title =
                typeof tab.title === "string" && tab.title.trim()
                  ? tab.title.trim().slice(0, 1024)
                  : "";
              var msg = {
                type: "update",
                marker: marker,
                url: tab.url,
                title: title,
                updatedAtMs: nowMs,
                windowId: focusedWin.id,
                tabId: tab.id,
              };
              postMessage(msg);
              return "done";
            });
          });
        });
      })
      .catch(function () {
        // Transient browser-API failure: send nothing (the host record goes
        // stale and the collector falls back after 30s). Never throw out of
        // the background page.
        return "done";
      });
  }

  function runPublish(gen, reason) {
    inFlight = true;
    return doPublish(gen, reason || "event").then(function () {
      inFlight = false;
      if (pendingReason !== null && gen !== seq) {
        var r = pendingReason;
        pendingReason = null;
        var g = seq;
        return runPublish(g, r);
      }
      pendingReason = null;
      return undefined;
    });
  }

  function requestPublish(reason) {
    seq += 1;
    var gen = seq;
    if (inFlight) {
      pendingReason = reason || "event";
      return;
    }
    runPublish(gen, reason || "event");
  }

  function wireEvents() {
    var a = api();
    if (!a) return;
    try {
      if (a.tabs && a.tabs.onActivated) {
        a.tabs.onActivated.addListener(function () {
          requestPublish("tab-activated");
        });
      }
      if (a.tabs && a.tabs.onUpdated) {
        a.tabs.onUpdated.addListener(function (_tabId, changeInfo, _tab) {
          // URL/title changes only; loading noise still coalesces.
          if (
            changeInfo &&
            (changeInfo.url || changeInfo.title || changeInfo.status === "complete")
          ) {
            requestPublish("tab-updated");
          }
        });
      }
      if (a.tabs && a.tabs.onRemoved) {
        a.tabs.onRemoved.addListener(function () {
          requestPublish("tab-removed");
        });
      }
      if (a.windows && a.windows.onFocusChanged) {
        a.windows.onFocusChanged.addListener(function (windowId) {
          var noneId =
            (a.windows && a.windows.WINDOW_ID_NONE) !== undefined
              ? a.windows.WINDOW_ID_NONE
              : -1;
          if (windowId === noneId) {
            requestPublish("focus-lost");
            return;
          }
          requestPublish("window-focus");
        });
      }
      if (a.windows && a.windows.onCreated) {
        a.windows.onCreated.addListener(function (win) {
          if (win && typeof win.id !== "undefined" && !win.incognito) {
            ensureMarker(win.id, false);
          }
        });
      }
      if (a.windows && a.windows.onRemoved) {
        a.windows.onRemoved.addListener(function (windowId) {
          markers.delete(windowId);
          // The focused window closed: the next focused window publishes on
          // its own focus event; proactively re-publish in case it does not.
          requestPublish("window-removed");
        });
      }
    } catch (e) {
      // Event wiring is best-effort; the heartbeat still publishes.
    }
  }

  function primeExistingWindows() {
    var a = api();
    if (!a || !a.windows || typeof a.windows.getAll !== "function") {
      return Promise.resolve();
    }
    return Promise.resolve(a.windows.getAll({}))
      .then(function (wins) {
        if (!wins) return;
        var chain = Promise.resolve();
        wins.forEach(function (win) {
          if (win && typeof win.id !== "undefined" && !win.incognito) {
            chain = chain.then(function () {
              return ensureMarker(win.id, false);
            });
          }
        });
        return chain;
      })
      .catch(function () {})
      .then(function () {
        requestPublish("startup");
      });
  }

  function startHeartbeat() {
    stopHeartbeat();
    try {
      heartbeatTimer = setInterval(function () {
        requestPublish("heartbeat");
      }, HEARTBEAT_MS);
      // Do not keep a test harness alive on the interval.
      if (
        heartbeatTimer &&
        typeof heartbeatTimer.unref === "function"
      ) {
        heartbeatTimer.unref();
      }
    } catch (e) {
      heartbeatTimer = null;
    }
  }

  function stopHeartbeat() {
    if (heartbeatTimer !== null) {
      try {
        clearInterval(heartbeatTimer);
      } catch (e) {}
      heartbeatTimer = null;
    }
  }

  // Boot in a real extension only (tests import the helpers without side
  // effects: no `browser`/`chrome` global means no wiring, no timers).
  if (typeof browser !== "undefined" || typeof chrome !== "undefined") {
    connect();
    wireEvents();
    primeExistingWindows();
    startHeartbeat();
  }

  var exported = {
    HOST_NAME: HOST_NAME,
    HEARTBEAT_MS: HEARTBEAT_MS,
    MARKER_RE: MARKER_RE,
    MAX_URL_CHARS: MAX_URL_CHARS,
    newMarker: newMarker,
    isSupportedUrl: isSupportedUrl,
    // Test hooks (reset between cases):
    _state: function () {
      return {
        markers: markers,
        getSeq: function () {
          return seq;
        },
      };
    },
    _reset: function () {
      markers.clear();
      try {
        if (port && port.disconnect) port.disconnect();
      } catch (e) {}
      port = null;
      seq = 0;
      inFlight = false;
      pendingReason = null;
    },
    _setPort: function (p) {
      port = p;
    },
    _ensureMarker: ensureMarker,
    _doPublish: doPublish,
    _requestPublish: requestPublish,
  };

  if (typeof globalThis !== "undefined") {
    globalThis.QSZen = exported;
  }
  if (typeof module !== "undefined" && module && module.exports) {
    module.exports = exported;
  }
})();
