"""Hermetic palette-orchestrator bridge integration tests.

No real pi / model / notes are touched. A temporary fake ``pi`` on PATH is
exercised through the REAL Rust bridge ``--mode palette`` direct invocation
(``pi --mode rpc --approve [--session-dir PI_CODING_AGENT_SESSION_DIR]
[--session cached]``) with a temporary pool/state/graph. The REAL Rust
binary is driven over stdin JSONL v1
(start/prompt{message,images}/newSession/switchSession/rename/
chooseModel{item}/compact/requestMessages/respond{requestId,fields}/
requestStats/abort/shutdown) and state/events updates are asserted.

Fixtures (fake-pi source, bridge harness, build helper) are reused from
``tests/test_scoped_orchestrator.py`` via import when practical; the palette
fake extends the scoped fake with a think/tool hidden-event prompt while
keeping directory/session semantics hermetic (temp PI session dir).

Run focused with cache disabled:
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_palette_orchestrator -v
"""
import sys as _sys
_sys.dont_write_bytecode = True

import json
import os
import queue
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
MANIFEST = REPO_ROOT / "services" / "agent-orchestrator" / "Cargo.toml"
BINARY = REPO_ROOT / "services" / "agent-orchestrator" / "target" / "debug" / "qs-agent-orchestrator"
REAL_DEFAULT_GRAPH = "/home/franzs/Nextcloud/Documents/Notes"

CARGO = shutil.which("cargo")
BUILT = False
BUILD_ERROR = ""

# -- reuse scoped fixtures when practical (do NOT modify that file) --------
_SCOPED = None
_SCOPED_IMPORT_ERROR = ""
for _mod in ("test_scoped_orchestrator", "tests.test_scoped_orchestrator"):
    try:
        __import__(_mod)
        _SCOPED = _sys.modules[_mod]
        break
    except Exception as exc:  # pragma: no cover - fallback path
        _SCOPED_IMPORT_ERROR = str(exc)
        continue


def ensure_built():
    global BUILT, BUILD_ERROR
    if BUILT:
        return True
    if _SCOPED is not None and hasattr(_SCOPED, "ensure_built"):
        try:
            ok = _SCOPED.ensure_built()
        except Exception as exc:
            BUILD_ERROR = str(exc)
            return False
        if ok:
            BUILT = True
            return True
        BUILD_ERROR = getattr(_SCOPED, "BUILD_ERROR", "")
        return False
    if CARGO is None:
        BUILD_ERROR = "cargo not found on PATH"
        return False
    try:
        completed = subprocess.run(
            [CARGO, "build", "--locked", "--manifest-path", str(MANIFEST)],
            capture_output=True, text=True, timeout=300,
        )
    except Exception as exc:  # pragma: no cover - environment failure
        BUILD_ERROR = f"cargo build failed to launch: {exc}"
        return False
    if completed.returncode != 0:
        BUILD_ERROR = (completed.stderr or completed.stdout)[-4000:]
        return False
    BUILT = True
    return True


_SCOPED_FAKE = getattr(_SCOPED, "FAKE_PI_SOURCE", "") if _SCOPED is not None else ""

if _SCOPED_FAKE:
    # Extend the scoped fake with a palette hidden-event prompt. The scoped
    # source already handles --session-dir/--session/--name, argv/env logs,
    # block_*/fail_*/delay_*/die_on_*, missing_extension, approve-me/expire-me,
    # messages_payload.json, and owned-descendant helpers. The injection below
    # only adds a "think-tool-me" branch that emits hidden thinking/tool
    # frames before the normal text stream; everything else stays verbatim.
    _ANCHOR = '            else:\n                emit({"type": "message_update",'
    _INJECT = (
        '            elif "think-tool-me" in msg:\n'
        '                emit({"type": "message_update",\n'
        '                      "assistantMessageEvent": {"type": "thinking_delta", "delta": "thinking-secret-hidden"}})\n'
        '                emit({"type": "message_update",\n'
        '                      "assistantMessageEvent": {"type": "tool_delta", "delta": "tool-secret-hidden"}})\n'
        '                emit({"type": "message_end",\n'
        '                      "message": {"role": "tool", "content": "tool-secret-hidden"}})\n'
        '                emit({"type": "message_update",\n'
        '                      "assistantMessageEvent": {"type": "text_delta", "delta": "hello "}})\n'
        '                emit({"type": "message_end",\n'
        '                      "message": {"role": "assistant", "content": "hello world"}})\n'
        '                emit({"type": "agent_end"})\n'
        '                emit({"type": "agent_settled"})\n'
        '            else:\n'
        '                emit({"type": "message_update",'
    )
    if _ANCHOR in _SCOPED_FAKE:
        FAKE_PI_SOURCE = _SCOPED_FAKE.replace(_ANCHOR, _INJECT)
    else:  # pragma: no cover - scoped source drifted; reuse verbatim
        FAKE_PI_SOURCE = _SCOPED_FAKE
else:  # pragma: no cover - standalone fallback when scoped import unavailable
    FAKE_PI_SOURCE = r'''#!/usr/bin/env python3
import json, os, sys, time, uuid, subprocess, threading
CTL = os.environ.get("PALETTE_FAKE_CTL", "") or os.environ.get("SCOPED_FAKE_CTL", "")
LOG = os.environ.get("PALETTE_FAKE_LOG", "") or os.environ.get("SCOPED_FAKE_LOG", "")
ARGV_LOG = os.environ.get("PALETTE_FAKE_ARGV_LOG", "") or os.environ.get("SCOPED_FAKE_ARGV_LOG", "")
ENV_LOG = os.environ.get("PALETTE_FAKE_ENV_LOG", "") or os.environ.get("SCOPED_FAKE_ENV_LOG", "")
OUT_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
def log_json(path, obj):
    if not path:
        return
    with open(path, "a", encoding="utf-8") as h:
        h.write(json.dumps(obj) + "\n")
def ctl_exists(name):
    return bool(CTL) and os.path.exists(os.path.join(CTL, name))
def ctl_text(name):
    try:
        with open(os.path.join(CTL, name), encoding="utf-8") as h:
            return h.read()
    except OSError:
        return ""
def wait_while_blocked(name, timeout=15.0):
    start = time.time()
    while ctl_exists(name):
        if time.time() - start > timeout:
            break
        time.sleep(0.05)
def valid_header(path):
    try:
        with open(path, encoding="utf-8") as h:
            first = h.readline()
        obj = json.loads(first)
        return (obj.get("type") == "session" and isinstance(obj.get("id"), str)
                and bool(obj.get("id")) and isinstance(obj.get("version"), int))
    except Exception:
        return False
def make_header(path, sid):
    with open(path, "w", encoding="utf-8") as h:
        h.write(json.dumps({"type": "session", "version": 3, "id": sid,
                            "timestamp": "2026-01-01T00:00:00.000Z",
                            "cwd": "/tmp/fake-graph"}) + "\n")
def parse_argv(argv):
    out = {"session_dir": "", "session": "", "name": ""}
    i = 0
    while i < len(argv):
        if argv[i] == "--session-dir" and i + 1 < len(argv):
            out["session_dir"] = argv[i + 1]; i += 2
        elif argv[i] == "--session" and i + 1 < len(argv):
            out["session"] = argv[i + 1]; i += 2
        elif argv[i] == "--name" and i + 1 < len(argv):
            out["name"] = argv[i + 1]; i += 2
        else:
            i += 1
    return out
def main():
    argv = sys.argv[1:]
    parsed = parse_argv(argv)
    session_dir = parsed["session_dir"]
    try:
        if session_dir:
            os.makedirs(session_dir, exist_ok=True)
    except Exception:
        pass
    current = parsed["session"]
    if current:
        cur_path = current
    else:
        cur_path = os.path.join(session_dir or "/tmp", f"fresh-{os.getpid()}.jsonl") if session_dir else "/tmp/fake-fresh.jsonl"
        sid = str(uuid.uuid4())
        try:
            if not os.path.exists(cur_path):
                make_header(cur_path, sid)
            elif not valid_header(cur_path):
                make_header(cur_path, sid)
        except Exception:
            pass
        current = cur_path
    try:
        with open(current, encoding="utf-8") as h:
            sid = json.loads(h.readline()).get("id", str(uuid.uuid4()))
    except Exception:
        sid = str(uuid.uuid4())
    session_id = sid
    log_json(ARGV_LOG, {"pid": os.getpid(), "argv": argv})
    log_json(ENV_LOG, {"pid": os.getpid(),
                       "PI_CODING_AGENT_SESSION_DIR": os.environ.get("PI_CODING_AGENT_SESSION_DIR"),
                       "QS_PROJECT_SESSION_SCOPE": os.environ.get("QS_PROJECT_SESSION_SCOPE"),
                       "QS_JOURNAL_SESSION_SCOPE": os.environ.get("QS_JOURNAL_SESSION_SCOPE"),
                       "QS_PROJECT_PATH": os.environ.get("QS_PROJECT_PATH"),
                       "QS_JOURNAL_MODE": os.environ.get("QS_JOURNAL_MODE"),
                       "LOGSEQ_GRAPH": os.environ.get("LOGSEQ_GRAPH")})
    if os.environ.get("FAKE_IGNORE_TERM") == "1":
        try:
            import signal as _sig
            _sig.signal(_sig.SIGTERM, _sig.SIG_IGN)
        except Exception:
            pass
    if os.environ.get("FAKE_SPAWN_SLEEP") == "1":
        try:
            child = subprocess.Popen(["sleep", "30"])
            if CTL:
                with open(os.path.join(CTL, "sleep_pid"), "w") as h:
                    h.write(str(child.pid))
                with open(os.path.join(CTL, "owned_descendants.log"), "a") as h:
                    h.write(str(child.pid) + "\n")
        except Exception:
            pass
    if os.environ.get("FAKE_RESISTANT") == "1":
        try:
            subprocess.Popen(["sh", "-c", 'trap "" TERM; sleep 30 &'])
            if CTL:
                with open(os.path.join(CTL, "resistant_armed"), "w") as h:
                    h.write(str(os.getpid()))
        except Exception:
            pass
    stdin = sys.stdin
    shared = {"current": current, "name": "", "sid": session_id, "counter": 0, "session_dir": session_dir, "last_new": "", "last_picker": ""}
    def emit(obj):
        with OUT_LOCK:
            sys.stdout.write(json.dumps(obj) + "\n")
            sys.stdout.flush()
    def respond(req_id, req_type, success, data=None, error=None):
        payload = {"type": "response", "id": req_id, "command": req_type, "success": success}
        if success:
            payload["data"] = data if data is not None else {}
        else:
            payload["error"] = error if error is not None else "failed"
        emit(payload)
    def do_get_state(rid):
        with STATE_LOCK:
            cur = shared["current"]; nm = shared["name"]; sid2 = shared["sid"]
        respond(rid, "get_state", True, {"sessionFile": cur, "sessionName": nm,
                                         "sessionId": sid2, "model": None,
                                         "isStreaming": False, "isCompacting": False})
    def do_get_messages(rid, captured):
        msgs = captured if captured is not None else []
        respond(rid, "get_messages", True, {"messages": msgs})
    def do_new_session(rid):
        with STATE_LOCK:
            shared["counter"] += 1
            sdir = shared["session_dir"]; cnt = shared["counter"]
        fresh = os.path.join(sdir, f"new-{os.getpid()}-{cnt}.jsonl") if sdir else f"/tmp/fake-new-{cnt}.jsonl"
        try:
            make_header(fresh, str(uuid.uuid4()))
        except Exception:
            pass
        with STATE_LOCK:
            shared["current"] = fresh
            try:
                with open(fresh, encoding="utf-8") as h:
                    shared["sid"] = json.loads(h.readline()).get("id", shared["sid"])
            except Exception:
                pass
        respond(rid, "new_session", True, {"cancelled": False})
    def async_after(block_name, fn):
        def run():
            wait_while_blocked(block_name)
            fn()
        threading.Thread(target=run, daemon=True).start()
        return True
    def watcher():
        while True:
            try:
                if CTL and os.path.exists(os.path.join(CTL, "emit_settled_once")):
                    try:
                        os.unlink(os.path.join(CTL, "emit_settled_once"))
                    except OSError:
                        pass
                    emit({"type": "agent_settled"})
            except Exception:
                pass
            time.sleep(0.05)
    threading.Thread(target=watcher, daemon=True).start()
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        log_json(LOG, obj)
        rtype = obj.get("type", "")
        rid = obj.get("id", "")
        if not rid:
            continue
        captured_messages = None
        if rtype == "get_messages":
            payload_path = os.path.join(CTL, "messages_payload.json") if CTL else ""
            try:
                if payload_path and os.path.exists(payload_path):
                    with open(payload_path, encoding="utf-8") as h:
                        captured_messages = json.load(h)
            except Exception:
                captured_messages = None
        if rtype == "new_session":
            with STATE_LOCK:
                shared["last_new"] = rid
        if rtype == "prompt" and obj.get("message") == "/desktop-sessions":
            with STATE_LOCK:
                shared["last_picker"] = rid
        if rtype == "new_session" and ctl_exists("block_new_session"):
            async_after("block_new_session", lambda rid=rid: do_new_session(rid))
            continue
        if rtype == "get_messages" and ctl_exists("block_get_messages"):
            async_after("block_get_messages",
                        lambda rid=rid, captured=captured_messages: do_get_messages(rid, captured))
            continue
        if ctl_exists(f"die_on_{rtype}"):
            os._exit(1)
        if ctl_exists(f"fail_{rtype}"):
            respond(rid, rtype, False, error=ctl_text(f"fail_{rtype}").strip() or f"{rtype} failed")
            continue
        if rtype == "get_state":
            do_get_state(rid)
        elif rtype == "get_commands":
            if ctl_exists("missing_extension"):
                respond(rid, rtype, True, {"commands": []})
            else:
                respond(rid, rtype, True, {"commands": [{"name": "desktop-sessions", "description": "sessions"}]})
        elif rtype == "get_available_models":
            respond(rid, rtype, True, {"models": []})
        elif rtype == "get_session_stats":
            respond(rid, rtype, True, {"tokens": {"total": 0}})
        elif rtype == "get_last_assistant_text":
            respond(rid, rtype, True, {"text": ""})
        elif rtype == "get_messages":
            msgs = captured_messages if captured_messages is not None else []
            respond(rid, rtype, True, {"messages": msgs})
        elif rtype == "prompt":
            msg = obj.get("message", "")
            if "__die__" in msg:
                sys.stdout.flush()
                os._exit(1)
            respond(rid, rtype, True, {})
            if msg == "/desktop-sessions":
                emit({"type": "extension_ui_request", "id": "sel-1", "method": "select",
                      "title": "sessions", "options": ["a", "b"], "timeout": 60000})
            elif "approve-me" in msg:
                emit({"type": "extension_ui_request", "id": "appr-1", "method": "confirm",
                      "message": "approve?", "timeout": 60000})
            elif "expire-me" in msg:
                emit({"type": "extension_ui_request", "id": "exp-1", "method": "confirm",
                      "message": "expire?", "timeout": 1200})
            elif "think-tool-me" in msg:
                emit({"type": "message_update",
                      "assistantMessageEvent": {"type": "thinking_delta", "delta": "thinking-secret-hidden"}})
                emit({"type": "message_update",
                      "assistantMessageEvent": {"type": "tool_delta", "delta": "tool-secret-hidden"}})
                emit({"type": "message_end",
                      "message": {"role": "tool", "content": "tool-secret-hidden"}})
                emit({"type": "message_update",
                      "assistantMessageEvent": {"type": "text_delta", "delta": "hello "}})
                emit({"type": "message_end",
                      "message": {"role": "assistant", "content": "hello world"}})
                emit({"type": "agent_end"})
                emit({"type": "agent_settled"})
            else:
                emit({"type": "message_update",
                      "assistantMessageEvent": {"type": "text_delta", "delta": "hello "}})
                emit({"type": "message_end",
                      "message": {"role": "assistant", "content": "hello world"}})
                emit({"type": "agent_end"})
                emit({"type": "agent_settled"})
            if "approve-me" in msg or "expire-me" in msg:
                emit({"type": "agent_end"})
                emit({"type": "agent_settled"})
        elif rtype == "new_session":
            do_new_session(rid)
        elif rtype == "set_session_name":
            with STATE_LOCK:
                shared["name"] = obj.get("name", "")
            respond(rid, rtype, True, {})
        elif rtype in ("set_model", "compact", "clear_queue", "abort", "get_state"):
            respond(rid, rtype, True, {})
        elif rtype == "extension_ui_response":
            pass
        else:
            respond(rid, rtype, True, {})
if __name__ == "__main__":
    main()
'''


class BridgeHarness:
    """Spawn the real bridge with hermetic env and read JSONL updates."""

    def __init__(self, mode, project_page, env, root=REPO_ROOT):
        self.mode = mode
        self.env = env
        self._counter = 0
        self._queue = queue.Queue()
        self._stop = threading.Event()
        args = [str(BINARY), "--root", str(root), "--mode", mode]
        if mode == "project":
            args += ["--project", project_page]
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1,
        )
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        self.updates = []

    def _pump(self):
        try:
            stdout = self.proc.stdout
            assert stdout is not None
            for raw in stdout:
                if self._stop.is_set():
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    self._queue.put(json.loads(line))
                except json.JSONDecodeError:
                    self._queue.put({"_raw": line})
        except Exception:
            pass

    def send(self, op, args=None, ident=None):
        self._counter += 1
        ident = ident or f"ui-{self._counter}-{uuid.uuid4().hex[:6]}"
        payload = {"version": 1, "id": ident, "op": op, "args": args or {}}
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        return ident

    def send_raw(self, text):
        assert self.proc.stdin is not None
        self.proc.stdin.write(text)
        self.proc.stdin.flush()

    def recv(self, timeout=10.0):
        try:
            update = self._queue.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("timed out waiting for bridge update")
        self.updates.append(update)
        return update

    def wait_ack(self, ident, timeout=10.0):
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"no ack for {ident}")
            update = self.recv(timeout=remaining)
            if update.get("ack") == ident:
                return update

    def wait_state(self, predicate, timeout=15.0):
        deadline = time.time() + timeout
        last = None
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"state predicate not met; last={json.dumps(last)[:2000] if last else None}")
            update = self.recv(timeout=remaining)
            last = update
            state = update.get("state", {})
            try:
                if predicate(state, update):
                    return update
            except Exception:
                continue

    def wait_event(self, name, timeout=15.0):
        def pred(state, update):
            for event in update.get("events", []) or []:
                if event.get("name") == name:
                    return True
            return False
        return self.wait_state(pred, timeout=timeout)

    def drain(self):
        while True:
            try:
                self.updates.append(self._queue.get_nowait())
            except queue.Empty:
                break

    def close_stdin(self):
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass

    def wait_exit(self, timeout=8.0):
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TimeoutError("bridge did not exit in time")

    def kill(self):
        try:
            self.proc.kill()
        except Exception:
            pass
        self._stop.set()

    def shutdown(self):
        self._stop.set()
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            if self.proc.stdout and not self.proc.stdout.closed:
                self.proc.stdout.close()
        except Exception:
            pass


if _SCOPED is not None and hasattr(_SCOPED, "BridgeHarness"):
    BridgeHarness = _SCOPED.BridgeHarness  # type: ignore[no-redef]


def _skip_unless_built(testcase):
    if CARGO is None and _SCOPED is None:
        testcase.skipTest("cargo unavailable; cannot build bridge")
    if _SCOPED is not None and hasattr(_SCOPED, "_skip_unless_built"):
        return _SCOPED._skip_unless_built(testcase)
    if CARGO is None:
        testcase.skipTest(f"cargo unavailable; cannot build bridge ({BUILD_ERROR})")
    if not ensure_built():
        testcase.fail(f"cargo build failed; cannot exercise bridge ({BUILD_ERROR})")
    if not BINARY.exists():
        testcase.fail("bridge binary missing after build")


class PaletteOrchestratorHarness(unittest.TestCase):
    def setUp(self):
        _skip_unless_built(self)
        # Hermetic temp layout: graph + state + palette pool + bin + ctl + logs.
        # No real pi / model / notes are touched; PI session dir is temp.
        self.tmp = tempfile.TemporaryDirectory(prefix="palette-orch-")
        self.root = Path(self.tmp.name)
        self.graph = self.root / "graph"
        (self.graph / "pages").mkdir(parents=True)
        (self.graph / "journals").mkdir(parents=True)
        self.state_home = self.root / "state"
        self.state_home.mkdir()
        self.pool = self.root / "pool"
        self.pool.mkdir()
        self.ctl = self.root / "ctl"
        self.ctl.mkdir()
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        self.fake_log = self.root / "fake-inputs.jsonl"
        self.fake_argv = self.root / "fake-argv.jsonl"
        self.fake_env = self.root / "fake-env.jsonl"
        fake = self.bindir / "pi"
        fake.write_text(FAKE_PI_SOURCE, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.assertNotEqual(str(self.graph.resolve()), REAL_DEFAULT_GRAPH)
        self.base_env = dict(
            os.environ,
            PATH=f"{self.bindir}{os.pathsep}{os.environ.get('PATH', '')}",
            LOGSEQ_GRAPH=str(self.graph),
            XDG_STATE_HOME=str(self.state_home),
            PI_CODING_AGENT_SESSION_DIR=str(self.pool),
            PI_OFFLINE="1",
        )
        # Palette must never inherit scoped identity; clear even if the
        # outer environment leaks project/journal vars.
        for key in ("QS_PROJECT_PATH", "QS_JOURNAL_MODE",
                    "QS_PROJECT_SESSION_SCOPE", "QS_JOURNAL_SESSION_SCOPE"):
            self.base_env.pop(key, None)
        # Poison inherited scopes: the palette child must clear them even
        # when the bridge parent was launched with them set.
        self.poison_env = {
            "QS_PROJECT_PATH": "pages/Poison.md",
            "QS_PROJECT_SESSION_SCOPE": "/tmp/poison-projects/scope",
            "QS_JOURNAL_MODE": "1",
            "QS_JOURNAL_SESSION_SCOPE": "/tmp/poison-journals/scope",
        }
        self.bridges = []
        self.owned_pgids: set[int] = set()
        self.owned_pids: set[int] = set()

    def _record_owned(self):
        try:
            if self.fake_argv.exists():
                for line in self.fake_argv.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        pid = int(json.loads(line).get("pid", 0))
                    except (ValueError, AttributeError):
                        continue
                    if pid > 0:
                        self.owned_pgids.add(pid)
                        self.owned_pids.add(pid)
        except OSError:
            pass
        try:
            desc = self.ctl / "owned_descendants.log"
            if desc.exists():
                for line in desc.read_text(encoding="utf-8").splitlines():
                    try:
                        pid = int(line.strip())
                    except ValueError:
                        continue
                    if pid > 0:
                        self.owned_pids.add(pid)
        except OSError:
            pass
        for name in ("sleep_pid",):
            try:
                pid = int((self.ctl / name).read_text(encoding="utf-8").strip().split()[0])
            except (OSError, ValueError, IndexError):
                continue
            if pid > 0:
                self.owned_pids.add(pid)

    def _cleanup_owned_groups(self):
        self._record_owned()
        for pgid in sorted(self.owned_pgids):
            if pgid <= 0:
                continue
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                continue
            deadline = time.time() + 2.0
            while time.time() < deadline:
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    break
                time.sleep(0.05)
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                continue
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                continue
            deadline = time.time() + 2.0
            while time.time() < deadline:
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    break
                time.sleep(0.05)

    def tearDown(self):
        for bridge in self.bridges:
            try:
                bridge.kill()
                try:
                    bridge.proc.wait(timeout=3)
                except Exception:
                    pass
                bridge.shutdown()
            except Exception:
                pass
        try:
            self._cleanup_owned_groups()
        except Exception:
            pass
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def make_bridge(self, extra_env=None):
        env = dict(self.base_env)
        env["SCOPED_FAKE_CTL"] = str(self.ctl)
        env["SCOPED_FAKE_LOG"] = str(self.fake_log)
        env["SCOPED_FAKE_ARGV_LOG"] = str(self.fake_argv)
        env["SCOPED_FAKE_ENV_LOG"] = str(self.fake_env)
        env["PALETTE_FAKE_CTL"] = str(self.ctl)
        env["PALETTE_FAKE_LOG"] = str(self.fake_log)
        env["PALETTE_FAKE_ARGV_LOG"] = str(self.fake_argv)
        env["PALETTE_FAKE_ENV_LOG"] = str(self.fake_env)
        if extra_env:
            env.update(extra_env)
        bridge = BridgeHarness("palette", "", env)
        self.bridges.append(bridge)
        return bridge

    def fake_inputs(self):
        if not self.fake_log.exists():
            return []
        out = []
        for line in self.fake_log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out

    def fake_launches(self):
        if not self.fake_argv.exists():
            return []
        out = []
        for line in self.fake_argv.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def fake_envs(self):
        if not self.fake_env.exists():
            return []
        out = []
        for line in self.fake_env.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def wait_fake_input(self, predicate, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for entry in self.fake_inputs():
                try:
                    if predicate(entry):
                        return entry
                except Exception:
                    continue
            time.sleep(0.05)
        raise TimeoutError("fake pi never received expected input")

    def wait_fake_launch(self, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.fake_launches():
                return self.fake_launches()
            time.sleep(0.05)
        raise TimeoutError("fake pi was never launched")

    def start_and_wait_ready(self, bridge, timeout=20.0):
        ident = bridge.send("start", {})
        ack = bridge.wait_ack(ident, timeout=10.0)
        self.assertEqual(ack.get("accepted"), True)
        ready = bridge.wait_state(
            lambda s, u: s.get("ready") is True and s.get("processStarted") is True,
            timeout=timeout,
        )
        return ack, ready

    # -- 1: explicit lazy start ------------------------------------------
    def test_lazy_start_explicit_only(self):
        bridge = self.make_bridge()
        # No auto-spawn: the bridge process exists but no fake pi runs and no
        # session gates resolve before the explicit start op.
        time.sleep(0.7)
        bridge.drain()
        self.assertFalse(self.fake_argv.exists() and self.fake_argv.read_text(encoding="utf-8").strip(),
                         "palette bridge must not launch pi before explicit start")
        states = [u.get("state", {}) for u in bridge.updates]
        self.assertFalse(any(s.get("processStarted") for s in states),
                         "no processStarted before explicit start")
        _, ready = self.start_and_wait_ready(bridge)
        self.assertTrue(ready["state"]["processStarted"])
        self.assertTrue(ready["state"]["ready"])
        launches = self.wait_fake_launch()
        self.assertEqual(len(launches), 1, "exactly one pi launch per explicit start")
        sid = bridge.send("shutdown", {})
        bridge.wait_ack(sid, timeout=5.0)
        bridge.wait_exit(timeout=8.0)

    # -- 2: direct argv / env, no wrapper or scoped leaks -----------------
    def test_argv_env_direct_no_wrapper_no_scope_leak(self):
        bridge = self.make_bridge(extra_env=self.poison_env)
        self.start_and_wait_ready(bridge)
        launches = self.wait_fake_launch()
        self.assertTrue(launches, "fake pi must have been launched directly")
        argv = launches[0]["argv"]
        # Direct pi invocation, not a python wrapper. The fake logs
        # sys.argv[1:] (without the executable), so the direct signature is
        # `--mode rpc --approve --session-dir <pool>` with no wrapper flags.
        self.assertGreaterEqual(len(argv), 4)
        self.assertEqual(argv[:4], ["--mode", "rpc", "--approve", "--session-dir"])
        self.assertIn("--session-dir", argv)
        scope = Path(argv[argv.index("--session-dir") + 1])
        self.assertEqual(str(scope), str(self.pool),
                         "palette session-dir must be the temp PI pool itself")
        # No scoped subtrees, no repo pool, no wrapper artifacts.
        flat = " ".join(argv)
        self.assertNotIn("project_sessions.py", flat)
        self.assertNotIn("journal_sessions.py", flat)
        self.assertNotIn("python3", flat)
        self.assertNotIn("--project", argv)
        self.assertNotIn("--pending-name", argv)
        self.assertNotIn("--new-session", argv)
        self.assertNotIn("--continue", argv)
        self.assertNotIn("--system-prompt", argv)
        self.assertNotIn("--model", argv)
        self.assertNotIn("projects", scope.parts)
        self.assertNotIn("journals", scope.parts)
        self.assertFalse(str(scope).startswith(str(REPO_ROOT / ".pi")))
        self.assertTrue(str(scope).startswith(str(self.root)))
        # Cached session, when present, stays in the direct pool.
        state_session = bridge.updates[-1]["state"]["sessionFile"]
        self.assertTrue(state_session, "startup must install a session identity")
        self.assertTrue(str(state_session).startswith(str(scope)))
        envs = self.fake_envs()
        self.assertTrue(envs)
        penv = envs[0]
        # Inherited scoped vars must be cleared by the palette worker.
        self.assertFalse(penv.get("QS_PROJECT_PATH"), f"QS_PROJECT_PATH leaked: {penv}")
        self.assertIsNone(penv.get("QS_PROJECT_SESSION_SCOPE"))
        self.assertIsNone(penv.get("QS_JOURNAL_SESSION_SCOPE"))
        # QS_JOURNAL_MODE cleared (None or empty both prove no journal mode).
        self.assertFalse(penv.get("QS_JOURNAL_MODE"), f"QS_JOURNAL_MODE leaked: {penv}")
        self.assertEqual(penv.get("PI_CODING_AGENT_SESSION_DIR"), str(scope))
        self.assertNotEqual(penv.get("LOGSEQ_GRAPH"), REAL_DEFAULT_GRAPH)
        # Generic allowlist unchanged: no allowlist flags on the child argv.
        self.assertNotIn("--allowlist", argv)
        self.assertNotIn("--tools", argv)
        sid = bridge.send("shutdown", {})
        bridge.wait_ack(sid, timeout=5.0)
        bridge.wait_exit(timeout=8.0)

    # -- 3: missing safety extension disables prompts ---------------------
    def test_missing_extension_blocks_prompt_without_pi_write(self):
        (self.ctl / "missing_extension").write_text("", encoding="utf-8")
        bridge = self.make_bridge()
        ident = bridge.send("start", {})
        bridge.wait_ack(ident, timeout=10.0)
        blocked = bridge.wait_state(
            lambda s, u: s.get("processStarted") is True and s.get("stateOk") is True
            and s.get("extensionOk") is False
            and "Safety extension unavailable" in str(s.get("status", "")),
            timeout=15.0,
        )
        self.assertFalse(blocked["state"]["ready"])
        before = len(self.fake_inputs())
        pid = bridge.send("prompt", {"message": "should never send"})
        ack = bridge.wait_ack(pid, timeout=10.0)
        self.assertEqual(ack.get("accepted"), False)
        self.assertIn("failed", [e.get("name") for e in ack.get("events", [])])
        time.sleep(1.0)
        bridge.drain()
        self.assertEqual(len(self.fake_inputs()), before, "blocked prompt must never write to pi")
        self.assertFalse(any(e.get("type") == "prompt" for e in self.fake_inputs()))
        self.assertIsNone(bridge.proc.poll())
        sid = bridge.send("shutdown", {})
        bridge.wait_ack(sid, timeout=5.0)
        bridge.wait_exit(timeout=8.0)

    # -- 4: image payload forwarded unchanged ------------------------------
    def test_image_payload_forwarded_unchanged(self):
        bridge = self.make_bridge()
        self.start_and_wait_ready(bridge)
        images = [{"data": "aGVsbG8td29ybGQ=", "mime": "image/png", "name": "shot.png"}]
        pid = bridge.send("prompt", {"message": "describe this", "images": images})
        ack = bridge.wait_ack(pid, timeout=10.0)
        self.assertEqual(ack.get("accepted"), True)
        delivered = self.wait_fake_input(
            lambda e: e.get("type") == "prompt" and e.get("message") == "describe this")
        self.assertEqual(delivered.get("images"), images,
                         "ai image payload must be forwarded unchanged")
        finished = bridge.wait_event("finished", timeout=10.0)
        self.assertIsNotNone(finished)
        sid = bridge.send("shutdown", {})
        bridge.wait_ack(sid, timeout=5.0)
        bridge.wait_exit(timeout=8.0)

    # -- 5: text stream + history hide thinking/tool -----------------------
    def test_text_stream_and_history_hidden_no_leak(self):
        bridge = self.make_bridge()
        self.start_and_wait_ready(bridge)
        pid = bridge.send("prompt", {"message": "think-tool-me please"})
        bridge.wait_ack(pid, timeout=10.0)
        settled = bridge.wait_state(lambda s, u: s.get("answer") == "hello world", timeout=10.0)
        self.assertEqual(settled["state"]["answer"], "hello world")
        deltas = []
        for u in bridge.updates:
            for e in u.get("events", []) or []:
                if e.get("name") == "textDelta":
                    deltas.append(str((e.get("args", [""])[0])))
        self.assertIn("hello ", "".join(deltas))
        blob = json.dumps(bridge.updates)
        self.assertNotIn("thinking-secret-hidden", blob)
        self.assertNotIn("tool-secret-hidden", blob)
        self.assertNotIn("thinking_delta", blob)
        # History: hidden roles/parts must never surface as searchable text.
        (self.ctl / "messages_payload.json").write_text(json.dumps([
            {"role": "user", "content": "visible user", "timestamp": 1},
            {"role": "assistant", "content": [
                {"type": "text", "text": "visible assistant"},
                {"type": "thinking", "text": "thinking-secret-hidden"},
                {"type": "tool_call", "text": "tool-secret-hidden"}],
             "timestamp": 2},
            {"role": "tool", "content": "tool-secret-hidden", "timestamp": 3},
            {"role": "assistant", "content": [{"type": "image", "data": "AAA"}],
             "timestamp": 4},
            {"role": "system", "content": "system-secret-hidden", "timestamp": 5},
        ]), encoding="utf-8")
        rid = bridge.send("requestMessages", {})
        bridge.wait_ack(rid, timeout=10.0)
        loaded = bridge.wait_event("historyLoaded", timeout=10.0)
        self.assertIsNotNone(loaded)
        state = bridge.updates[-1]["state"]
        texts = [m.get("text", "") for m in state.get("messages", []) or []]
        self.assertIn("visible user", texts)
        self.assertIn("visible assistant", texts)
        joined = "\n".join(texts)
        self.assertNotIn("thinking-secret-hidden", joined)
        self.assertNotIn("tool-secret-hidden", joined)
        self.assertNotIn("system-secret-hidden", joined)
        self.assertEqual(len(texts), 2, "only textual user/assistant messages belong in history")
        sid = bridge.send("shutdown", {})
        bridge.wait_ack(sid, timeout=5.0)
        bridge.wait_exit(timeout=8.0)

    # -- 6: requestStats RPC + signal --------------------------------------
    def test_request_stats_rpc_and_signal(self):
        bridge = self.make_bridge()
        self.start_and_wait_ready(bridge)
        rid = bridge.send("requestStats", {})
        ack = bridge.wait_ack(rid, timeout=10.0)
        self.assertEqual(ack.get("accepted"), True, "requestStats must be accepted")
        self.wait_fake_input(lambda e: e.get("type") == "get_session_stats", timeout=10.0)
        stats_ev = bridge.wait_event("statsChanged", timeout=10.0)
        args = [e for e in stats_ev.get("events", []) if e.get("name") == "statsChanged"]
        self.assertTrue(args, "statsChanged signal must fire")
        self.assertIn("tokens", json.dumps(args[0].get("args", [])))
        # statsText is carried on the same update (already consumed); assert
        # on it directly instead of waiting for a further update.
        self.assertIn("tokens", stats_ev["state"].get("statsText", ""))
        sid = bridge.send("shutdown", {})
        bridge.wait_ack(sid, timeout=5.0)
        bridge.wait_exit(timeout=8.0)

    # -- 7: new / rename / restore (+cancel) then model + compact ---------
    def test_new_rename_restore_cancel_then_model_compact(self):
        bridge = self.make_bridge()
        _, ready = self.start_and_wait_ready(bridge)
        first = ready["state"]["sessionFile"]
        self.assertTrue(first)
        self.assertIsNone(ready["state"]["model"], "fake provides no model")
        self.assertEqual(ready["state"]["models"], [])

        new_id = bridge.send("newSession", {})
        self.assertEqual(bridge.wait_ack(new_id, timeout=10.0).get("accepted"), True)
        fresh = bridge.wait_state(
            lambda s, u: s.get("ready") is True and s.get("sessionFile") not in ("", first),
            timeout=15.0)
        new_file = fresh["state"]["sessionFile"]
        self.assertNotEqual(new_file, first)
        self.assertEqual(fresh["state"]["messages"], [])

        rn = bridge.send("rename", {"name": "Palette Name"})
        self.assertEqual(bridge.wait_ack(rn, timeout=10.0).get("accepted"), True)
        renamed = bridge.wait_state(lambda s, u: s.get("sessionName") == "Palette Name", timeout=10.0)
        self.assertEqual(renamed["state"]["sessionName"], "Palette Name")
        self.wait_fake_input(lambda e: e.get("type") == "set_session_name" and e.get("name") == "Palette Name")

        # Restore cancel: park the picker, abort, then release; no replay.
        (self.ctl / "block_picker").write_text("", encoding="utf-8")
        sw = bridge.send("switchSession", {})
        self.assertEqual(bridge.wait_ack(sw, timeout=10.0).get("accepted"), True)
        self.wait_fake_input(lambda e: e.get("type") == "prompt" and e.get("message") == "/desktop-sessions")
        abort = bridge.send("abort", {})
        bridge.wait_ack(abort, timeout=10.0)
        try:
            (self.ctl / "block_picker").unlink()
        except FileNotFoundError:
            pass
        bridge.wait_state(
            lambda s, u: s.get("ready") is True or any(
                e.get("name") == "failed" for e in u.get("events", []) or []),
            timeout=15.0)

        # Model with fake (no models listed): explicit item still forwards.
        item = {"provider": "test", "modelId": "m1"}
        cm = bridge.send("chooseModel", {"item": item})
        cm_ack = bridge.wait_ack(cm, timeout=10.0)
        self.assertEqual(cm_ack.get("accepted"), True)
        self.wait_fake_input(lambda e: e.get("type") == "set_model"
                             and e.get("provider") == "test" and e.get("modelId") == "m1",
                             timeout=10.0)
        bridge.wait_state(lambda s, u: s.get("ready") is True, timeout=15.0)

        cp = bridge.send("compact", {})
        self.assertEqual(bridge.wait_ack(cp, timeout=10.0).get("accepted"), True)
        self.wait_fake_input(lambda e: e.get("type") == "compact", timeout=10.0)
        done = bridge.wait_state(
            lambda s, u: s.get("compacting") is False and s.get("ready") is True,
            timeout=15.0)
        self.assertFalse(done["state"]["compacting"])
        sid = bridge.send("shutdown", {})
        bridge.wait_ack(sid, timeout=5.0)
        bridge.wait_exit(timeout=8.0)

    # -- 8: approvals exact/timeout/stale, no auto-restart -----------------
    def test_approval_exact_timeout_stale_no_restart(self):
        bridge = self.make_bridge()
        self.start_and_wait_ready(bridge)
        aid = bridge.send("prompt", {"message": "please approve-me now"})
        bridge.wait_ack(aid, timeout=10.0)
        approval = bridge.wait_state(
            lambda s, u: s.get("pendingApproval") is not None
            and (s.get("pendingApproval") or {}).get("id") == "appr-1",
            timeout=10.0)
        self.assertEqual(approval["state"]["pendingApproval"]["method"], "confirm")
        fields = {"confirmed": False, "note": "exact \u20ac pass"}
        rid = bridge.send("respond", {"requestId": "appr-1", "fields": fields})
        self.assertEqual(bridge.wait_ack(rid, timeout=10.0).get("accepted"), True)
        delivered = self.wait_fake_input(
            lambda e: e.get("type") == "extension_ui_response" and e.get("id") == "appr-1",
            timeout=10.0)
        self.assertEqual(delivered.get("confirmed"), False)
        self.assertEqual(delivered.get("note"), "exact \u20ac pass")
        bridge.wait_state(lambda s, u: s.get("pendingApproval") is None, timeout=10.0)
        count_after_first = len(self.fake_inputs())

        dup = bridge.send("respond", {"requestId": "appr-1", "fields": fields})
        self.assertEqual(bridge.wait_ack(dup, timeout=10.0).get("accepted"), False)
        unknown = bridge.send("respond", {"requestId": "no-such-id", "fields": {}})
        self.assertEqual(bridge.wait_ack(unknown, timeout=10.0).get("accepted"), False)
        time.sleep(1.0)
        self.assertEqual(len(self.fake_inputs()), count_after_first)
        self.assertIsNone(bridge.proc.poll(), "stale/duplicate respond must not restart")
        self.assertTrue(bridge.updates[-1]["state"]["processStarted"])

        eid = bridge.send("prompt", {"message": "please expire-me now"})
        bridge.wait_ack(eid, timeout=10.0)
        bridge.wait_state(lambda s, u: (s.get("pendingApproval") or {}).get("id") == "exp-1",
                          timeout=10.0)
        expired = self.wait_fake_input(
            lambda e: e.get("type") == "extension_ui_response" and e.get("id") == "exp-1"
            and e.get("cancelled") is True,
            timeout=8.0)
        self.assertTrue(expired.get("cancelled"))
        bridge.wait_state(lambda s, u: s.get("pendingApproval") is None, timeout=8.0)
        before_late = len(self.fake_inputs())
        late = bridge.send("respond", {"requestId": "exp-1", "fields": {"confirmed": True}})
        self.assertEqual(bridge.wait_ack(late, timeout=10.0).get("accepted"), False)
        time.sleep(0.5)
        self.assertEqual(len(self.fake_inputs()), before_late)
        self.assertIsNone(bridge.proc.poll(), "expiry must not restart the child")
        sid = bridge.send("shutdown", {})
        bridge.wait_ack(sid, timeout=5.0)
        bridge.wait_exit(timeout=8.0)

    # -- 9: EOF / SIGTERM kill the child group, bounded --------------------
    def test_eof_and_sigterm_kill_child_group_bounded(self):
        bridge = self.make_bridge(extra_env={"FAKE_SPAWN_SLEEP": "1"})
        self.start_and_wait_ready(bridge)
        sleep_pid = None
        deadline = time.time() + 10.0
        while time.time() < deadline and sleep_pid is None:
            try:
                sleep_pid = int((self.ctl / "sleep_pid").read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                time.sleep(0.1)
        self.assertIsNotNone(sleep_pid, "fake must spawn a grandchild sleep")
        fake_pid = self.fake_launches()[-1]["pid"]
        start = time.time()
        bridge.close_stdin()
        bridge.wait_exit(timeout=8.0)
        self.assertLess(time.time() - start, 8.0, "EOF shutdown must be bounded")
        time.sleep(0.5)
        assert isinstance(fake_pid, int) and isinstance(sleep_pid, int)
        for pid in (fake_pid, sleep_pid):
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                continue
            else:
                try:
                    status = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()
                    self.assertEqual(status[0], "Z", f"pid {pid} survived EOF group kill")
                except FileNotFoundError:
                    pass
                else:
                    self.fail(f"pid {pid} survived EOF group kill")

        (self.ctl / "sleep_pid").unlink(missing_ok=True)
        bridge2 = self.make_bridge(extra_env={"FAKE_SPAWN_SLEEP": "1"})
        self.start_and_wait_ready(bridge2)
        sleep_pid2 = None
        deadline = time.time() + 10.0
        while time.time() < deadline and sleep_pid2 is None:
            try:
                text = (self.ctl / "sleep_pid").read_text(encoding="utf-8").strip()
                if text and int(text) != sleep_pid:
                    sleep_pid2 = int(text)
                else:
                    time.sleep(0.1)
            except (OSError, ValueError):
                time.sleep(0.1)
        self.assertIsNotNone(sleep_pid2)
        fake_pid2 = self.fake_launches()[-1]["pid"]
        start = time.time()
        bridge2.proc.send_signal(signal.SIGTERM)
        bridge2.wait_exit(timeout=8.0)
        self.assertLess(time.time() - start, 8.0, "SIGTERM shutdown must be bounded")
        time.sleep(0.5)
        assert isinstance(fake_pid2, int) and isinstance(sleep_pid2, int)
        for pid in (fake_pid2, sleep_pid2):
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                continue
            else:
                try:
                    status = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()
                    self.assertEqual(status[0], "Z", f"pid {pid} survived SIGTERM group kill")
                except FileNotFoundError:
                    pass
                else:
                    self.fail(f"pid {pid} survived SIGTERM group kill")

    # -- 10: explicit retry/resume, palette pool disjoint from scoped ------
    def test_retry_resume_cached_not_shared_scoped(self):
        bridge = self.make_bridge()
        _, ready = self.start_and_wait_ready(bridge)
        first_session = ready["state"]["sessionFile"]
        self.assertTrue(first_session)
        self.assertTrue(str(first_session).startswith(str(self.pool)))
        self.assertNotIn("projects", Path(first_session).parts)
        self.assertNotIn("journals", Path(first_session).parts)
        first_launches = len(self.fake_launches())

        die = bridge.send("prompt", {"message": "trigger __die__ now"})
        self.assertEqual(bridge.wait_ack(die, timeout=10.0).get("accepted"), True)
        died = bridge.wait_event("failed", timeout=10.0)
        texts = " ".join(str(e.get("args", "")) for e in died.get("events", []))
        self.assertIn("pi interrupted", texts)
        self.assertIn("no prompt was replayed", texts)
        self.assertFalse(died["state"]["processStarted"])
        self.assertFalse(died["state"]["desiredRunning"])
        prompts_before = [e for e in self.fake_inputs() if e.get("type") == "prompt"]
        time.sleep(1.0)
        prompts_after = [e for e in self.fake_inputs() if e.get("type") == "prompt"]
        self.assertEqual(len(prompts_after), len(prompts_before))
        self.assertEqual(len(self.fake_launches()), first_launches, "death must not auto-restart")

        self.start_and_wait_ready(bridge)
        relaunches = self.fake_launches()
        self.assertGreater(len(relaunches), first_launches)
        resumed_argv = relaunches[-1]["argv"]
        self.assertIn("--session", resumed_argv)
        self.assertIn(first_session, resumed_argv)
        self.assertEqual(resumed_argv[resumed_argv.index("--session-dir") + 1], str(self.pool))
        self.assertNotIn("projects", resumed_argv[resumed_argv.index("--session-dir") + 1].split(os.sep))
        # Explicit pause/resume preserves the same validated cache file.
        stop = bridge.send("stopIdle", {})
        stop_ack = bridge.wait_ack(stop, timeout=10.0)
        self.assertEqual(stop_ack.get("accepted"), True)
        paused = bridge.wait_state(
            lambda s, u: s.get("idleStopped") is True and s.get("processStarted") is False,
            timeout=10.0)
        cached = paused["state"]["sessionFile"]
        self.assertTrue(cached)
        launches_before_resume = len(self.fake_launches())
        resume = bridge.send("start", {})
        bridge.wait_ack(resume, timeout=10.0)
        bridge.wait_state(lambda s, u: s.get("ready") is True, timeout=15.0)
        self.assertGreater(len(self.fake_launches()), launches_before_resume)
        self.assertIn(cached, self.fake_launches()[-1]["argv"])
        sid = bridge.send("shutdown", {})
        bridge.wait_ack(sid, timeout=5.0)
        bridge.wait_exit(timeout=8.0)


class PaletteCapabilityMatrixTests(unittest.TestCase):
    """Phase 1 §2.2: static capability-matrix pins for desktop-agent.ts.

    No bridge/pi is launched: these assert the documented matrix (§2.2)
    directly on the extension source so they stay green without cargo.
    """

    EXTENSION = (REPO_ROOT / ".pi" / "extensions" / "desktop-agent.ts").read_text(encoding="utf-8")
    SYSTEM = (REPO_ROOT / ".pi" / "SYSTEM.md").read_text(encoding="utf-8")

    DESKTOP_READ = ["desktop_current_context", "desktop_project_todos",
                    "desktop_project_logseq_context", "desktop_project_activity",
                    "desktop_current_session", "desktop_search_activity",
                    "desktop_get_session", "desktop_resume_plan",
                    "session_search"]

    def test_desktop_read_tools_pass_every_scope_gate(self):
        marker = 'if ((DESKTOP_READ_TOOLS as string[]).includes(event.toolName)) return;'
        self.assertIn(marker, self.EXTENSION)
        for name in self.DESKTOP_READ:
            self.assertIn(f'"{name}"', self.EXTENSION)
            self.assertIn(f'name: "{name}"', self.EXTENSION)

    def test_agenda_registered_for_palette_and_project_journal_denied(self):
        # Registered once under `if (!journalMode())`: palette + project.
        self.assertIn("if (!journalMode()) {", self.EXTENSION)
        block = self.EXTENSION[self.EXTENSION.index("if (!journalMode()) {"):]
        self.assertIn('name: "logseq_agenda_list"', block)
        self.assertIn('name: "logseq_agenda_add"', block)
        # Guards deny journal only (no palette-only remnant).
        self.assertNotIn("palette-only", self.EXTENSION)
        self.assertIn("agenda list is unavailable in journal mode", self.EXTENSION)
        self.assertIn("agenda add is unavailable in journal mode", self.EXTENSION)
        self.assertIn("agenda tool is unavailable in journal mode", self.EXTENSION)

    def test_project_allowlist_includes_agenda(self):
        allow = self.EXTENSION[self.EXTENSION.index("if (projectMode() && !["):]
        allow = allow[:allow.index("].includes")]
        self.assertIn('"logseq_agenda_list"', allow)
        self.assertIn('"logseq_agenda_add"', allow)

    def test_agenda_flow_unchanged_preview_and_confirm(self):
        # §8.2: no silent writes — the list/select flow with preview + UI
        # confirm is untouched by the scope widening.
        self.assertIn("Approve add to daily todos", self.EXTENSION)
        self.assertIn("stale revision; list again and request a new approval", self.EXTENSION)

    def test_palette_default_scoping_documented(self):
        self.assertEqual(
            self.EXTENSION.count("Omit project to use the fresh current project"), 3)

    def test_journal_handoff_documented_allowlist_unchanged(self):
        self.assertIn('file to project X', self.EXTENSION)
        journal_block = self.EXTENSION[self.EXTENSION.index("if (journalMode()) {"):]
        journal_block = journal_block[:journal_block.index("if (projectMode() && !journalMode()) {")]
        self.assertIn('name: "logseq_journal_context"', journal_block)
        self.assertIn('name: "logseq_journal_append"', journal_block)
        self.assertNotIn("logseq_agenda", journal_block)

    def test_system_matrix_matches_extension(self):
        self.assertIn("logseq_agenda_list", self.SYSTEM)
        self.assertIn("Available in palette and project scopes", self.SYSTEM)
        self.assertIn("journal is denied", self.SYSTEM)
        self.assertIn("file to project X", self.SYSTEM)
        self.assertIn("to use the fresh current project", self.SYSTEM)


if __name__ == "__main__":
    unittest.main()
