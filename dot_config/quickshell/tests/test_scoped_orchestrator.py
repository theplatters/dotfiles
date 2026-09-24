"""Hermetic scoped-orchestrator bridge integration tests.

No real pi / model / notes are touched. A temporary fake ``pi`` on PATH is
exercised through the REAL ``scripts/project_sessions.py`` and
``scripts/journal_sessions.py`` wrappers with a temporary valid graph and
state. The REAL Rust bridge binary is driven over stdin JSONL v1
(start/prompt/abort/newSession/switchSession/rename/requestMessages/respond/
stopIdle/shutdown) and state/events updates are asserted.

Run focused with cache disabled:
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_scoped_orchestrator -v
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
SCOPED_QML = REPO_ROOT / "widgets" / "ScopedAgent.qml"
REAL_DEFAULT_GRAPH = "/home/franzs/Nextcloud/Documents/Notes"

CARGO = shutil.which("cargo")
BUILT = False
BUILD_ERROR = ""


def ensure_built():
    global BUILT, BUILD_ERROR
    if BUILT:
        return True
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


FAKE_PI_SOURCE = r'''#!/usr/bin/env python3
import json, os, sys, time, uuid, subprocess, threading
CTL = os.environ.get("SCOPED_FAKE_CTL", "")
LOG = os.environ.get("SCOPED_FAKE_LOG", "")
ARGV_LOG = os.environ.get("SCOPED_FAKE_ARGV_LOG", "")
ENV_LOG = os.environ.get("SCOPED_FAKE_ENV_LOG", "")
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
    current_name = parsed["name"]
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
    else_branch_sid = None
    try:
        with open(current, encoding="utf-8") as h:
            sid = json.loads(h.readline()).get("id", str(uuid.uuid4()))
    except Exception:
        sid = str(uuid.uuid4())
    session_id = sid
    counter = [0]
    log_json(ARGV_LOG, {"pid": os.getpid(), "argv": argv})
    log_json(ENV_LOG, {"pid": os.getpid(),
                       "PI_CODING_AGENT_SESSION_DIR": os.environ.get("PI_CODING_AGENT_SESSION_DIR"),
                       "QS_PROJECT_SESSION_SCOPE": os.environ.get("QS_PROJECT_SESSION_SCOPE"),
                       "QS_JOURNAL_SESSION_SCOPE": os.environ.get("QS_JOURNAL_SESSION_SCOPE"),
                       "QS_PROJECT_PATH": os.environ.get("QS_PROJECT_PATH"),
                       "QS_JOURNAL_MODE": os.environ.get("QS_JOURNAL_MODE"),
                       "LOGSEQ_GRAPH": os.environ.get("LOGSEQ_GRAPH")})
    # Direct child may ignore TERM itself: bridge must escalate to KILL bounded.
    if os.environ.get("FAKE_IGNORE_TERM") == "1":
        try:
            import signal as _sig
            _sig.signal(_sig.SIGTERM, _sig.SIG_IGN)
        except Exception:
            pass
    # Optional grandchild to verify process-group kills. Exact pid logged for
    # owned-only teardown (never pattern-matched).
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
    # TERM-resistant descendant: survives TERM and the direct parent exit,
    # keeping the pgid alive until KILL. Mirrors pi_child resistant-group test.
    if os.environ.get("FAKE_RESISTANT") == "1":
        try:
            subprocess.Popen(["sh", "-c", 'trap "" TERM; sleep 30 &'])
            if CTL:
                with open(os.path.join(CTL, "resistant_armed"), "w") as h:
                    h.write(str(os.getpid()))
        except Exception:
            pass
    # Wedged-transport modes: long unterminated stdout/stderr lines and a
    # stdin the fake never drains. Each must still allow bounded shutdown via
    # the bridge finite transport deadline (fail-closed), never a hang.
    if os.environ.get("FAKE_HOG_STDOUT_BYTES"):
        try:
            _hog_n = int(os.environ.get("FAKE_HOG_STDOUT_BYTES") or "0")
        except ValueError:
            _hog_n = 0
        def _hog_out(n=_hog_n):
            try:
                chunk = b"z" * 65536
                left = n
                with OUT_LOCK:
                    while left > 0:
                        step = chunk[:min(len(chunk), left)]
                        sys.stdout.buffer.write(step)
                        left -= len(step)
                    sys.stdout.buffer.flush()
            except Exception:
                pass
        threading.Thread(target=_hog_out, daemon=True).start()
    if os.environ.get("FAKE_HOG_STDERR_BYTES"):
        try:
            _hog_e = int(os.environ.get("FAKE_HOG_STDERR_BYTES") or "0")
        except ValueError:
            _hog_e = 0
        def _hog_err(n=_hog_e):
            try:
                chunk = b"e" * 65536
                left = n
                while left > 0:
                    step = chunk[:min(len(chunk), left)]
                    sys.stderr.buffer.write(step)
                    left -= len(step)
                sys.stderr.buffer.flush()
            except Exception:
                pass
        threading.Thread(target=_hog_err, daemon=True).start()
    if os.environ.get("FAKE_NONREADING") == "1":
        # Never drain stdin: bridge writes must hit the finite deadline and
        # fail closed instead of wedging Stop/expiry/EOF handling.
        while True:
            time.sleep(1.0)
    stdin = sys.stdin
    shared = {"current": current, "name": current_name, "sid": session_id, "counter": 0, "session_dir": session_dir, "last_new": "", "last_picker": ""}
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
    def do_picker_prompt(rid):
        respond(rid, "prompt", True, {})
        emit({"type": "extension_ui_request", "id": "sel-1", "method": "select",
              "title": "sessions", "options": ["a", "b"], "timeout": 60000})
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
                if CTL and os.path.exists(os.path.join(CTL, "emit_old_state_once")):
                    try:
                        os.unlink(os.path.join(CTL, "emit_old_state_once"))
                    except OSError:
                        pass
                    emit({"type": "response", "id": "qs-stale-injected", "command": "get_state",
                          "success": True, "data": {"sessionFile": "/tmp/evil.jsonl",
                          "sessionName": "evil", "sessionId": "evil",
                          "model": None, "isStreaming": False, "isCompacting": False}})
                if CTL and os.path.exists(os.path.join(CTL, "emit_foreign_new_session_once")):
                    try:
                        os.unlink(os.path.join(CTL, "emit_foreign_new_session_once"))
                    except OSError:
                        pass
                    emit({"type": "response", "id": "qs-foreign-1", "command": "new_session",
                          "success": True, "data": {"cancelled": False}})
                if CTL and os.path.exists(os.path.join(CTL, "emit_mismatched_new_once")):
                    try:
                        os.unlink(os.path.join(CTL, "emit_mismatched_new_once"))
                    except OSError:
                        pass
                    with STATE_LOCK:
                        reuse = shared.get("last_new") or "qs-1"
                    # Same id as the inflight new_session but wrong command:
                    # must be ignored WITHOUT consuming correlation.
                    emit({"type": "response", "id": reuse, "command": "prompt",
                          "success": True, "data": {}})
                if CTL and os.path.exists(os.path.join(CTL, "emit_mismatched_picker_once")):
                    try:
                        os.unlink(os.path.join(CTL, "emit_mismatched_picker_once"))
                    except OSError:
                        pass
                    with STATE_LOCK:
                        reuse = shared.get("last_picker") or "qs-1"
                    emit({"type": "response", "id": reuse, "command": "new_session",
                          "success": True, "data": {"cancelled": False}})
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
        # One-shot corrupt output instead of a real response.
        if ctl_exists("corrupt_next"):
            try:
                os.unlink(os.path.join(CTL, "corrupt_next"))
            except OSError:
                pass
            with OUT_LOCK:
                sys.stdout.write("NOT-JSON{{{\n")
                sys.stdout.flush()
            continue
        # Capture payload snapshot at arrival for stale-fencing checks.
        captured_messages = None
        if rtype == "get_messages":
            payload_path = os.path.join(CTL, "messages_payload.json") if CTL else ""
            try:
                if payload_path and os.path.exists(payload_path):
                    with open(payload_path, encoding="utf-8") as h:
                        captured_messages = json.load(h)
            except Exception:
                captured_messages = None
        # Track inflight change ids so mismatched-reuse injection can target them.
        if rtype == "new_session":
            with STATE_LOCK:
                shared["last_new"] = rid
        if rtype == "prompt" and obj.get("message") == "/desktop-sessions":
            with STATE_LOCK:
                shared["last_picker"] = rid
        # Blocking controls are async so the read loop never stalls: the
        # parked reply lands after the test unblocks, letting abort-before-ack
        # and stale-generation races be exercised deterministically.
        if rtype == "new_session" and ctl_exists("block_new_session"):
            async_after("block_new_session", lambda rid=rid: do_new_session(rid))
            continue
        if rtype == "prompt" and obj.get("message") == "/desktop-sessions" and ctl_exists("block_picker"):
            async_after("block_picker", lambda rid=rid: do_picker_prompt(rid))
            continue
        if rtype == "get_messages" and ctl_exists("block_get_messages"):
            async_after("block_get_messages",
                        lambda rid=rid, captured=captured_messages: do_get_messages(rid, captured))
            continue
        delay_name = f"delay_{rtype}"
        if ctl_exists(delay_name):
            try:
                secs = float(ctl_text(delay_name).strip() or "0")
            except ValueError:
                secs = 0
            time.sleep(max(0.0, min(secs, 5.0)))
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
            if "diag-me" in msg:
                sys.stderr.write("fake diagnostic line\n")
                sys.stderr.flush()
            respond(rid, rtype, True, {})
            if msg == "/desktop-sessions":
                emit({"type": "extension_ui_request", "id": "sel-1", "method": "select",
                      "title": "sessions", "options": ["a", "b"], "timeout": 60000})
            elif "approve-me" in msg:
                emit({"type": "extension_ui_request", "id": "appr-1", "method": "confirm",
                      "message": "approve?", "timeout": 60000})
            elif "approve-two" in msg:
                emit({"type": "extension_ui_request", "id": "appr-2", "method": "confirm",
                      "message": "approve second?", "timeout": 60000})
            elif "expire-me" in msg:
                emit({"type": "extension_ui_request", "id": "exp-1", "method": "confirm",
                      "message": "expire?", "timeout": 1200})
            else:
                emit({"type": "message_update",
                      "assistantMessageEvent": {"type": "text_delta", "delta": "hello "}})
                emit({"type": "message_end",
                      "message": {"role": "assistant", "content": "hello world"}})
                emit({"type": "agent_end"})
                emit({"type": "agent_settled"})
                if "approve-me" in msg or "expire-me" in msg or "approve-two" in msg:
                    pass
            # Settle ordinary approval prompts too so busy clears deterministically.
            if "approve-me" in msg or "expire-me" in msg or "approve-two" in msg:
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


def _skip_unless_built(testcase):
    if CARGO is None:
        testcase.skipTest(f"cargo unavailable; cannot build bridge ({BUILD_ERROR})")
    if not ensure_built():
        testcase.fail(f"cargo build failed; cannot exercise bridge ({BUILD_ERROR})")
    if not BINARY.exists():
        testcase.fail("bridge binary missing after build")


class ScopedOrchestratorHarness(unittest.TestCase):
    def setUp(self):
        _skip_unless_built(self)
        # Hermetic temp layout: graph + state + bin + control + logs.
        self.tmp = tempfile.TemporaryDirectory(prefix="scoped-orch-")
        self.root = Path(self.tmp.name)
        self.graph = self.root / "graph"
        (self.graph / "pages").mkdir(parents=True)
        (self.graph / "journals").mkdir(parents=True)
        (self.graph / "pages" / "Project.md").write_text("# project\n", encoding="utf-8")
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
        # Guard: never touch the real notes graph or a real pi.
        self.assertNotEqual(str(self.graph.resolve()), REAL_DEFAULT_GRAPH)
        self.base_env = dict(
            os.environ,
            PATH=f"{self.bindir}{os.pathsep}{os.environ.get('PATH', '')}",
            LOGSEQ_GRAPH=str(self.graph),
            XDG_STATE_HOME=str(self.state_home),
            PI_CODING_AGENT_SESSION_DIR=str(self.pool),
            PI_OFFLINE="1",
        )
        self.base_env.pop("QS_PROJECT_PATH", None)
        self.base_env.pop("QS_JOURNAL_MODE", None)
        self.base_env.pop("QS_PROJECT_SESSION_SCOPE", None)
        self.base_env.pop("QS_JOURNAL_SESSION_SCOPE", None)
        self.bridges = []
        self.owned_pgids: set[int] = set()
        self.owned_pids: set[int] = set()

    def _record_owned(self):
        # Exact owned identities only: fake group leaders (=pgids) from the
        # argv log plus explicitly logged descendant pids. Never pattern-match.
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
        # Bounded, exact-only teardown of owned groups. No pkill, no scans.
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
        # Owned-group sweep only; never broad-pattern kills.
        try:
            self._cleanup_owned_groups()
        except Exception:
            pass
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def make_bridge(self, mode="project", project_page="pages/Project.md", extra_env=None):
        env = dict(self.base_env)
        env["SCOPED_FAKE_CTL"] = str(self.ctl)
        env["SCOPED_FAKE_LOG"] = str(self.fake_log)
        env["SCOPED_FAKE_ARGV_LOG"] = str(self.fake_argv)
        env["SCOPED_FAKE_ENV_LOG"] = str(self.fake_env)
        if extra_env:
            env.update(extra_env)
        bridge = BridgeHarness(mode, project_page, env)
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

    def start_and_wait_ready(self, bridge, timeout=20.0):
        ident = bridge.send("start", {})
        ack = bridge.wait_ack(ident, timeout=10.0)
        self.assertEqual(ack.get("accepted"), True)
        ready = bridge.wait_state(
            lambda s, u: s.get("ready") is True and s.get("processStarted") is True,
            timeout=timeout,
        )
        return ack, ready

    # -- 1: startup / prompt / rename / requestMessages -------------------
    def test_startup_ready_prompt_rename_request_messages(self):
        bridge = self.make_bridge(mode="project")
        _, ready = self.start_and_wait_ready(bridge)
        state = ready["state"]
        self.assertTrue(state["stateOk"])
        self.assertTrue(state["extensionOk"])
        self.assertFalse(state["sessionRefreshPending"])
        session_file = state["sessionFile"]
        self.assertTrue(session_file, "startup must install a session identity")
        self.assertTrue(Path(session_file).exists(), "fake must materialize the session file")
        header = json.loads(Path(session_file).read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(header["type"], "session")

        # requestMessages round-trips through the live child.
        rid = bridge.send("requestMessages", {})
        ack = bridge.wait_ack(rid, timeout=10.0)
        self.assertEqual(ack.get("accepted"), True)
        loaded = bridge.wait_event("historyLoaded", timeout=10.0)
        args = loaded["events"] and [e for e in loaded["events"] if e.get("name") == "historyLoaded"][0]["args"]
        self.assertTrue(args, "historyLoaded must carry session identity")

        # Prompt lifecycle emits deltas then finishes and refreshes state.
        pid = bridge.send("prompt", {"message": "hello world"})
        ack = bridge.wait_ack(pid, timeout=10.0)
        self.assertEqual(ack.get("accepted"), True)
        finished = bridge.wait_event("finished", timeout=10.0)
        self.assertIsNotNone(finished)
        settled = bridge.wait_state(lambda s, u: s.get("answer") == "hello world", timeout=10.0)
        self.assertEqual(settled["state"]["status"], "Ready")
        self.wait_fake_input(lambda e: e.get("type") == "prompt" and e.get("message") == "hello world")

        # Rename installs the new name via an authoritative refresh.
        nid = bridge.send("rename", {"name": "New Name"})
        ack = bridge.wait_ack(nid, timeout=10.0)
        self.assertEqual(ack.get("accepted"), True)
        renamed = bridge.wait_state(lambda s, u: s.get("sessionName") == "New Name", timeout=10.0)
        self.assertEqual(renamed["state"]["sessionName"], "New Name")
        self.wait_fake_input(lambda e: e.get("type") == "set_session_name" and e.get("name") == "New Name")

        sid = bridge.send("shutdown", {})
        bridge.wait_ack(sid, timeout=5.0)
        bridge.wait_exit(timeout=8.0)

    # -- 2: missing safety extension blocks prompts -----------------------
    def test_missing_extension_blocks_prompt_without_pi_write(self):
        (self.ctl / "missing_extension").write_text("", encoding="utf-8")
        bridge = self.make_bridge(mode="project")
        ident = bridge.send("start", {})
        bridge.wait_ack(ident, timeout=10.0)
        blocked_state = bridge.wait_state(
            lambda s, u: s.get("processStarted") is True and s.get("stateOk") is True
            and s.get("extensionOk") is False
            and "Safety extension unavailable" in str(s.get("status", "")),
            timeout=15.0,
        )
        self.assertFalse(blocked_state["state"]["ready"])
        self.assertIn("Safety extension unavailable", blocked_state["state"]["status"])
        before = len(self.fake_inputs())
        pid = bridge.send("prompt", {"message": "should never send"})
        ack = bridge.wait_ack(pid, timeout=10.0)
        self.assertEqual(ack.get("accepted"), False)
        names = [e.get("name") for e in ack.get("events", [])]
        self.assertIn("failed", names)
        time.sleep(1.0)
        bridge.drain()
        after_inputs = self.fake_inputs()
        self.assertEqual(len(after_inputs), before, "blocked prompt must never write to pi")
        self.assertFalse(any(e.get("type") == "prompt" for e in after_inputs))
        # Bridge stays alive and can shut down cleanly; no restart happened.
        self.assertIsNone(bridge.proc.poll())
        sid = bridge.send("shutdown", {})
        bridge.wait_ack(sid, timeout=5.0)
        bridge.wait_exit(timeout=8.0)

    # -- 3: wrapper argv / env scope --------------------------------------
    def test_wrapper_argv_env_scope_project_and_journal(self):
        # Project mode through the real wrapper.
        bridge = self.make_bridge(mode="project", project_page="pages/Project.md")
        self.start_and_wait_ready(bridge)
        launches = self.fake_launches()
        self.assertTrue(launches, "fake pi must have been launched through the wrapper")
        argv = launches[0]["argv"]
        self.assertEqual(argv[:4], ["--mode", "rpc", "--approve", "--session-dir"])
        scope = Path(argv[4])
        self.assertFalse(str(scope).startswith(str(REPO_ROOT / ".pi")))
        self.assertTrue(str(scope).startswith(str(self.pool)))
        self.assertIn("projects", scope.parts)
        self.assertNotIn("--continue", argv)
        self.assertNotIn("--system-prompt", argv)
        self.assertNotIn("--model", argv)
        envs = self.fake_envs()
        self.assertTrue(envs)
        penv = envs[0]
        self.assertEqual(penv["QS_PROJECT_PATH"], "pages/Project.md")
        self.assertIsNone(penv["QS_JOURNAL_MODE"])
        self.assertEqual(penv["PI_CODING_AGENT_SESSION_DIR"], str(scope))
        self.assertEqual(penv["QS_PROJECT_SESSION_SCOPE"], str(scope))
        self.assertEqual(Path(penv["LOGSEQ_GRAPH"]), self.graph.resolve())
        self.assertNotEqual(penv["LOGSEQ_GRAPH"], REAL_DEFAULT_GRAPH)
        # Cached session, when present, is passed explicitly and stays in scope.
        state_session = bridge.updates[-1]["state"]["sessionFile"]
        self.assertTrue(str(state_session).startswith(str(scope)))
        sid = bridge.send("shutdown", {})
        bridge.wait_ack(sid, timeout=5.0)
        bridge.wait_exit(timeout=8.0)

        # Journal mode uses the sibling journals scope and clears project vars.
        bridge2 = self.make_bridge(mode="journal")
        self.start_and_wait_ready(bridge2)
        launches2 = self.fake_launches()
        self.assertGreaterEqual(len(launches2), 2)
        jargv = launches2[-1]["argv"]
        jscope = Path(jargv[jargv.index("--session-dir") + 1])
        self.assertIn("journals", jscope.parts)
        self.assertNotIn("projects", jscope.parts)
        jenv = self.fake_envs()[-1]
        self.assertEqual(jenv["QS_JOURNAL_MODE"], "1")
        self.assertFalse(jenv["QS_PROJECT_PATH"])
        self.assertIsNone(jenv["QS_PROJECT_SESSION_SCOPE"])
        self.assertEqual(jenv["QS_JOURNAL_SESSION_SCOPE"], str(jscope))
        self.assertEqual(jenv["PI_CODING_AGENT_SESSION_DIR"], str(jscope))
        sid2 = bridge2.send("shutdown", {})
        bridge2.wait_ack(sid2, timeout=5.0)
        bridge2.wait_exit(timeout=8.0)

    # -- 4: stale history + abort-before-ack gates -------------------------
    def test_stale_history_ignored_and_abort_before_ack_retains_gates(self):
        bridge = self.make_bridge(mode="project")
        self.start_and_wait_ready(bridge)

        # Stale get_messages success is fenced by generation: capture STALE,
        # then bump the generation with newSession before the stale reply lands.
        (self.ctl / "messages_payload.json").write_text(
            json.dumps([{"role": "user", "content": "STALE", "timestamp": 1}]),
            encoding="utf-8",
        )
        (self.ctl / "block_get_messages").write_text("", encoding="utf-8")
        (self.ctl / "block_new_session").write_text("", encoding="utf-8")
        stale_id = bridge.send("requestMessages", {})
        bridge.wait_ack(stale_id, timeout=10.0)
        self.wait_fake_input(lambda e: e.get("type") == "get_messages")
        # Bump generation while the stale reply is parked.
        new_id = bridge.send("newSession", {})
        new_ack = bridge.wait_ack(new_id, timeout=10.0)
        self.assertEqual(new_ack.get("accepted"), True)
        self.wait_fake_input(lambda e: e.get("type") == "new_session")
        # Current payload is empty; stale payload stays STALE.
        (self.ctl / "messages_payload.json").write_text("[]", encoding="utf-8")
        # Abort before the new_session ack: gates must be retained, not failed.
        # The ack update itself already carries the retained gates (no further
        # async update arrives while the fake is parked), so assert on it.
        abort_id = bridge.send("abort", {})
        abort_ack = bridge.wait_ack(abort_id, timeout=10.0)
        self.assertEqual(abort_ack.get("accepted"), True)
        cstate = abort_ack["state"]
        self.assertTrue(cstate["sessionRefreshPending"])
        self.assertTrue(cstate["sessionChangeInFlight"])
        self.assertEqual(cstate["sessionChangeKind"], "new")
        self.assertTrue(cstate["sessionChangeCancelRequested"])
        # Prompts stay gated while the refresh is pending.
        gated = bridge.send("prompt", {"message": "gated?"})
        gated_ack = bridge.wait_ack(gated, timeout=10.0)
        self.assertEqual(gated_ack.get("accepted"), False)
        # Release both parked replies: stale first, then the authoritative one.
        try:
            (self.ctl / "block_get_messages").unlink()
        except FileNotFoundError:
            pass
        # Give the stale reply a moment to land and be ignored.
        time.sleep(1.0)
        bridge.drain()
        stale_texts = [
            m.get("text") for u in bridge.updates for m in u.get("state", {}).get("messages", []) or []
        ]
        self.assertNotIn("STALE", stale_texts, "stale history must never be applied")
        try:
            (self.ctl / "block_new_session").unlink()
        except FileNotFoundError:
            pass
        recovered = bridge.wait_state(lambda s, u: s.get("ready") is True, timeout=15.0)
        self.assertFalse(recovered["state"]["sessionRefreshPending"])
        self.assertTrue(recovered["state"]["sessionFile"])

        # Restore (switchSession) abort before picker ack keeps the same gates.
        (self.ctl / "block_picker").write_text("", encoding="utf-8")
        switch_id = bridge.send("switchSession", {})
        switch_ack = bridge.wait_ack(switch_id, timeout=10.0)
        self.assertEqual(switch_ack.get("accepted"), True)
        self.wait_fake_input(lambda e: e.get("type") == "prompt" and e.get("message") == "/desktop-sessions")
        abort2 = bridge.send("abort", {})
        abort2_ack = bridge.wait_ack(abort2, timeout=10.0)
        self.assertTrue(abort2_ack["state"]["sessionRefreshPending"])
        self.assertTrue(abort2_ack["state"]["sessionChangeInFlight"])
        try:
            (self.ctl / "block_picker").unlink()
        except FileNotFoundError:
            pass
        # Picker cancellation resolves via the abort path; an explicit failed
        # event or a return to ready both prove no prompt was replayed.
        bridge.wait_state(
            lambda s, u: s.get("ready") is True or any(
                e.get("name") == "failed" for e in u.get("events", []) or []
            ),
            timeout=15.0,
        )
        sid = bridge.send("shutdown", {})
        bridge.wait_ack(sid, timeout=5.0)
        bridge.wait_exit(timeout=8.0)

    # -- 5: approvals ------------------------------------------------------
    def test_approval_passthrough_expiry_and_unknown_duplicate(self):
        bridge = self.make_bridge(mode="project")
        self.start_and_wait_ready(bridge)

        # Exact passthrough: fields survive verbatim with type/id added.
        aid = bridge.send("prompt", {"message": "please approve-me now"})
        bridge.wait_ack(aid, timeout=10.0)
        approval = bridge.wait_state(
            lambda s, u: s.get("pendingApproval") is not None
            and (s.get("pendingApproval") or {}).get("id") == "appr-1",
            timeout=10.0,
        )
        self.assertEqual(approval["state"]["pendingApproval"]["method"], "confirm")
        fields = {"confirmed": False, "note": "exact \u20ac pass"}
        rid = bridge.send("respond", {"requestId": "appr-1", "fields": fields})
        rack = bridge.wait_ack(rid, timeout=10.0)
        self.assertEqual(rack.get("accepted"), True)
        delivered = self.wait_fake_input(
            lambda e: e.get("type") == "extension_ui_response" and e.get("id") == "appr-1",
            timeout=10.0,
        )
        self.assertEqual(delivered.get("confirmed"), False)
        self.assertEqual(delivered.get("note"), "exact \u20ac pass")
        cleared = bridge.wait_state(lambda s, u: s.get("pendingApproval") is None, timeout=10.0)
        self.assertIsNone(cleared["state"]["pendingApproval"])
        count_after_first = len(self.fake_inputs())

        # Duplicate and unknown replies never write and never restart.
        dup = bridge.send("respond", {"requestId": "appr-1", "fields": fields})
        dup_ack = bridge.wait_ack(dup, timeout=10.0)
        self.assertEqual(dup_ack.get("accepted"), False)
        unknown = bridge.send("respond", {"requestId": "no-such-id", "fields": {}})
        unknown_ack = bridge.wait_ack(unknown, timeout=10.0)
        self.assertEqual(unknown_ack.get("accepted"), False)
        time.sleep(1.0)
        self.assertEqual(len(self.fake_inputs()), count_after_first)
        self.assertIsNone(bridge.proc.poll(), "unknown/duplicate respond must not restart")
        self.assertTrue(bridge.updates[-1]["state"]["processStarted"])

        # Expiry: short-timeout approval is cancelled exactly once, bounded.
        eid = bridge.send("prompt", {"message": "please expire-me now"})
        bridge.wait_ack(eid, timeout=10.0)
        bridge.wait_state(
            lambda s, u: (s.get("pendingApproval") or {}).get("id") == "exp-1",
            timeout=10.0,
        )
        expired = self.wait_fake_input(
            lambda e: e.get("type") == "extension_ui_response" and e.get("id") == "exp-1"
            and e.get("cancelled") is True,
            timeout=8.0,
        )
        self.assertTrue(expired.get("cancelled"))
        bridge.wait_state(lambda s, u: s.get("pendingApproval") is None, timeout=8.0)
        # Late denial after expiry is a duplicate: rejected, no second write.
        before_late = len(self.fake_inputs())
        late = bridge.send("respond", {"requestId": "exp-1", "fields": {"confirmed": True}})
        late_ack = bridge.wait_ack(late, timeout=10.0)
        self.assertEqual(late_ack.get("accepted"), False)
        time.sleep(0.5)
        self.assertEqual(len(self.fake_inputs()), before_late)
        sid = bridge.send("shutdown", {})
        bridge.wait_ack(sid, timeout=5.0)
        bridge.wait_exit(timeout=8.0)

    def test_surface_and_defer_round_robin_end_to_end(self):
        # S-048 over the real binary: surfaceRequest marks a request seen
        # (no silent expiry); deferRequest parks it round-robin with a
        # truthful deferredCount; a surfaced short-timeout request never
        # expires; unknown ids refuse without side effects.
        bridge = self.make_bridge(mode="project")
        self.start_and_wait_ready(bridge)

        aid = bridge.send("prompt", {"message": "please approve-me now"})
        bridge.wait_ack(aid, timeout=10.0)
        bridge.wait_state(
            lambda s, u: (s.get("pendingApproval") or {}).get("id") == "appr-1",
            timeout=10.0,
        )
        bid = bridge.send("prompt", {"message": "please approve-two now"})
        bridge.wait_ack(bid, timeout=10.0)
        both = bridge.wait_state(
            lambda s, u: len(s.get("pendingRequests") or {}) == 2,
            timeout=10.0,
        )
        self.assertIn("deferredCount", both["state"])
        self.assertEqual(both["state"]["deferredCount"], 0)

        unk = bridge.send("surfaceRequest", {"requestId": "no-such-id"})
        self.assertEqual(bridge.wait_ack(unk, timeout=10.0).get("accepted"), False)
        unk2 = bridge.send("deferRequest", {"requestId": "no-such-id"})
        self.assertEqual(bridge.wait_ack(unk2, timeout=10.0).get("accepted"), False)

        sid = bridge.send("surfaceRequest", {"requestId": "appr-1"})
        sack = bridge.wait_ack(sid, timeout=10.0)
        self.assertEqual(sack.get("accepted"), True)
        # Surfacing alone never moves the display.
        self.assertEqual((sack["state"].get("pendingApproval") or {}).get("id"), "appr-1")

        did = bridge.send("deferRequest", {"requestId": "appr-1"})
        dack = bridge.wait_ack(did, timeout=10.0)
        self.assertEqual(dack.get("accepted"), True)
        # The ack update already carries the parked round-robin state.
        self.assertEqual((dack["state"].get("pendingApproval") or {}).get("id"), "appr-2")
        self.assertEqual(dack["state"]["deferredCount"], 1)

        rid = bridge.send("respond", {"requestId": "appr-2", "fields": {"confirmed": True}})
        rack = bridge.wait_ack(rid, timeout=10.0)
        self.assertEqual(rack.get("accepted"), True)
        # Answering appr-2 starts a new round: appr-1 returns, flags cleared.
        self.assertEqual((rack["state"].get("pendingApproval") or {}).get("id"), "appr-1")
        self.assertEqual(rack["state"]["deferredCount"], 0)

        rid1 = bridge.send("respond", {"requestId": "appr-1", "fields": {"confirmed": True}})
        rack1 = bridge.wait_ack(rid1, timeout=10.0)
        self.assertEqual(rack1.get("accepted"), True)
        self.assertIsNone(rack1["state"].get("pendingApproval"))

        # A surfaced short-timeout request never expires silently.
        eid = bridge.send("prompt", {"message": "please expire-me now"})
        bridge.wait_ack(eid, timeout=10.0)
        bridge.wait_state(
            lambda s, u: (s.get("pendingApproval") or {}).get("id") == "exp-1",
            timeout=10.0,
        )
        esid = bridge.send("surfaceRequest", {"requestId": "exp-1"})
        self.assertEqual(bridge.wait_ack(esid, timeout=10.0).get("accepted"), True)
        count_before = len(self.fake_inputs())
        time.sleep(3.0)
        survivors = [
            e for e in self.fake_inputs()[count_before:]
            if e.get("type") == "extension_ui_response" and e.get("id") == "exp-1"
        ]
        self.assertEqual(survivors, [])
        # Still queued afterwards: re-surfacing is idempotent and the
        # snapshot still shows exp-1 with nothing cancelled.
        esid2 = bridge.send("surfaceRequest", {"requestId": "exp-1"})
        esack2 = bridge.wait_ack(esid2, timeout=10.0)
        self.assertEqual(esack2.get("accepted"), True)
        self.assertEqual((esack2["state"].get("pendingApproval") or {}).get("id"), "exp-1")
        sid = bridge.send("shutdown", {})
        bridge.wait_ack(sid, timeout=5.0)
        bridge.wait_exit(timeout=8.0)

    # -- 6: malformed UI framing -------------------------------------------
    def test_malformed_framing_rejected_without_side_effects(self):
        bridge = self.make_bridge(mode="project")
        # Garbage line without an id: failed event, no ack, child not started.
        bridge.send_raw("not json at all\n")
        bad = bridge.wait_event("failed", timeout=10.0)
        self.assertTrue(any("Invalid UI command" in str(e.get("args", "")) for e in bad.get("events", [])))
        self.assertIsNone(bad.get("ack"))
        # Unknown op is rejected with its ack.
        unk = bridge.send("bogusOp", {})
        unk_ack = bridge.wait_ack(unk, timeout=10.0)
        self.assertEqual(unk_ack.get("accepted"), False)
        self.assertTrue(any(e.get("name") == "failed" for e in unk_ack.get("events", [])))
        # Wrong protocol version is rejected.
        bridge.send_raw(json.dumps({"version": 2, "id": "ver-1", "op": "start", "args": {}}) + "\n")
        ver = bridge.wait_event("failed", timeout=10.0)
        self.assertEqual(ver.get("ack"), "ver-1")
        self.assertEqual(ver.get("accepted"), False)
        # Oversize line is rejected with its id prefix and performs no writes.
        # Fixed framer: 4MiB+100 must ack promptly with its own id.
        big_id = f"big-{uuid.uuid4().hex[:6]}"
        huge = json.dumps({"version": 1, "id": big_id, "op": "prompt",
                           "args": {"message": "x" * (4 * 1024 * 1024 + 100)}}) + "\n"
        bridge.send_raw(huge)
        over = bridge.wait_ack(big_id, timeout=10.0)
        self.assertEqual(over.get("accepted"), False)
        self.assertTrue(any("4 MiB" in str(e.get("args", "")) for e in over.get("events", [])))
        self.assertFalse(self.fake_log.exists() and self.fake_log.read_text(encoding="utf-8").strip(),
                         "oversize input must not reach pi")
        # The bridge is still usable after framing errors.
        self.start_and_wait_ready(bridge)
        sid = bridge.send("shutdown", {})
        bridge.wait_ack(sid, timeout=5.0)
        bridge.wait_exit(timeout=8.0)

    # -- 6b: framing — coalesced / fragmented / oversize-then-valid ---------
    def test_framing_coalesced_fragmented_oversize_then_valid(self):
        bridge = self.make_bridge(mode="project")
        # Three commands coalesced in a single write must all surface in order.
        ids = [f"coal-{i}-{uuid.uuid4().hex[:4]}" for i in range(3)]
        blob = "".join(
            json.dumps({"version": 1, "id": ident, "op": "bogusOp", "args": {}}) + "\n"
            for ident in ids
        )
        bridge.send_raw(blob)
        for ident in ids:
            ack = bridge.wait_ack(ident, timeout=10.0)
            self.assertEqual(ack.get("accepted"), False)
        # One command fragmented across writes (including a split inside the
        # id string) must reassemble and ack with the proper id.
        frag_id = f"frag-{uuid.uuid4().hex[:6]}"
        line = json.dumps({"version": 1, "id": frag_id, "op": "bogusOp", "args": {}}) + "\n"
        cut = len(line) // 2
        assert bridge.proc.stdin is not None
        bridge.proc.stdin.write(line[:cut])
        bridge.proc.stdin.flush()
        time.sleep(0.2)
        bridge.proc.stdin.write(line[cut:])
        bridge.proc.stdin.flush()
        frag_ack = bridge.wait_ack(frag_id, timeout=10.0)
        self.assertEqual(frag_ack.get("accepted"), False)
        # 4MiB+100 oversize followed immediately by a valid command: both must
        # ack promptly with their own ids (old chunk reader lost the trailing
        # newline and hung until the next input).
        big_id = f"big-{uuid.uuid4().hex[:6]}"
        huge = json.dumps({"version": 1, "id": big_id, "op": "prompt",
                           "args": {"message": "y" * (4 * 1024 * 1024 + 100)}}) + "\n"
        valid_id = f"valid-{uuid.uuid4().hex[:6]}"
        valid = json.dumps({"version": 1, "id": valid_id, "op": "bogusOp", "args": {}}) + "\n"
        bridge.send_raw(huge + valid)
        over = bridge.wait_ack(big_id, timeout=12.0)
        self.assertEqual(over.get("accepted"), False)
        self.assertTrue(any("4 MiB" in str(e.get("args", "")) for e in over.get("events", [])))
        valid_ack = bridge.wait_ack(valid_id, timeout=10.0)
        self.assertEqual(valid_ack.get("accepted"), False)
        self.assertTrue(any(e.get("name") == "failed" for e in valid_ack.get("events", [])))
        self.assertFalse(self.fake_log.exists() and self.fake_log.read_text(encoding="utf-8").strip())
        sid = bridge.send("shutdown", {})
        bridge.wait_ack(sid, timeout=5.0)
        bridge.wait_exit(timeout=8.0)

    # -- 6c: settled-before-ACK and old/foreign control never release gate --
    def test_settled_and_old_state_do_not_release_new_restore_gate(self):
        bridge = self.make_bridge(mode="project")
        _, ready = self.start_and_wait_ready(bridge)
        first_session = ready["state"]["sessionFile"]
        self.assertTrue(first_session)
        # New-session gate: settled + old/foreign/same-id-mismatch frames
        # before the matching ACK must all retain gates WITHOUT consuming the
        # genuine correlation (state worker peeks before removing).
        (self.ctl / "block_new_session").write_text("", encoding="utf-8")
        new_id = bridge.send("newSession", {})
        new_ack = bridge.wait_ack(new_id, timeout=10.0)
        self.assertEqual(new_ack.get("accepted"), True)
        # Ack itself carries the gate (no async update lands while parked).
        self.assertTrue(new_ack["state"]["sessionRefreshPending"])
        self.assertTrue(new_ack["state"]["sessionChangeInFlight"])
        self.assertEqual(new_ack["state"]["sessionChangeKind"], "new")
        for trigger in ("emit_settled_once", "emit_old_state_once",
                        "emit_foreign_new_session_once",
                        "emit_mismatched_new_once"):
            (self.ctl / trigger).write_text("", encoding="utf-8")
            time.sleep(1.0)
            bridge.drain()
            st = bridge.updates[-1]["state"]
            self.assertTrue(st["sessionRefreshPending"], trigger)
            self.assertTrue(st["sessionChangeInFlight"], trigger)
            self.assertNotEqual(st.get("sessionFile"), "/tmp/evil.jsonl", trigger)
            self.assertFalse(st.get("ready"), trigger)
        try:
            (self.ctl / "block_new_session").unlink()
        except FileNotFoundError:
            pass
        recovered = bridge.wait_state(lambda s, u: s.get("ready") is True, timeout=15.0)
        self.assertNotEqual(recovered["state"]["sessionFile"], "/tmp/evil.jsonl")
        self.assertTrue(recovered["state"]["sessionFile"])
        # Restore gate: same pin with the picker prompt in flight.
        (self.ctl / "block_picker").write_text("", encoding="utf-8")
        switch_id = bridge.send("switchSession", {})
        switch_ack = bridge.wait_ack(switch_id, timeout=10.0)
        self.assertEqual(switch_ack.get("accepted"), True)
        self.assertTrue(switch_ack["state"]["sessionRefreshPending"])
        self.assertEqual(switch_ack["state"]["sessionChangeKind"], "restore")
        for trigger in ("emit_settled_once", "emit_old_state_once",
                        "emit_foreign_new_session_once",
                        "emit_mismatched_picker_once"):
            (self.ctl / trigger).write_text("", encoding="utf-8")
            time.sleep(1.0)
            bridge.drain()
            st = bridge.updates[-1]["state"]
            self.assertTrue(st["sessionRefreshPending"], f"restore:{trigger}")
            self.assertNotEqual(st.get("sessionFile"), "/tmp/evil.jsonl", trigger)
        try:
            (self.ctl / "block_picker").unlink()
        except FileNotFoundError:
            pass
        bridge.wait_state(
            lambda s, u: s.get("ready") is True or any(
                e.get("name") == "failed" for e in u.get("events", []) or []),
            timeout=15.0,
        )
        sid = bridge.send("shutdown", {})
        bridge.wait_ack(sid, timeout=5.0)
        bridge.wait_exit(timeout=8.0)

    # -- 7: child death + idle pause/resume + new-empty --------------------
    def test_child_death_no_replay_and_idle_resume_cached_new_empty(self):
        bridge = self.make_bridge(mode="project")
        _, ready = self.start_and_wait_ready(bridge)
        first_session = ready["state"]["sessionFile"]
        self.assertTrue(first_session)
        first_launches = len(self.fake_launches())

        # Abrupt child death reports an error and never replays the prompt.
        die = bridge.send("prompt", {"message": "trigger __die__ now"})
        die_ack = bridge.wait_ack(die, timeout=10.0)
        self.assertEqual(die_ack.get("accepted"), True)
        died = bridge.wait_event("failed", timeout=10.0)
        texts = " ".join(str(e.get("args", "")) for e in died.get("events", []))
        self.assertIn("pi interrupted", texts)
        self.assertIn("no prompt was replayed", texts)
        dead_state = died["state"]
        self.assertFalse(dead_state["processStarted"])
        self.assertFalse(dead_state["desiredRunning"])
        prompts_before = [e for e in self.fake_inputs() if e.get("type") == "prompt"]
        time.sleep(1.0)
        prompts_after = [e for e in self.fake_inputs() if e.get("type") == "prompt"]
        self.assertEqual(len(prompts_after), len(prompts_before),
                         "dead child must not receive a replayed prompt")
        self.assertEqual(len(self.fake_launches()), first_launches, "death must not auto-restart")

        # Explicit start recovers and reuses the validated cached session.
        self.start_and_wait_ready(bridge)
        relaunches = self.fake_launches()
        self.assertGreater(len(relaunches), first_launches)
        resumed_argv = relaunches[-1]["argv"]
        self.assertIn("--session", resumed_argv)
        self.assertIn(first_session, resumed_argv)

        # Idle pause preserves the session without running pi.
        stop = bridge.send("stopIdle", {})
        stop_ack = bridge.wait_ack(stop, timeout=10.0)
        self.assertEqual(stop_ack.get("accepted"), True)
        paused = bridge.wait_state(
            lambda s, u: s.get("idleStopped") is True and s.get("processStarted") is False,
            timeout=10.0,
        )
        self.assertIn("Paused", paused["state"]["status"])
        cached = paused["state"]["sessionFile"]
        self.assertTrue(cached)
        launches_before_resume = len(self.fake_launches())
        # Busy-gated stopIdle would be rejected; here idle stop was accepted once.
        # Resume reuses the same validated cache file.
        resume = bridge.send("start", {})
        bridge.wait_ack(resume, timeout=10.0)
        bridge.wait_state(lambda s, u: s.get("ready") is True, timeout=15.0)
        self.assertGreater(len(self.fake_launches()), launches_before_resume)
        self.assertIn(cached, self.fake_launches()[-1]["argv"])

        # New session installs a fresh empty identity.
        new_id = bridge.send("newSession", {})
        bridge.wait_ack(new_id, timeout=10.0)
        fresh = bridge.wait_state(
            lambda s, u: s.get("ready") is True and s.get("sessionFile") not in ("", cached),
            timeout=15.0,
        )
        new_file = fresh["state"]["sessionFile"]
        self.assertNotEqual(new_file, cached)
        self.assertEqual(fresh["state"]["messages"], [])

        # Deleting the cached file makes the next resume start empty (no --session).
        bridge2_stop = bridge.send("stopIdle", {})
        bridge.wait_ack(bridge2_stop, timeout=10.0)
        bridge.wait_state(lambda s, u: s.get("idleStopped") is True, timeout=10.0)
        try:
            Path(new_file).unlink()
        except FileNotFoundError:
            pass
        # Bridge still holds the path; drop it by forcing a fresh start is not
        # possible via ops, but a resume with a missing file must not pass a
        # stale --session: the wrapper treats it as a cache miss. Emulate the
        # not-yet-flushed empty case by starting with a missing pending file is
        # covered by the wrapper contract; here assert resume still succeeds.
        restart = bridge.send("start", {})
        bridge.wait_ack(restart, timeout=10.0)
        bridge.wait_state(lambda s, u: s.get("ready") is True, timeout=15.0)
        sid = bridge.send("shutdown", {})
        bridge.wait_ack(sid, timeout=5.0)
        bridge.wait_exit(timeout=8.0)

    # -- 8: EOF / SIGTERM kill the child group, bounded --------------------
    def test_eof_and_sigterm_kill_child_group_bounded(self):
        # EOF path.
        bridge = self.make_bridge(mode="project", extra_env={"FAKE_SPAWN_SLEEP": "1"})
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
        elapsed = time.time() - start
        self.assertLess(elapsed, 8.0, "EOF shutdown must be bounded")
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
                # Reap check: a zombie still has a /proc entry but is dead.
                try:
                    status = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()
                    self.assertEqual(status[0], "Z", f"pid {pid} survived EOF group kill")
                except FileNotFoundError:
                    pass
                else:
                    self.fail(f"pid {pid} survived EOF group kill")

        # SIGTERM path.
        (self.ctl / "sleep_pid").unlink(missing_ok=True)
        bridge2 = self.make_bridge(mode="project", extra_env={"FAKE_SPAWN_SLEEP": "1"})
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


    # -- 8b: TERM-resistant descendant cleaned even after leader exit -----
    def test_resistant_descendant_cleaned_on_eof_and_term(self):
        for phase, action in (("eof", "close"), ("term", "sigterm")):
            with self.subTest(phase=phase):
                (self.ctl / "resistant_armed").unlink(missing_ok=True)
                bridge = self.make_bridge(mode="project", extra_env={"FAKE_RESISTANT": "1"})
                self.start_and_wait_ready(bridge)
                deadline = time.time() + 10.0
                while time.time() < deadline:
                    if (self.ctl / "resistant_armed").exists():
                        break
                    time.sleep(0.1)
                self.assertTrue((self.ctl / "resistant_armed").exists())
                pgid = int(self.fake_launches()[-1]["pid"])
                # Resistant sleep keeps the group alive after the leader would
                # exit: signal-0 probe must succeed before teardown.
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    self.fail("resistant group did not outlive spawn")
                start = time.time()
                if action == "close":
                    bridge.close_stdin()
                else:
                    bridge.proc.send_signal(signal.SIGTERM)
                bridge.wait_exit(timeout=10.0)
                self.assertLess(time.time() - start, 10.0)
                # Poll for confirmed group death (TERM then finite KILL wait).
                dead_by = time.time() + 8.0
                alive = True
                while time.time() < dead_by:
                    try:
                        os.killpg(pgid, 0)
                    except ProcessLookupError:
                        alive = False
                        break
                    except PermissionError:
                        break
                    time.sleep(0.1)
                self.assertFalse(alive, f"resistant pgid {pgid} survived {phase}")
                (self.ctl / "resistant_armed").unlink(missing_ok=True)

    # -- 8b2: TERM-resistant direct child cleaned bounded (KILL escalation)
    def test_resistant_direct_child_cleaned_bounded(self):
        for phase, action in (("eof", "close"), ("term", "sigterm")):
            with self.subTest(phase=phase):
                bridge = self.make_bridge(mode="project", extra_env={"FAKE_IGNORE_TERM": "1"})
                self.start_and_wait_ready(bridge)
                self._record_owned()
                pgid = int(self.fake_launches()[-1]["pid"])
                self.assertIn(pgid, self.owned_pgids)
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    self.fail("resistant direct child group did not outlive spawn")
                start = time.time()
                if action == "close":
                    bridge.close_stdin()
                else:
                    bridge.proc.send_signal(signal.SIGTERM)
                bridge.wait_exit(timeout=10.0)
                self.assertLess(time.time() - start, 10.0)
                dead_by = time.time() + 8.0
                alive = True
                while time.time() < dead_by:
                    try:
                        os.killpg(pgid, 0)
                    except ProcessLookupError:
                        alive = False
                        break
                    except PermissionError:
                        break
                    time.sleep(0.1)
                self.assertFalse(alive, f"TERM-ignoring child pgid {pgid} survived {phase}")
                self._record_owned()

    # -- 8b3: UI ACK saturation (bounded burst, no Pi overlap) ---------------
    def test_ui_ack_saturation_bounded(self):
        # Pure UI path (no Pi writes): burst of coalesced commands must all
        # ack in order without hanging the state loop. Sized under the bounded
        # main-queue cap so fail-closed termination is not expected.
        bridge = self.make_bridge(mode="project")
        count = 40
        ids = [f"sat-{i}-{uuid.uuid4().hex[:4]}" for i in range(count)]
        blob = "".join(
            json.dumps({"version": 1, "id": ident, "op": "bogusOp", "args": {}}) + "\n"
            for ident in ids
        )
        start = time.time()
        bridge.send_raw(blob)
        for ident in ids:
            ack = bridge.wait_ack(ident, timeout=15.0)
            self.assertEqual(ack.get("accepted"), False)
        self.assertLess(time.time() - start, 15.0, "ACK saturation hung")
        # Still usable afterwards.
        self.start_and_wait_ready(bridge)
        sid = bridge.send("shutdown", {})
        bridge.wait_ack(sid, timeout=5.0)
        bridge.wait_exit(timeout=8.0)

    # -- 8c: wedged transport still shuts down bounded (fail-closed) --------
    def test_wedged_transport_shutdown_bounded(self):
        # Phase 1 (hog-stdout) is fail-closed TERMINAL, not a clean shutdown.
        # The fake emits 2MiB of unterminated stdout, then the real JSON reply
        # on the same line, so the frame is malformed ("zzz...{json}\n").
        # The bridge must latch transport failure, emit an explicit terminal
        # failure, never replay, reap the child group, and exit on its own.
        # Writing shutdown to the already-dead peer would raise BrokenPipe, so
        # this phase asserts the terminal update + bounded exit instead of
        # sending shutdown. No pipe/timeout error is swallowed: a missing
        # terminal update or a lingering process fails explicitly below.
        bridge = self.make_bridge(mode="project",
                                  extra_env={"FAKE_HOG_STDOUT_BYTES": str(2 * 1024 * 1024)})
        ident = bridge.send("start", {})
        bridge.wait_ack(ident, timeout=10.0)
        terminal = None
        deadline = time.time() + 15.0
        while time.time() < deadline:
            remaining = deadline - time.time()
            try:
                update = bridge.recv(timeout=remaining)
            except TimeoutError:
                break
            for event in update.get("events", []) or []:
                if event.get("name") == "failed" and "Invalid pi RPC output" in str(event.get("args", "")):
                    terminal = update
                    break
            if terminal is not None:
                break
        self.assertIsNotNone(
            terminal,
            "hog-stdout must surface 'Invalid pi RPC output' fail-closed failure, not hang",
        )
        assert terminal is not None
        terminal_text = " ".join(str(e.get("args", "")) for e in terminal.get("events", []) or [])
        self.assertIn("Invalid pi RPC output", terminal_text)
        self.assertIn("session terminated without replay", terminal_text)
        # No replay and no restart while fail-closed.
        time.sleep(0.5)
        bridge.drain()
        self.assertEqual(
            [e for e in self.fake_inputs() if e.get("type") == "prompt"], [],
            "malformed stdout must not replay prompts",
        )
        self.assertEqual(len(self.fake_launches()), 1, "fail-closed must not auto-restart")
        # Already terminal: bounded exit without sending shutdown to the dead
        # peer. wait_exit raises TimeoutError on a hang (explicit, not
        # swallowed); poll proves exit vs arbitrary crash combined with the
        # terminal message asserted above.
        fake_pid = int(self.fake_launches()[-1]["pid"])
        start = time.time()
        bridge.wait_exit(timeout=10.0)
        self.assertLess(time.time() - start, 10.0, "hog-stdout fail-closed exit hung")
        self.assertIsNotNone(bridge.proc.poll(), "bridge must have exited fail-closed")
        # Child group reaped: group probe must go away bounded, and the direct
        # pid must be gone (a zombie /proc entry still counts as reaped here
        # only if the kernel has it as Z; anything else is a survivor).
        dead_by = time.time() + 5.0
        alive = True
        while time.time() < dead_by:
            try:
                os.killpg(fake_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            except PermissionError:
                break
            time.sleep(0.1)
        self.assertFalse(alive, f"hog-stdout pgid {fake_pid} survived fail-closed teardown")
        try:
            os.kill(fake_pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
        else:
            try:
                status = Path(f"/proc/{fake_pid}/stat").read_text().rsplit(")", 1)[-1].split()
                self.assertEqual(status[0], "Z", f"pid {fake_pid} survived hog-stdout group kill")
            except FileNotFoundError:
                pass
        # Long unterminated stderr line: diagnostics only, session stays live
        # so an explicit shutdown must ack and exit bounded.
        bridge2 = self.make_bridge(mode="project",
                                   extra_env={"FAKE_HOG_STDERR_BYTES": str(2 * 1024 * 1024)})
        ident2 = bridge2.send("start", {})
        bridge2.wait_ack(ident2, timeout=10.0)
        time.sleep(1.0)
        self.assertIsNone(bridge2.proc.poll(), "hog-stderr must keep the bridge alive")
        start = time.time()
        sid2 = bridge2.send("shutdown", {})
        ack2 = bridge2.wait_ack(sid2, timeout=8.0)
        self.assertEqual(ack2.get("accepted"), True)
        bridge2.wait_exit(timeout=10.0)
        self.assertLess(time.time() - start, 10.0, "hog-stderr shutdown hung")
        self.assertIsNotNone(bridge2.proc.poll())
        # Nonreading stdin: writes hit the finite deadline and fail closed.
        bridge3 = self.make_bridge(mode="project", extra_env={"FAKE_NONREADING": "1"})
        ident3 = bridge3.send("start", {})
        bridge3.wait_ack(ident3, timeout=10.0)
        time.sleep(1.0)
        self.assertIsNone(bridge3.proc.poll())
        start = time.time()
        bridge3.close_stdin()
        bridge3.wait_exit(timeout=10.0)
        self.assertLess(time.time() - start, 10.0, "nonreading-stdin EOF hung")


class ScopedAgentAdapterTests(unittest.TestCase):
    """Critical local dispatch order in widgets/ScopedAgent.qml (static)."""

    @classmethod
    def setUpClass(cls):
        cls.source = SCOPED_QML.read_text(encoding="utf-8")

    def test_handle_update_applies_state_before_signals(self):
        apply_at = self.source.index("applyState(value.state)")
        text_at = self.source.index('if (event.name === "textDelta")')
        self.assertLess(apply_at, text_at)
        ack_at = self.source.index("if (value.ack && _pendingAcks[value.ack])")
        self.assertLess(apply_at, ack_at)

    def test_queued_ops_flush_in_order_on_started(self):
        started = self.source[self.source.index("onStarted:"):self.source.index("onRunningChanged:")]
        self.assertIn("_queuedOps", started)
        self.assertIn("for (let item of queue)", started)
        queue_copy = started.index("let queue = root._queuedOps")
        clear = started.index("root._queuedOps = []")
        loop = started.index("for (let item of queue)")
        self.assertLess(queue_copy, clear)
        self.assertLess(clear, loop)

    def test_pending_gates_clear_only_on_ack(self):
        self.assertIn("function trackPending(op)", self.source)
        self.assertIn("function clearPending(id)", self.source)
        handle = self.source[self.source.index("function handleUpdate"):self.source.index("Component.onCompleted:")]
        self.assertIn("clearPending(value.ack)", handle)
        self.assertIn("opFinished(value.ack, op, accepted, message)", handle)
        self.assertIn("ready: _bReady && _pendingCount === 0", self.source)

    def test_lazy_start_only_spawner_no_auto_sendop(self):
        # Refactored contract: lazy launch, only start() spawns, sendOp never
        # auto-spawns, death never replays (opFinished false + bridgeDead).
        completed = self.source[self.source.index("Component.onCompleted:"):self.source.index("Component.onDestruction:")]
        self.assertIn("LAZY launch", completed)
        self.assertNotIn("bridgeProc.running = true", completed)
        sendop = self.source[self.source.index("function sendOp"):self.source.index("function start()")]
        self.assertIn("if (!bridgeProc.running) return", sendop)
        self.assertNotIn("bridgeProc.running = true", sendop)
        start = self.source[self.source.index("function start()"):self.source.index("function stopIdle()")]
        self.assertIn("The ONLY explicit spawner", start)
        self.assertIn("refreshSpawnIdentity()", start)
        self.assertIn("bridgeProc.running = true", start)
        self.assertIn("signal opFinished(string id, string op, bool accepted, string message)", self.source)
        self.assertIn("signal bridgeDead()", self.source)
        self.assertIn("dropPendingNoReplay()", self.source)
        self.assertIn("handleBridgeDead()", self.source)
        self.assertIn("handleFailedToStart()", self.source)


class ScopedCapabilityMatrixTests(unittest.TestCase):
    """Phase 1 §2.2: static matrix pins from the scoped (project/journal) side.

    No bridge/pi is launched: these assert allow/deny per scope directly
    on the extension source plus the §8.2 invariants that must not break.
    """

    EXTENSION = (REPO_ROOT / ".pi" / "extensions" / "desktop-agent.ts").read_text(encoding="utf-8")

    def test_project_mode_permits_agenda_denies_journal_tools(self):
        allow = self.EXTENSION[self.EXTENSION.index("if (projectMode() && !["):]
        allow = allow[:allow.index("].includes")]
        for name in ("logseq_agenda_list", "logseq_agenda_add",
                     "logseq_project_read", "logseq_project_update",
                     "project_folder_write"):
            self.assertIn(f'"{name}"', allow)
        self.assertNotIn('"logseq_journal_context"', allow)
        self.assertNotIn('"logseq_journal_append"', allow)

    def test_journal_mode_permits_only_journal_tools_plus_reads(self):
        gate = 'if (journalMode() && !["logseq_journal_context", "logseq_journal_append"].includes(event.toolName))'
        self.assertIn(gate, self.EXTENSION)
        # Desktop reads stay available as an exception in every scope.
        marker = 'if ((DESKTOP_READ_TOOLS as string[]).includes(event.toolName)) return;'
        self.assertIn(marker, self.EXTENSION)

    def test_agenda_guards_deny_journal_only(self):
        self.assertNotIn("agenda list is palette-only", self.EXTENSION)
        self.assertNotIn("agenda add is palette-only", self.EXTENSION)
        self.assertIn('if (journalMode()) throw new Error("agenda list is unavailable in journal mode")',
                      self.EXTENSION)
        self.assertIn('if (journalMode()) throw new Error("agenda add is unavailable in journal mode")',
                      self.EXTENSION)

    def test_no_silent_writes_ui_confirm_preserved(self):
        # §8.2: every markdown write stays prepare → exact preview →
        # Confirm → apply with UI approval.
        for marker in ("Approve add to daily todos",
                       "Approve Logseq journal append",
                       "Approve selected project page update",
                       "Approve linked folder file write",
                       "UI confirmation unavailable"):
            self.assertIn(marker, self.EXTENSION)

    def test_untrusted_and_bounded_conventions_preserved(self):
        # §8.2: untrusted-data rule, bounded helpers, sensitive paths.
        self.assertIn("untrusted data", self.EXTENSION)
        self.assertIn("exceeds 1 MiB", self.EXTENSION)
        self.assertIn(".ssh", self.EXTENSION)
        self.assertIn("ScopedAgent.qml", self.EXTENSION)


if __name__ == "__main__":
    unittest.main()
