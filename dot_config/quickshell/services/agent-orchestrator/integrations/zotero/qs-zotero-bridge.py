#!/usr/bin/env python3
"""Verified Zotero active-reader bridge (Hyprland binding, stdlib only).

The Zotero plugin (`bootstrap.js`) exposes the Zotero-INTERNAL active reader
at ``GET http://127.0.0.1:23119/qs-active-reader`` but — by design — carries
no compositor binding: the in-process JS API exposes no native OS window
handle and the local library API (``/api/``) exposes library data only, so the
process-global active reader must never be treated as per-window correlation.

This bridge supplies the verified binding under a fail-closed single Zotero
window invariant with same-tick active window sampling: in the SAME sampling
tick it reads the focused Hyprland window (``hyprctl activewindow -j``:
opaque ``address`` + client ``pid`` + ``class``) AND the full client list
(``hyprctl clients -j``). The process-global active reader is mapped to the
focused window ONLY when exactly one client whose class contains "zotero"
(case-insensitive) exists AND the active window address matches that single
client address. The write is atomic (exclusive ``0600`` temp + rename) and
bounded (<=16 KiB).

Contract (mirrors the Zen explicit-file precedent):
- ``QS_ZOTERO_CONTEXT_FILE`` unset: collector attaches nothing (no title
  fallback for Zotero — titles cannot prove the active tab).
- Exactly one Zotero client + focused window is that client + plugin reports
  ``state:"open"`` with a valid identity: write ``{window_id, pid,
  updated_at_ms, server_id, library_type, library_id, item_key,
  attachment_key?, title?, collections[], ancestor_collections[], version?,
  zotero_uri?}``.
- Exactly one Zotero client + focused window is that client + plugin reports
  closed/unreadable/invalid (reader closed, no tabs, tab switch mid-flight,
  bad keys): write an explicit tombstone ``{window_id, pid, updated_at_ms,
  state:"closed"}`` so the collector EVICTS instead of serving the previous
  document from last-good.
- Zero or 2+ Zotero clients, clients query failure/parse error, or
  active-address mismatch: NEVER write an open document record. If the active
  window is itself a Zotero client, write the closed tombstone for it
  (immediate eviction, preferred over letting a previous record go stale);
  otherwise leave the file untouched ("skip", exactly like the non-Zotero
  focus path).
- Focused window is NOT Zotero: leave the file untouched (it goes stale and
  the collector treats it as a confirmed mismatch on the next Zotero focus).
- Plugin unreachable / Hyprland active query fails: leave the file untouched
  (transient; the collector serves bounded last-good for the same focus).

Usage (manual; nothing auto-installs or auto-starts)::

    export QS_ZOTERO_CONTEXT_FILE="$XDG_RUNTIME_DIR/qs-zotero-context.json"
    python3 services/agent-orchestrator/integrations/zotero/qs-zotero-bridge.py --once
    python3 services/agent-orchestrator/integrations/zotero/qs-zotero-bridge.py --loop --interval 5

Options: ``--once`` (single tick, exit 0/1), ``--loop`` (refresh cadence,
freshness target <=10 s so the 30 s collector window always holds),
``--interval SEC`` (default 5), ``--endpoint URL``, ``--file PATH``,
``--hyprctl BIN``. Only local reads: ``hyprctl``, the plugin endpoint, and
the private file. No SQLite, no cloud, no content extraction.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

ENDPOINT_DEFAULT = "http://127.0.0.1:23119/qs-active-reader"
FILE_LIMIT = 16 * 1024
HTTP_TIMEOUT = 2.0
HYPR_TIMEOUT = 2.0
KEY_RE_LETTERS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


def _is_key(s) -> bool:
    return isinstance(s, str) and len(s) == 8 and all(c in KEY_RE_LETTERS for c in s)


def _is_libid(s) -> bool:
    return isinstance(s, str) and bool(s) and len(s) <= 32 and s.isdigit()


def _hypr_active(hyprctl: str):
    try:
        out = subprocess.run(
            [hyprctl, "activewindow", "-j"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=HYPR_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout.decode("utf-8", "replace"))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    addr = data.get("address")
    pid_raw = data.get("pid")
    cls = data.get("class") or data.get("initialClass") or ""
    if not isinstance(addr, str) or not addr:
        return None
    try:
        pid = int(pid_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    return {"window_id": addr, "pid": pid, "class": str(cls)}


def _hypr_clients(hyprctl: str):
    """Return the raw ``hyprctl clients -j`` list, or None on failure.

    Any transport error, non-zero exit, undecodable JSON, or unexpected shape
    (not a list) yields None so the caller fails closed (never an open
    record). Individual entries are validated by `_single_zotero_address`.
    """
    try:
        out = subprocess.run(
            [hyprctl, "clients", "-j"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=HYPR_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout.decode("utf-8", "replace"))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(data, list):
        return None
    return data


def _single_zotero_address(clients) -> str | None:
    """Return the address of the single Zotero client, else None.

    Counts entries whose ``class``/``initialClass`` contains "zotero"
    (case-insensitive). Returns the address string only when exactly one such
    client exists and its address is a non-empty string; otherwise returns
    None (zero, 2+, malformed entry, or clients query failure). Callers treat
    None as "invariant not satisfied: never write an open record".
    """
    if not isinstance(clients, list):
        return None
    found: list[str] = []
    for entry in clients:
        if not isinstance(entry, dict):
            return None
        cls = entry.get("class") or entry.get("initialClass") or ""
        if "zotero" not in str(cls).lower():
            continue
        addr = entry.get("address")
        if not isinstance(addr, str) or not addr:
            return None
        found.append(addr)
    if len(found) != 1:
        return None
    return found[0]


def _is_zotero_class(cls) -> bool:
    return "zotero" in str(cls or "").lower()


def _fetch_plugin(endpoint: str):
    try:
        with urllib.request.urlopen(endpoint, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read(FILE_LIMIT + 1)
    except (OSError, ValueError):
        return None
    if len(raw) > FILE_LIMIT:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def _atomic_write(path: str, payload: dict) -> bool:
    try:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return False
    if len(raw) > FILE_LIMIT:
        return False
    parent = os.path.dirname(os.path.abspath(path)) or "."
    try:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    except OSError:
        return False
    try:
        fd, tmp = tempfile.mkstemp(prefix=".qs-zotero-", suffix=".tmp", dir=parent)
    except OSError:
        return False
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
        try:
            dfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            return True
        try:
            os.fsync(dfd)
        except OSError:
            pass
        finally:
            try:
                os.close(dfd)
            except OSError:
                pass
        return True
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def tick(endpoint: str, path: str, hyprctl: str, now_ms: int | None = None) -> str:
    """One verified tick. Returns 'wrote' | 'tombstone' | 'skip' | 'error'."""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    # Same-tick sampling: focused window AND full client list. The
    # process-global plugin reader may only be mapped when the single Zotero
    # window invariant holds (exactly one Zotero client whose address matches
    # the active window). Anything else fails closed: never an open record.
    active = _hypr_active(hyprctl)
    clients = _hypr_clients(hyprctl)
    single_addr = _single_zotero_address(clients)

    def _closed_for_active():
        assert active is not None
        base = {
            "window_id": active["window_id"],
            "pid": active["pid"],
            "updated_at_ms": now_ms,
            "state": "closed",
        }
        return "tombstone" if _atomic_write(path, base) else "error"

    if single_addr is None:
        # Zero / 2+ Zotero clients, or clients query failed/parsed
        # unexpectedly: no open record. Evict immediately when the focused
        # window is itself Zotero; otherwise leave the file untouched.
        if active is not None and _is_zotero_class(active.get("class", "")):
            return _closed_for_active()
        return "skip"
    if active is None:
        return "skip"
    if not _is_zotero_class(active.get("class", "")):
        return "skip"
    # Exactly one Zotero client exists and focus is Zotero: require the active
    # address to be that client (active pid/window_id logic stays — the record
    # still carries the active window's address/pid).
    try:
        match = str(active.get("window_id", "")).lower() == str(single_addr).lower()
    except (AttributeError, TypeError, ValueError):
        match = False
    if not match:
        # Race/mismatch (e.g. focus moved between the two hyprctl calls):
        # active is Zotero so evict rather than risk misattribution.
        return _closed_for_active()
    snap = _fetch_plugin(endpoint)
    if not isinstance(snap, dict):
        return "skip"
    base = {
        "window_id": active["window_id"],
        "pid": active["pid"],
        "updated_at_ms": now_ms,
    }
    if snap.get("state") != "open":
        base["state"] = "closed"
        return "tombstone" if _atomic_write(path, base) else "error"
    server = snap.get("server_id", "")
    ltype = snap.get("library_type")
    libid = snap.get("library_id")
    item = snap.get("item_key")
    if not isinstance(server, str) or not server.strip() or len(server.strip()) > 128:
        base["state"] = "closed"
        return "tombstone" if _atomic_write(path, base) else "error"
    if ltype not in ("user", "group") or not _is_libid(libid):
        base["state"] = "closed"
        return "tombstone" if _atomic_write(path, base) else "error"
    if ltype == "group" and str(libid) == "0":
        base["state"] = "closed"
        return "tombstone" if _atomic_write(path, base) else "error"
    if not _is_key(item):
        base["state"] = "closed"
        return "tombstone" if _atomic_write(path, base) else "error"
    att = snap.get("attachment_key")
    if att in (None, ""):
        att = None
    elif not _is_key(att):
        base["state"] = "closed"
        return "tombstone" if _atomic_write(path, base) else "error"
    cols = snap.get("collections", [])
    ancs = snap.get("ancestor_collections", snap.get("ancestors", []))
    if not isinstance(cols, list) or not isinstance(ancs, list):
        base["state"] = "closed"
        return "tombstone" if _atomic_write(path, base) else "error"
    for lst in (cols, ancs):
        if len(lst) > 256 or any(not _is_key(x) for x in lst):
            base["state"] = "closed"
            return "tombstone" if _atomic_write(path, base) else "error"
    payload = dict(base)
    payload.update({
        "server_id": server.strip(),
        "library_type": ltype,
        "library_id": str(libid),
        "item_key": item,
        "collections": list(dict.fromkeys(cols)),
        "ancestor_collections": list(dict.fromkeys(ancs)),
    })
    if att is not None:
        payload["attachment_key"] = att
    title = snap.get("title")
    if isinstance(title, str) and title.strip():
        payload["title"] = title.strip()[:1024]
    ver = snap.get("version")
    if isinstance(ver, int) and ver >= 0:
        payload["version"] = ver
    uri = snap.get("zotero_uri", snap.get("uri"))
    if isinstance(uri, str) and uri.strip().startswith(("zotero://select/", "zotero://open-pdf/")):
        payload["zotero_uri"] = uri.strip()[:2048]
    return "wrote" if _atomic_write(path, payload) else "error"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verified Zotero/Hyprland bridge (see module docstring).")
    ap.add_argument("--endpoint", default=os.environ.get("QS_ZOTERO_ENDPOINT", ENDPOINT_DEFAULT))
    ap.add_argument("--file", default=os.environ.get("QS_ZOTERO_CONTEXT_FILE", ""))
    ap.add_argument("--hyprctl", default="hyprctl")
    ap.add_argument("--interval", type=float, default=5.0)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    args = ap.parse_args(argv)
    if not args.file.strip():
        print("error: QS_ZOTERO_CONTEXT_FILE/--file is required", file=sys.stderr)
        return 2
    if args.loop:
        interval = min(max(args.interval, 1.0), 25.0)
        while True:
            tick(args.endpoint, args.file, args.hyprctl)
            time.sleep(interval)
        return 0
    outcome = tick(args.endpoint, args.file, args.hyprctl)
    if outcome == "error":
        print("error: bridge write failed", file=sys.stderr)
        return 1
    print(outcome)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
