#!/usr/bin/env python3
"""Opt-in Zen URL publisher for the desktop-activity collector.

The collector never invents Zen URLs: without this publisher it stores
``{"adapter": "zen-title", "title": ...}`` with ``url`` null. With it,
the focused Zen window binds ``{"adapter": "zen", "url": ..., "title": ...}``
when the explicit record matches the focused compositor window id AND pid
and is fresh (<=30 s). See ``services/agent-orchestrator/integrations/zen/README.md``.

Publisher contract (no bundled producer exists):

- ``push`` samples ``hyprctl activewindow -j`` in the SAME tick as the URL
  it attributes, requires the focused app to be Zen, and writes the record
  atomically (exclusive 0600 temp + rename). Call it from a Zen WebExtension
  native host, or manually: focus Zen, copy the address-bar URL, then
  ``zen_context.py push --url "$(wl-paste)"``. Re-push on every tab change
  and roughly every ~10 s while Zen stays focused; stale records (>30 s)
  honestly fall back to ``zen-title``.
- ``capture`` is the no-extension one-key flow: it verifies the focused
  Zen window, clears the clipboard, sends Ctrl+L / Ctrl+C to that exact
  window, reads the clipboard, and refuses to publish when the copy
  leaves the clipboard empty or non-``http(s)`` (fail closed against
  stale clipboard content). Bind it in the compositor to capture the
  current tab URL without leaving Zen. The previous clipboard content
  is lost by design (recoverable from cliphist); only the URL is
  published.
- Never point the collector at Zen session databases; unattributed global
  URLs are worse than the title fallback.

Conventions match the other helpers: JSON to stdout (single line, compact,
sorted keys), failures as one ``error: ...`` line on stderr with exit 1,
no tracebacks, list-form argv with ``shell=False``, bounded IO.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

import qscli

FILE_ENV = "QS_ZEN_CONTEXT_FILE"
MAX_FILE_BYTES = 16 * 1024
MAX_URL_CHARS = 2048
MAX_TITLE_CHARS = 1024
HYPR_TIMEOUT_S = 5.0
HYPR_OUTPUT_LIMIT = 256 * 1024
CLIPBOARD_LIMIT = 8 * 1024
FRESHNESS_MS = 30_000
URL_BAR_SETTLE_S = 0.35
COPY_SETTLE_S = 0.45
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{1,16}$")
_SHORTCUT_KEYS = ("l", "c", "Escape")
_SHORTCUT_MODS = ("CTRL", "SHIFT", "ALT", "SUPER")


class ZenContextError(ValueError):
    pass


def _error(message: str):
    raise ZenContextError(message)


def default_file() -> Path:
    raw = os.environ.get(FILE_ENV, "")
    if isinstance(raw, str) and raw.strip():
        return Path(raw.strip()).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME", "")
    if isinstance(xdg, str) and xdg.strip():
        return Path(xdg.strip()) / "quickshell" / "desktop-activity" / "zen-context.json"
    home = os.environ.get("HOME", "")
    if isinstance(home, str) and home.strip():
        return Path(home.strip()) / ".local" / "state" / "quickshell" / "desktop-activity" / "zen-context.json"
    _error("context file path cannot be resolved (set --file or QS_ZEN_CONTEXT_FILE)")


def _resolve_file(explicit: object) -> Path:
    if isinstance(explicit, str) and explicit.strip():
        return Path(explicit.strip()).expanduser()
    return default_file()


def _check_parent(path: Path) -> None:
    parent = path.parent
    try:
        st = parent.lstat()
    except FileNotFoundError:
        _error(f"context directory does not exist: {parent}")
    except OSError as exc:
        _error(f"context directory is not accessible: {exc}")
    if stat.S_ISLNK(st.st_mode):
        _error("context directory is a symlink")
    if not stat.S_ISDIR(st.st_mode):
        _error("context parent is not a directory")
    if stat.S_IMODE(st.st_mode) & 0o077 != 0:
        _error("context directory must be private (0700)")
    try:
        if st.st_uid != os.geteuid():
            _error("context directory is not owned by you")
    except AttributeError:
        pass


def _validate_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("push requires --url http(s)://...")
    text = value.strip()
    if len(text) > MAX_URL_CHARS or "\x00" in text:
        _error("url is too long or contains NUL")
    if not (text.startswith("http://") or text.startswith("https://")):
        _error("url must start with http:// or https://")
    if any(ord(c) < 32 for c in text):
        _error("url contains control characters")
    return text


def _validate_title(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if len(text) > MAX_TITLE_CHARS or "\x00" in text:
        _error("title is too long or contains NUL")
    text = "".join(" " if ord(c) < 32 else c for c in text)
    return " ".join(text.split())


def _focused_window() -> dict:
    try:
        completed = subprocess.run(
            ["hyprctl", "activewindow", "-j"],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=HYPR_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        _error("hyprctl not found on PATH (Hyprland session required)")
    except subprocess.TimeoutExpired:
        _error("hyprctl timed out")
    except OSError as exc:
        _error(f"hyprctl failed to start: {exc}")
    if completed.returncode != 0:
        _error("hyprctl activewindow failed")
    try:
        raw = completed.stdout.decode("utf-8", errors="replace")
    except Exception:
        _error("hyprctl output is unreadable")
    if len(raw.encode("utf-8")) > HYPR_OUTPUT_LIMIT:
        _error("hyprctl output is too large")
    try:
        data = json.loads(raw)
    except ValueError:
        _error("hyprctl output is not JSON")
    if not isinstance(data, dict):
        _error("hyprctl output is not an object")
    return data


def _atomic_write(path: Path, payload: dict) -> None:
    _check_parent(path)
    if path.is_symlink():
        _error("context file is a symlink")
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw = text.encode("utf-8")
    if len(raw) > MAX_FILE_BYTES:
        _error("context record is too large")
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        _error("publisher collision, retry")
    except OSError as exc:
        _error(f"cannot write context file: {exc}")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        os.replace(str(tmp), str(path))
    finally:
        try:
            if tmp.exists() and not tmp.is_symlink():
                tmp.unlink()
        except OSError:
            pass
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass


def _zen_window() -> dict:
    """Verified focused Zen window (address/pid/title), fail closed."""
    win = _focused_window()
    klass = str(win.get("class") or "")
    initial = str(win.get("initialClass") or "")
    if "zen" not in klass.lower() and "zen" not in initial.lower():
        _error(f"focused window is not Zen (class={klass or 'unknown'})")
    address = win.get("address")
    if not isinstance(address, str) or not _ADDRESS_RE.fullmatch(address.strip()):
        _error("focused window has no valid address")
    pid = win.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        _error("focused window has no client pid")
    focused_title = win.get("title")
    focused_title = focused_title.strip() if isinstance(focused_title, str) else ""
    return {"address": address.strip(), "pid": pid, "title": focused_title}


def _record_for(win: dict, url: str, title: str | None) -> dict:
    record = {
        "window_id": win["address"],
        "pid": win["pid"],
        "url": url,
        "updated_at_ms": int(time.time() * 1000),
    }
    chosen = _validate_title(title) or _validate_title(win.get("title"))
    if chosen:
        record["title"] = chosen
    return record


def _read_clipboard() -> str:
    try:
        completed = subprocess.run(
            ["wl-paste", "--no-newline"],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=HYPR_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        _error("wl-paste not found on PATH (Wayland clipboard required)")
    except subprocess.TimeoutExpired:
        _error("wl-paste timed out")
    except OSError as exc:
        _error(f"wl-paste failed to start: {exc}")
    if completed.returncode != 0:
        return ""
    if len(completed.stdout) > CLIPBOARD_LIMIT:
        _error("clipboard content is too large")
    return completed.stdout.decode("utf-8", errors="replace").strip()


def _clear_clipboard() -> None:
    try:
        completed = subprocess.run(
            ["wl-copy", "--clear"],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=HYPR_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        _error("wl-copy not found on PATH (Wayland clipboard required)")
    except subprocess.TimeoutExpired:
        _error("wl-copy timed out")
    except OSError as exc:
        _error(f"wl-copy failed to start: {exc}")
    if completed.returncode != 0:
        _error("wl-copy could not clear the clipboard")


def _send_shortcut(address: str, key: str, mods: str | None = None) -> None:
    """Inject one fixed shortcut into the exact verified Zen window."""
    if not isinstance(address, str) or not _ADDRESS_RE.fullmatch(address):
        _error("invalid window address")
    if key not in _SHORTCUT_KEYS:
        _error("invalid shortcut key")
    if mods is not None and mods not in _SHORTCUT_MODS:
        _error("invalid shortcut modifier")
    mods_text = mods if mods is not None else ""
    lua = ('hl.dsp.send_shortcut({ mods = "%s", key = "%s", '
           'window = "address:%s" })' % (mods_text, key, address))
    try:
        completed = subprocess.run(
            ["hyprctl", "dispatch", lua],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=HYPR_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        _error("hyprctl not found on PATH (Hyprland session required)")
    except subprocess.TimeoutExpired:
        _error("hyprctl shortcut dispatch timed out")
    except OSError as exc:
        _error(f"hyprctl shortcut dispatch failed to start: {exc}")
    if completed.returncode != 0:
        _error("hyprctl rejected the shortcut dispatch")
    try:
        if completed.stdout.decode("utf-8", errors="replace").strip() != "ok":
            _error("hyprctl rejected the shortcut dispatch")
    except Exception:
        _error("hyprctl shortcut dispatch output is unreadable")


def cmd_push(args) -> dict:
    url = _validate_url(args.url)
    path = _resolve_file(args.file)
    win = _zen_window()
    record = _record_for(win, url, args.title)
    _atomic_write(path, record)
    return {
        "ok": True,
        "file": str(path),
        "window_id": record["window_id"],
        "pid": record["pid"],
        "url": url,
        "title": record.get("title", ""),
    }


def cmd_capture(args) -> dict:
    """One-key capture: verify Zen focus, copy the URL bar, fail closed.

    The clipboard is cleared before the Ctrl+L/Ctrl+C injection, so a
    stale identical URL cannot masquerade as a fresh copy: the published
    URL must appear in the clipboard during this run, else the command
    errors out and leaves any existing record untouched.
    """
    path = _resolve_file(args.file)
    win = _zen_window()
    _clear_clipboard()
    _send_shortcut(win["address"], "l", "CTRL")
    time.sleep(URL_BAR_SETTLE_S)
    _send_shortcut(win["address"], "c", "CTRL")
    time.sleep(COPY_SETTLE_S)
    _send_shortcut(win["address"], "Escape")
    after = _read_clipboard()
    if not after:
        _error("clipboard empty after copy; retry or use push --url")
    url = _validate_url(after)
    record = _record_for(win, url, None)
    _atomic_write(path, record)
    return {
        "ok": True,
        "captured": True,
        "file": str(path),
        "window_id": record["window_id"],
        "pid": record["pid"],
        "url": url,
        "title": record.get("title", ""),
    }


def cmd_status(args) -> dict:
    path = _resolve_file(args.file)
    result: dict = {"file": str(path), "exists": False, "fresh": False, "matches_focus": False}
    try:
        if path.is_symlink():
            result["reason"] = "symlink refused"
            return result
        raw = path.read_bytes()
    except FileNotFoundError:
        result["reason"] = "no publisher record yet (title fallback active)"
        return result
    except OSError as exc:
        result["reason"] = f"unreadable: {exc}"
        return result
    if len(raw) > MAX_FILE_BYTES:
        result["reason"] = "record too large, ignored by collector"
        return result
    result["exists"] = True
    try:
        record = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        result["reason"] = "malformed JSON, ignored by collector"
        return result
    if not isinstance(record, dict):
        result["reason"] = "record is not an object"
        return result
    result["record"] = {
        "window_id": str(record.get("window_id") or ""),
        "pid": record.get("pid"),
        "url": str(record.get("url") or "")[:80],
        "has_title": bool(record.get("title")),
    }
    now_ms = int(time.time() * 1000)
    updated = record.get("updated_at_ms")
    if isinstance(updated, bool) or not isinstance(updated, int):
        try:
            mtime_ms = int(path.stat().st_mtime * 1000)
        except OSError:
            mtime_ms = 0
        fresh = (now_ms - mtime_ms) <= FRESHNESS_MS if mtime_ms else False
    else:
        fresh = 0 <= (now_ms - updated) <= FRESHNESS_MS
    result["fresh"] = bool(fresh)
    try:
        win = _focused_window()
        result["matches_focus"] = (
            isinstance(record.get("window_id"), str)
            and record["window_id"] == win.get("address")
            and record.get("pid") == win.get("pid")
        )
        result["focused"] = {
            "address": str(win.get("address") or ""),
            "pid": win.get("pid"),
            "class": str(win.get("class") or ""),
        }
    except ZenContextError as exc:
        result["reason"] = str(exc)
    if not result["fresh"]:
        result["reason"] = result.get("reason") or "stale (>30s), collector falls back to title"
    elif not result["matches_focus"]:
        result["reason"] = result.get("reason") or "record is for another window, collector falls back to title"
    else:
        result["reason"] = "bound: collector stores adapter zen with url"
    return result


def cmd_clear(args) -> dict:
    path = _resolve_file(args.file)
    try:
        if not path.exists() and not path.is_symlink():
            return {"ok": True, "file": str(path), "removed": False}
        if path.is_symlink():
            _error("context file is a symlink")
        path.unlink()
        return {"ok": True, "file": str(path), "removed": True}
    except OSError as exc:
        _error(f"cannot clear context file: {exc}")
    raise AssertionError("unreachable")


def _parse_args(argv):
    parser = qscli.SafeParser(description="Opt-in Zen URL publisher (same-tick hyprctl binding).")
    sub = parser.add_subparsers(dest="command", required=True,
                                parser_class=qscli.SafeParser)

    def _with_file(p):
        p.add_argument("--file", default=None,
                       help="context file override (default: $QS_ZEN_CONTEXT_FILE or state dir)")
        return p

    p = _with_file(sub.add_parser("push", help="sample focused Zen window + attribute a URL in the same tick"))
    p.add_argument("--url", required=True, help="http(s):// URL of the focused Zen tab")
    p.add_argument("--title", default=None, help="optional title override (default: focused window title)")
    _with_file(sub.add_parser("capture", help="verify Zen focus, copy the current tab URL, and publish it"))
    _with_file(sub.add_parser("status", help="inspect the publisher record without writing"))
    _with_file(sub.add_parser("clear", help="remove the publisher record (back to title fallback)"))
    return parser.parse_args(argv if argv is not None else sys.argv[1:])


def _dispatch(args):
    if args.command == "push":
        value = cmd_push(args)
    elif args.command == "capture":
        value = cmd_capture(args)
    elif args.command == "status":
        value = cmd_status(args)
    elif args.command == "clear":
        value = cmd_clear(args)
    else:
        _error("unknown command")
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


_BOUNDED_EXCEPTIONS = (ZenContextError, OSError, TypeError, ValueError,
                       UnicodeError)


def main(argv=None) -> int:
    return qscli.run_main(_parse_args, _dispatch, "zen context",
                          _BOUNDED_EXCEPTIONS, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
