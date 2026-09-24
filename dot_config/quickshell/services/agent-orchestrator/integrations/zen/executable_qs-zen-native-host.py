#!/usr/bin/env python3
"""Zen native-messaging host: marker-verified tab publisher (stdlib only).

The WebExtension (``extension/``) assigns each non-private window a random
per-window marker ``[qs-zen-<8 lowercase hex>] `` via
``browser.windows.update({titlePreface})`` and posts ``{type:"update"}``
messages over a persistent ``runtime.connectNative`` port to
``local.quickshell.zen_context`` (this host, stdio native messaging).

This host maps a message to the focused compositor window ONLY when, in the
SAME handling tick, the Hyprland active window (sampled BEFORE and AFTER
validation) has:

- class/initialClass containing "zen" (case-insensitive),
- title starting with the EXACT marker carried by the message,
- identical address/pid/title across both samples (focus-race guard).

Page-title heuristics are never used. Until the marker appears in the
compositor title (titlePreface propagates asynchronously) every update fails
closed to ``{}``. On success the host atomically writes the collector record
``{window_id, pid, url, title?, updated_at_ms}`` (``0600``, ``<=16 KiB``,
``QS_ZEN_CONTEXT_FILE``); on malformed/unsupported/focus-mismatch the host
atomically writes ``{}`` so the collector EVICTS to the honest ``zen-title``
fallback (a readable ``{}`` is a confirmed mismatch; deletion merely serves
bounded last-good). On stdin EOF (extension disconnect) the host clears to
``{}`` and exits 0.

Only local reads: ``hyprctl activewindow -j`` and the private file. No
shell interpolation (list-form argv), no clipboard, no key injection, no
network, no profile-database reads. Diagnostics go to stderr WITHOUT URLs,
titles, or markers; stdout carries ONLY native-messaging frames.

Native framing: ``<u32LE length><UTF-8 JSON>`` both directions. Inbound
frames are bounded (``FRAME_LIMIT``); a complete body that fails JSON
decoding invalidates (``{}``) but stays connected, while unrecoverable
framing errors (overlong length without draining, truncated header/body,
I/O errors) terminate fail-closed (``{}`` on exit) so attacker bytes are
never reparsed as headers. ``updatedAtMs`` from the extension
is validated (int ms, ``<=30 s`` old, ``<=5 s`` in the future) so delayed
records are rejected instead of published. The host is launched by Firefox
as ``host <manifest-path> <extension-id>``; both positionals are optional
(standalone ``--file``/``--hyprctl`` use still works) and an unexpected
extension id is refused. Broken stdout terminates (``{}``) instead of
spinning.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import struct
import subprocess
import sys
import time

HOST_NAME = "local.quickshell.zen_context"
EXPECTED_EXTENSION_ID = "qs-zen-context@quickshell.local"
FILE_ENV = "QS_ZEN_CONTEXT_FILE"
FILE_LIMIT = 16 * 1024
FRAME_LIMIT = 64 * 1024
MAX_URL_CHARS = 2048
MAX_TITLE_CHARS = 1024
FRESHNESS_MS = 30_000
FUTURE_SKEW_MS = 5_000
HYPR_TIMEOUT_S = 2.0
HYPR_OUTPUT_LIMIT = 256 * 1024
HEARTBEAT_MS = 10_000
MARKER_RE = re.compile(r"^\[qs-zen-[0-9a-f]{8}\] $")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{1,16}$")


def _is_expected_chrome_origin(value: object) -> bool:
    """Return True iff value is exactly the chrome-extension origin.

    Only ``chrome-extension://<expected-id>`` with an optional single
    trailing slash is accepted; no substring matching.
    """
    if not isinstance(value, str):
        return False
    if not value.startswith("chrome-extension://"):
        return False
    rest = value[len("chrome-extension://"):]
    return rest == EXPECTED_EXTENSION_ID or rest == EXPECTED_EXTENSION_ID + "/"


def _log(msg: str) -> None:
    try:
        print(msg, file=sys.stderr)
    except OSError:
        pass


def default_file() -> str:
    raw = os.environ.get(FILE_ENV, "")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    xdg = os.environ.get("XDG_STATE_HOME", "")
    if isinstance(xdg, str) and xdg.strip():
        return os.path.join(
            xdg.strip(), "quickshell", "desktop-activity", "zen-context.json"
        )
    home = os.environ.get("HOME", "")
    if isinstance(home, str) and home.strip():
        return os.path.join(
            home.strip(), ".local", "state", "quickshell",
            "desktop-activity", "zen-context.json",
        )
    raise ValueError("context file path cannot be resolved")


def _ensure_parent_private(path: str) -> bool:
    parent = os.path.dirname(os.path.abspath(path)) or "."
    try:
        st = os.lstat(parent)
    except FileNotFoundError:
        try:
            os.makedirs(parent, mode=0o700, exist_ok=True)
        except OSError:
            _log("host: context directory cannot be created")
            return False
        try:
            st = os.lstat(parent)
        except OSError:
            return False
    except OSError:
        _log("host: context directory is not accessible")
        return False
    if stat.S_ISLNK(st.st_mode):
        _log("host: context directory is a symlink")
        return False
    if not stat.S_ISDIR(st.st_mode):
        _log("host: context parent is not a directory")
        return False
    try:
        if st.st_uid != os.geteuid():
            _log("host: context directory is not owned by you")
            return False
    except AttributeError:
        pass
    if stat.S_IMODE(st.st_mode) & 0o077 != 0:
        _log("host: context directory must be private")
        return False
    return True


def _atomic_write(path: str, payload: dict) -> bool:
    if not _ensure_parent_private(path):
        return False
    try:
        if os.path.islink(path):
            _log("host: context file is a symlink")
            return False
    except OSError:
        return False
    try:
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        _log("host: record is not serializable")
        return False
    if len(raw) > FILE_LIMIT:
        _log("host: record is too large")
        return False
    parent = os.path.dirname(os.path.abspath(path)) or "."
    tmp = os.path.join(parent, f".zen-context.{os.getpid()}.tmp")
    try:
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
    except FileExistsError:
        _log("host: publisher collision, retry")
        return False
    except OSError:
        _log("host: cannot write context file")
        return False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.lexists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return True


def invalidate(path: str) -> bool:
    """Atomically write the eviction tombstone ``{}`` (fail closed)."""
    return _atomic_write(path, {})


def is_marker(value: object) -> bool:
    return isinstance(value, str) and MARKER_RE.match(value) is not None


def _validate_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_URL_CHARS or "\x00" in text:
        return None
    if not (text.startswith("http://") or text.startswith("https://")):
        return None
    if any(ord(c) < 32 for c in text):
        return None
    return text


def _validate_title(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or "\x00" in text:
        return None
    text = "".join(" " if ord(c) < 32 else c for c in text)
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) > MAX_TITLE_CHARS:
        text = text[:MAX_TITLE_CHARS]
    return text


def _validate_timestamp(value: object, now_ms: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0:
        return None
    if value - now_ms > FUTURE_SKEW_MS:
        return None
    if now_ms - value > FRESHNESS_MS:
        return None
    return value


def _is_zen_class(cls: object) -> bool:
    return "zen" in str(cls or "").lower()


def hypr_active(hyprctl: str = "hyprctl"):
    """Sample the focused Hyprland window, or None (fail closed).

    List-form argv only (no shell). Bounded time/output. Never raises.
    """
    try:
        completed = subprocess.run(
            [hyprctl, "activewindow", "-j"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=HYPR_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    raw = completed.stdout or b""
    if len(raw) > HYPR_OUTPUT_LIMIT:
        return None
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    address = data.get("address")
    if not isinstance(address, str) or not ADDRESS_RE.fullmatch(address.strip()):
        return None
    pid = data.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    klass = data.get("class") or data.get("initialClass") or ""
    title = data.get("title")
    title = title if isinstance(title, str) else ""
    return {
        "window_id": address.strip(),
        "pid": pid,
        "class": str(klass),
        "initial_class": str(data.get("initialClass") or ""),
        "title": title,
    }


def _focus_matches(marker: str, active) -> bool:
    if not isinstance(active, dict):
        return False
    if not (_is_zen_class(active.get("class")) or _is_zen_class(active.get("initial_class"))):
        return False
    title = active.get("title")
    if not isinstance(title, str) or not title.startswith(marker):
        return False
    return True


def handle_message(
    msg: object,
    path: str,
    get_active=None,
    now_ms: int | None = None,
) -> str:
    """Handle one native message. Returns 'wrote' | 'invalidated'.

    ``get_active`` injects the compositor sampler (tests use it for focus
    races); default is :func:`hypr_active`. Both samples must agree on
    address/pid/title or the message invalidates. Never logs payload
    contents (no URL/title/marker in diagnostics).
    """
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    sampler = get_active or (lambda: hypr_active("hyprctl"))
    # Allow tests to inject a parametrized hyprctl path via partials: when
    # get_active is None and HYPRCTL env override exists, honor it.
    before = sampler()
    if not isinstance(msg, dict):
        _log("host: malformed message ignored")
        invalidate(path)
        return "invalidated"
    mtype = msg.get("type")
    if mtype == "invalidate":
        invalidate(path)
        _log("host: invalidate applied")
        return "invalidated"
    if mtype != "update":
        _log("host: unknown message type ignored")
        invalidate(path)
        return "invalidated"
    if msg.get("incognito") is True:
        invalidate(path)
        _log("host: private context rejected")
        return "invalidated"
    marker = msg.get("marker")
    if not is_marker(marker):
        invalidate(path)
        _log("host: bad marker rejected")
        return "invalidated"
    url = _validate_url(msg.get("url"))
    if url is None:
        invalidate(path)
        _log("host: unsupported url rejected")
        return "invalidated"
    stamp = _validate_timestamp(msg.get("updatedAtMs"), now)
    if stamp is None:
        invalidate(path)
        _log("host: stale timestamp rejected")
        return "invalidated"
    title = _validate_title(msg.get("title"))
    if before is None or not _focus_matches(marker, before):
        invalidate(path)
        _log("host: focus mismatch rejected")
        return "invalidated"
    after = sampler()
    if after is None:
        invalidate(path)
        _log("host: focus resample failed")
        return "invalidated"
    if (
        after.get("window_id") != before.get("window_id")
        or after.get("pid") != before.get("pid")
        or after.get("title") != before.get("title")
    ):
        invalidate(path)
        _log("host: focus race rejected")
        return "invalidated"
    if not _focus_matches(marker, after):
        invalidate(path)
        _log("host: focus mismatch rejected")
        return "invalidated"
    record: dict = {
        "window_id": before["window_id"],
        "pid": before["pid"],
        "url": url,
        "updated_at_ms": stamp,
    }
    if title:
        record["title"] = title
    if _atomic_write(path, record):
        _log("host: update applied")
        return "wrote"
    _log("host: write failed")
    return "invalidated"


# --- native framing (read-exact, bounded, stdout is frames only) ---

def _read_exact(stream, n: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        try:
            chunk = stream.read(remaining)
        except (OSError, ValueError):
            return None
        if chunk is None:
            # Non-blocking empty read: treat as failure (callers use
            # blocking stdio; tests use BytesIO which never returns None).
            return None
        if chunk == b"":
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_frame_detailed(stream):
    """Read one native frame with fatal classification.

    Returns ``(msg, eof, malformed, fatal)``. ``eof=True`` means clean EOF
    at a frame boundary (host must clear). ``malformed=True, fatal=False``
    means a complete body failed JSON decoding: framing stays in sync so
    the host may invalidate but stay connected. ``malformed=True,
    fatal=True`` means an unrecoverable framing error (overlong/zero
    length, truncated header/body, I/O error): the stream is desynced or
    attacker-controlled, so the host must invalidate and terminate WITHOUT
    draining arbitrary sizes.
    """
    try:
        header = stream.read(4)
    except (OSError, ValueError):
        return None, False, True, True
    if header is None:
        return None, False, True, True
    if header == b"":
        return None, True, False, False
    if len(header) < 4:
        # Short read (e.g. split chunks): accumulate to a full header.
        # Only a zero-byte boundary read is clean EOF; a truncated header
        # is unrecoverable (fail closed, terminate).
        full = _read_exact(stream, 4 - len(header))
        if full is None:
            return None, False, True, True
        header = header + full
    (length,) = struct.unpack("<I", header)
    if length == 0 or length > FRAME_LIMIT:
        # Do NOT drain: length is attacker-controlled (up to 4 GiB).
        # Terminate fail-closed so payload bytes are never reparsed as
        # headers.
        return None, False, True, True
    body = _read_exact(stream, length)
    if body is None or len(body) != length:
        # Truncated body / EOF mid-frame: unrecoverable, terminate.
        return None, False, True, True
    try:
        msg = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError):
        # Complete body, bad JSON: framing still in sync, recoverable.
        return None, False, True, False
    return msg, False, False, False


def read_frame(stream):
    """Read one native frame. Returns (msg, eof, malformed).

    ``eof=True`` means clean EOF at a frame boundary (host must clear).
    ``malformed=True`` covers both recoverable JSON errors (stay
    connected) and unrecoverable framing errors (caller should terminate;
    see :func:`serve_forever` which uses ``_read_frame_detailed`` to tell
    them apart). Kept as a 3-tuple for backward compatibility.
    """
    msg, eof, malformed, _fatal = _read_frame_detailed(stream)
    return msg, eof, malformed


def write_frame(stream, obj: dict) -> bool:
    try:
        raw = json.dumps(
            obj, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError):
        return False
    if len(raw) > FRAME_LIMIT:
        return False
    try:
        stream.write(struct.pack("<I", len(raw)))
        stream.write(raw)
        stream.flush()
        return True
    except (OSError, ValueError):
        return False


def serve_forever(
    path: str,
    hyprctl: str = "hyprctl",
    stdin=None,
    stdout=None,
) -> int:
    """Serve native messages until EOF or unrecoverable framing error.

    Clears to ``{}`` on EOF and on every exit path (fail closed).
    Complete-body JSON errors invalidate, ack ``{"ok": False}``, and stay
    connected. Unrecoverable framing errors (oversized/truncated) and
    broken stdout invalidate and terminate (return 1) instead of
    re-parsing attacker bytes as headers or spinning on failed writes.
    """
    stdin = stdin if stdin is not None else sys.stdin.buffer
    stdout = stdout if stdout is not None else sys.stdout.buffer
    sampler = lambda: hypr_active(hyprctl)
    while True:
        msg, eof, malformed, fatal = _read_frame_detailed(stdin)
        if eof:
            invalidate(path)
            _log("host: eof cleared")
            return 0
        if malformed:
            invalidate(path)
            if fatal:
                _log("host: unrecoverable framing error, terminating")
                return 1
            _log("host: malformed frame rejected")
            if not write_frame(stdout, {"ok": False, "error": "malformed"}):
                invalidate(path)
                _log("host: stdout broken, terminating")
                return 1
            continue
        outcome = handle_message(msg, path, get_active=sampler)
        if not write_frame(stdout, {"ok": outcome == "wrote", "outcome": outcome}):
            invalidate(path)
            _log("host: stdout broken, terminating")
            return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Marker-verified Zen native host (see module docstring)."
    )
    parser.add_argument("--file", default=None)
    parser.add_argument("--hyprctl", default="hyprctl")
    # Firefox launches the host as: host <manifest-path> <extension-id>.
    # Both are optional positionals so standalone --file/--hyprctl use
    # (and unit tests) keep working. Chrome passes a single
    # chrome-extension:// origin instead; accept only its exact form.
    parser.add_argument("launch_args", nargs="*", help=(
        "Optional launch positionals from the browser: "
        "[manifest-path] [extension-id]."
    ))
    args = parser.parse_args(argv)
    launch = list(args.launch_args or [])
    if len(launch) > 2:
        _log("host: too many launch arguments")
        return 2
    extension_id: str | None = None
    if len(launch) == 2:
        # Firefox: manifest path + extension id. The manifest path is
        # caller-supplied and untrusted: ignore it, validate only the id
        # by exact equality (no substring matching).
        _, extension_id = launch
    elif len(launch) == 1:
        single = launch[0]
        if single == EXPECTED_EXTENSION_ID or _is_expected_chrome_origin(single):
            extension_id = single
        elif single.startswith("chrome-extension://"):
            # Chrome origin that is not exactly the expected origin:
            # authenticate as a caller id so the exact check below
            # refuses it (no substring matching).
            extension_id = single
        elif "/" in single or single.endswith(".json") or single.endswith("manifest"):
            # Bare manifest path (e.g. manual launch with one positional):
            # ignore, no caller to authenticate.
            extension_id = None
        else:
            # Unknown single positional: treat as a caller id and refuse
            # unless it matches (fail closed against unexpected callers).
            extension_id = single
    if extension_id is not None:
        # Accept only the exact id or the exact chrome-extension://
        # origin for it; refuse anything else, including wrappers that
        # merely contain the expected id as a substring.
        if extension_id != EXPECTED_EXTENSION_ID and not _is_expected_chrome_origin(extension_id):
            _log("host: unexpected caller rejected")
            return 2
    raw = args.file
    if isinstance(raw, str) and raw.strip():
        path = raw.strip()
    else:
        try:
            path = default_file()
        except ValueError as exc:
            _log(f"host: {exc}")
            return 2
    if not path.strip():
        _log("host: context file path is required")
        return 2
    return serve_forever(path, hyprctl=args.hyprctl)


if __name__ == "__main__":
    raise SystemExit(main())
