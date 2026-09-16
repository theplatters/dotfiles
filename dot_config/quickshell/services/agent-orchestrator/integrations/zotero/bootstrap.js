/* QS Active Reader — Zotero 7+ bootstrapped plugin (metadata only).
 *
 * What it does:
 * - Registers a local endpoint `GET /qs-active-reader` on Zotero's existing
 *   connector server (default `http://127.0.0.1:23119`, same server documented
 *   in "Zotero Connector HTTP Server"). The endpoint returns the
 *   Zotero-INTERNAL active reader only: server/library/item/attachment keys,
 *   title, current parent memberships + ancestor collection keys, local
 *   version, and a stable `zotero://select/...` URI. No page numbers,
 *   annotations, full text, or file bytes are ever returned.
 * - Listens to reader/tab events (`Zotero.Reader.registerEventListener`,
 *   `Zotero.Notifier` for `item`/`collection` changes) so the endpoint always
 *   reflects tab switches, detached reader windows, and reader closure.
 *
 * What it deliberately does NOT do (fail-closed, see README.md):
 * - It never writes `window_id`/`pid` (Hyprland address/client PID). The
 *   in-process JS API (`Zotero.Reader` tab manager, `Zotero.getMainWindows()`)
 *   exposes no native OS window handle that maps to a compositor window, and
 *   the local library API (`/api/`, "Local API" docs) exposes library data
 *   only — no focus/reader state. Fabricating a native id or publishing a
 *   process-global "last active reader" as if window-bound would misattribute
 *   across multiple/detached windows. The external bridge
 *   (`qs-zotero-bridge.py`) samples `hyprctl activewindow -j` in the SAME tick
 *   as this endpoint and writes the verified `QS_ZOTERO_CONTEXT_FILE`.
 *
 * Internal-API notes (sources: zotero/zotero `chrome/content/zotero`,
 * `zotero/reader`, and the "Zotero JavaScript API" docs):
 * - `Zotero.getActiveZoteroPane().getSelectedItems()` gives library
 *   selection (not reader focus); reader focus comes from `Zotero.Reader`.
 * - Reader tabs: `Zotero.Reader.getSelectedReader?.()` /
 *   `Zotero.Reader._readers` (version-dependent; probed defensively below).
 *   Each reader exposes its attachment item id; the parent regular item,
 *   collections (`item.getCollections()` → collection keys via
 *   `Zotero.Collections.get(id).key`), and ancestors (parent-collection walk)
 *   are resolved at request time so membership is always current.
 * - Library identity: `item.libraryID` + `Zotero.Libraries` /
 *   `Zotero.Groups` for `library_type`; `Zotero-Server-ID` is read from any
 *   local-API response header when available (Zotero 10+; earlier servers
 *   leave `server_id` empty and never match a pinned registry entry).
 * - Local enrichment uses the on-disk database through the public JS objects
 *   above — never direct SQLite reads (the collector likewise never reads
 *   `zotero.sqlite`).
 *
 * Compatibility: Zotero 7.0+ (bootstrapped, `onMainWindowLoad` hooks; the
 * manifest caps `strict_max_version` at the tested Zotero line — `10.0.*` —
 * and must be bumped when a newer Zotero is tested). No XUL overlay is used.
 * The code probes version-dependent reader accessors defensively and degrades
 * to `state:"closed"` rather than throwing. `server_id` comes from the
 * in-process `Zotero.Server.LocalAPI.getServerID()` on Zotero 10+ (the HTTP
 * server rejects browser-looking requests there), with a guarded local-API
 * header read as the fallback for Zotero 7-9; builds without a stable server
 * ID report `server_id:""` and match nothing pinned (fail closed).
 */

var QSActiveReader = {
  id: null,
  version: null,
  rootURI: null,
  initialized: false,
  _notifierID: null,

  init({ id, version, rootURI }) {
    if (this.initialized) return;
    this.id = id;
    this.version = version;
    this.rootURI = rootURI;
    this.initialized = true;
    try {
      this._registerEndpoint();
    } catch (e) {
      Zotero.debug(`QSActiveReader: endpoint register failed: ${e}`, 2);
    }
    try {
      // Keep the endpoint fresh across tab switches and library edits.
      // Reader events (open/close/focus) + library notifier (item/collection
      // membership changes) both invalidate the cached snapshot.
      if (Zotero.Reader && Zotero.Reader.registerEventListener) {
        const bump = () => { this._seq++; };
        for (const type of ["open", "close", "focus", "unload"]) {
          try {
            Zotero.Reader.registerEventListener(type, bump, this.id);
          } catch (e) { /* older builds lack some types; ignore */ }
        }
      }
      if (Zotero.Notifier && Zotero.Notifier.registerObserver) {
        this._notifierID = Zotero.Notifier.registerObserver(
          { notify: () => { this._seq++; } },
          ["item", "collection"],
          "qs-active-reader",
        );
      }
    } catch (e) {
      Zotero.debug(`QSActiveReader: listener setup failed: ${e}`, 2);
    }
  },

  _seq: 0,

  _registerEndpoint() {
    const Z = Zotero;
    if (!Z.Server || !Z.Server.Endpoints) return;
    const self = this;
    const EP = "/qs-active-reader";
    if (Z.Server.Endpoints[EP]) return;
    const Handler = function () {};
    Handler.prototype = {
      supportedMethods: ["GET"],
      supportedDataTypes: ["application/json", "text/plain"],
      init(data, sendResponseCallback) {
        let body;
        try {
          body = JSON.stringify(self.snapshot());
        } catch (e) {
          sendResponseCallback(500, "application/json", JSON.stringify({ error: String(e && e.message || e) }));
          return;
        }
        sendResponseCallback(200, "application/json", body);
      },
    };
    Z.Server.Endpoints[EP] = Handler;
  },

  _unregisterEndpoint() {
    try {
      if (Zotero.Server && Zotero.Server.Endpoints) {
        delete Zotero.Server.Endpoints["/qs-active-reader"];
      }
    } catch (e) {}
  },

  // Active reader snapshot (Zotero-internal only, no OS/window binding).
  snapshot() {
    const out = {
      generator: "qs-active-reader/0.1.0",
      served_at_ms: Date.now(),
      seq: this._seq,
      state: "closed",
      readers_open: 0,
    };
    let reader = null;
    try {
      reader = this._selectedReader();
    } catch (e) {
      Zotero.debug(`QSActiveReader: reader probe failed: ${e}`, 2);
    }
    if (!reader) return out;
    let attachment = null;
    let parent = null;
    try {
      const ids = this._readerItemIDs(reader);
      attachment = ids.attachment;
      parent = ids.parent;
    } catch (e) {
      Zotero.debug(`QSActiveReader: item resolve failed: ${e}`, 2);
      return out;
    }
    if (!attachment && !parent) return out;
    const doc = parent || attachment;
    let collections = [];
    let ancestors = [];
    try {
      collections = this._directCollectionKeys(doc);
      ancestors = this._ancestorCollectionKeys(collections);
    } catch (e) {
      Zotero.debug(`QSActiveReader: collection resolve failed: ${e}`, 2);
    }
    out.state = "open";
    out.readers_open = this._openReaderCount();
    out.server_id = this._serverID();
    out.library_type = doc.libraryType || (doc.libraryID == 1 ? "user" : "group");
    // Local-API convention: the personal library is addressed as users/0.
    // The collector treats registry user/"0" as the server-bound alias.
    out.library_id = String(doc.libraryID == 1 ? 0 : doc.libraryID);
    out.item_key = parent ? parent.key : attachment.key;
    out.attachment_key = attachment && parent && attachment.key !== parent.key
      ? attachment.key
      : (attachment && !parent ? attachment.key : null);
    out.title = this._titleOf(parent || attachment);
    out.collections = collections;
    out.ancestor_collections = ancestors;
    out.ancestors = ancestors; // back-compat alias for older bridges
    out.version = typeof doc.version === "number" ? doc.version : null;
    out.zotero_uri = this._selectURI(doc);
    out.uri = out.zotero_uri;
    return out;
  },

  _selectedReader() {
    const R = Zotero.Reader;
    if (!R) return null;
    if (typeof R.getSelectedReader === "function") {
      try {
        const r = R.getSelectedReader();
        if (r) return r;
      } catch (e) {}
    }
    // Fallbacks across Zotero 7/8 internals (all in-process, no OS handle).
    const pools = [R._readers, R._readersByTabID, R.readers];
    for (const pool of pools) {
      if (!pool) continue;
      try {
        if (Array.isArray(pool) && pool.length) return pool[0];
        if (typeof pool === "object") {
          const vals = Object.values(pool).filter(Boolean);
          if (vals.length) return vals[0];
        }
      } catch (e) {}
    }
    // Tab-store fallback: Zotero_Tabs holds reader tabs with itemIDs.
    try {
      const tabs = (typeof Zotero_Tabs !== "undefined" && Zotero_Tabs._tabs) || [];
      const rt = tabs.find((t) => t && t.type === "reader" && t.data && t.data.itemID);
      if (rt) return { __tabItemID: rt.data.itemID };
    } catch (e) {}
    return null;
  },

  _openReaderCount() {
    try {
      const R = Zotero.Reader;
      if (R && Array.isArray(R._readers)) return R._readers.length;
      if (typeof Zotero_Tabs !== "undefined" && Array.isArray(Zotero_Tabs._tabs)) {
        return Zotero_Tabs._tabs.filter((t) => t && t.type === "reader").length;
      }
    } catch (e) {}
    return 0;
  },

  _readerItemIDs(reader) {
    // Preferred: reader.itemID is the opened attachment's numeric id.
    const rawID = reader.itemID != null
      ? reader.itemID
      : (reader._itemID != null ? reader._itemID : reader.__tabItemID);
    if (rawID == null) return { attachment: null, parent: null };
    const attachment = Zotero.Items.get(rawID) || Zotero.Items.getByLibraryAndKey
      ? (Zotero.Items.get(rawID) || null)
      : null;
    if (!attachment) return { attachment: null, parent: null };
    let parent = null;
    try {
      if (attachment.isAttachment && attachment.isAttachment()) {
        const pid = attachment.parentID;
        if (pid) parent = Zotero.Items.get(pid) || null;
      } else if (attachment.isRegularItem && attachment.isRegularItem()) {
        parent = attachment;
      }
    } catch (e) {}
    return { attachment, parent };
  },

  _directCollectionKeys(item) {
    if (!item || typeof item.getCollections !== "function") return [];
    const ids = item.getCollections() || [];
    const keys = [];
    for (const id of ids) {
      try {
        const c = Zotero.Collections.get(id);
        if (c && c.key && /^[A-Z0-9]{8}$/.test(c.key) && !keys.includes(c.key)) keys.push(c.key);
      } catch (e) {}
    }
    return keys;
  },

  _ancestorCollectionKeys(directKeys) {
    const seen = new Set(directKeys);
    const out = [];
    const byKey = {};
    try {
      for (const c of Zotero.Collections.getAll ? Zotero.Collections.getAll() : []) {
        if (c && c.key) byKey[c.key] = c;
      }
    } catch (e) { return []; }
    const visit = (key) => {
      const c = byKey[key];
      if (!c) return;
      let pid = null;
      try {
        pid = c.parentID != null ? c.parentID : (c.parentKey ? byKey[c.parentKey]?.id : null);
      } catch (e) { pid = null; }
      if (!pid) return;
      try {
        const p = Zotero.Collections.get(pid);
        if (p && p.key && !seen.has(p.key)) {
          seen.add(p.key);
          out.push(p.key);
          visit(p.key);
        }
      } catch (e) {}
    };
    for (const k of directKeys) visit(k);
    return out;
  },

  _titleOf(item) {
    try {
      const t = item.getField ? (item.getField("title") || item.getDisplayTitle?.()) : null;
      if (t && String(t).trim()) return String(t).trim().slice(0, 1024);
    } catch (e) {}
    return null;
  },

  _selectURI(item) {
    try {
      // Stable select link: zotero://select/library/items/<key> (user) or
      // zotero://select/groups/<libraryID>/items/<key> (group).
      const key = item.key;
      if (!key || !/^[A-Z0-9]{8}$/.test(key)) return null;
      if (item.libraryType === "group" || (item.libraryID != null && item.libraryID != 1)) {
        return `zotero://select/groups/${item.libraryID}/items/${key}`;
      }
      return `zotero://select/library/items/${key}`;
    } catch (e) { return null; }
  },

  _serverID() {
    // Zotero 10+ keeps the stable local-API server ID in process
    // (`Zotero.Server.LocalAPI.getServerID()`, loaded at server startup into
    // the `localAPI` settings row) and reports it as `Zotero-Server-ID` on
    // every local-API response. Read it in-process: since Zotero 10 the HTTP
    // server drops requests that look browser-originated (User-Agent starting
    // with `Mozilla/` or any Origin header) unless they carry
    // `Zotero-Allowed-Request`, so an internal XHR only works with that
    // header and is only a fallback for builds without the accessor.
    if (this._cachedServerID) return this._cachedServerID;
    try {
      const api = Zotero.Server && Zotero.Server.LocalAPI;
      if (api && typeof api.getServerID === "function") {
        const sid = api.getServerID();
        if (sid && String(sid).trim()) {
          this._cachedServerID = String(sid).trim();
          return this._cachedServerID;
        }
      }
    } catch (e) {
      // Accessor unavailable (server not initialized) or older build.
    }
    try {
      const req = new XMLHttpRequest();
      req.open("GET", "http://localhost:23119/api/", false);
      // Zotero 10's browser-request guard: the XHR carries a Mozilla/*
      // User-Agent, so it needs the explicit opt-in header.
      req.setRequestHeader("Zotero-Allowed-Request", "1");
      req.send(null);
      const sid = req.getResponseHeader("Zotero-Server-ID") || "";
      if (sid && sid.trim()) this._cachedServerID = sid.trim();
    } catch (e) {}
    return this._cachedServerID || "";
  },
};

function startup({ id, version, rootURI }) {
  QSActiveReader.init({ id, version, rootURI });
}

function shutdown() {
  try {
    if (QSActiveReader._notifierID != null && Zotero.Notifier) {
      Zotero.Notifier.unregisterObserver(QSActiveReader._notifierID);
    }
  } catch (e) {}
  QSActiveReader._unregisterEndpoint();
}

function install() {}
function uninstall() {}

function onMainWindowLoad({ window }) {
  // No per-window DOM needed: the endpoint is process-global by design.
  // The bridge supplies the verified compositor binding (see header).
}

function onMainWindowUnload({ window }) {
  // Nothing window-specific to tear down.
}
