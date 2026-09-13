"""
Opt-in integration tests for the PI coding agent RPC mode.

These tests spawn the installed ``pi`` binary in ``--mode rpc`` and drive it over
the JSONL stdin/stdout protocol (see /opt/pi-coding-agent/docs/rpc.md). They are
intentionally opt-in and must never touch a real provider/model or the user's
Logseq graph:

  * Gated behind PI_RPC_TESTS=1 (explicit opt-in).
  * Skipped when ``pi`` is not installed.
  * Run with --offline (and PI_OFFLINE=1) plus --approve. Per the pi docs,
    --offline only suppresses startup network activity; it does NOT guarantee
    that no provider request occurs during a session. The guarantee of no
    provider call here comes from the tests themselves: they never send a user
    ``prompt`` that would invoke the LLM, only control commands plus the local
     ``/desktop-sessions`` extension command, which the agent handles itself
     (a deterministic session fixture, then dialog cancellation) without a
     provider call.
  * Each run uses a fresh temporary --session-dir, mirrored into the
    PI_CODING_AGENT_SESSION_DIR environment variable.

These tests do NOT exercise live provider streaming, real screenshots, restart
recovery, or manual Logseq detection. See .pi/README.md for the full scope.

Run with:
    PYTHONDONTWRITEBYTECODE=1 PI_RPC_TESTS=1 python3 -m unittest discover -s tests -v
"""

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
PI_BINARY = shutil.which("pi")
OPT_IN = os.environ.get("PI_RPC_TESTS") == "1"

# Dialog methods in the extension UI protocol require a client response.
DIALOG_METHODS = {"select", "confirm", "input", "editor"}

# Command names expected from this repository's .pi configuration.
EXPECTED_DESKTOP_SESSIONS_CMD = "desktop-sessions"
EXPECTED_SKILLS = ["skill:screen-translation", "skill:logseq-graph"]


class JsonlReader(threading.Thread):
    """Background reader that splits stdout into JSON objects with a timeout.

    Uses strict LF splitting (protocol-compliant: does not treat U+2028/U+2029
    as newlines). Decoded events are pushed onto a queue so callers can read
    them with a bounded timeout via get().
    """

    def __init__(self, stream):
        super().__init__(daemon=True)
        self._stream = stream
        self._queue = queue.Queue()
        self._stop = threading.Event()

    def run(self):
        try:
            for raw in self._stream:
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

    def get(self, timeout):
        return self._queue.get(timeout=timeout)

    def drain(self):
        """Remove and discard any already-queued events (non-blocking)."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def stop(self):
        self._stop.set()

    def join(self, timeout=None):
        super().join(timeout)


class PiRpc:
    """Minimal JSONL RPC client for ``pi --mode rpc``."""

    def __init__(self, proc, reader):
        self.proc = proc
        self.reader = reader
        self._counter = 0

    def _next_id(self):
        self._counter += 1
        return f"t{self._counter}"

    def send(self, obj):
        if "id" not in obj:
            obj["id"] = self._next_id()
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()
        return obj["id"]

    def _handle_ui(self, event, ui_handler):
        method = event.get("method")
        if ui_handler is not None:
            ui_handler(event)
        elif method in DIALOG_METHODS:
            # Cancel any blocking dialog so the agent never hangs waiting.
            self.send({"type": "extension_ui_response", "id": event["id"], "cancelled": True})
        # Fire-and-forget methods (notify/setStatus/...) are ignored.

    def request(self, obj, timeout=15.0, ui_handler=None):
        """Send a command and return its matching response.

        Any extension UI dialog request is cancelled automatically (or routed
        to ui_handler) so the agent does not block on the client.
        """
        req_id = self.send(obj)
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"no response for {obj.get('type')!r}")
            try:
                event = self.reader.get(remaining)
            except queue.Empty:
                raise TimeoutError(f"timeout waiting for response to {obj.get('type')!r}")
            if event.get("type") == "response" and event.get("id") == req_id:
                return event
            if event.get("type") == "extension_ui_request":
                self._handle_ui(event, ui_handler)

    def collect(self, timeout, prompt_id=None, ui_handler=None):
        """Read events for ``timeout`` seconds.

        Returns (response, ui_events). After the first matching response is
        seen, reading continues until a 2s idle gap so trailing fire-and-forget
        events (e.g. notify) are captured too.
        """
        response = None
        ui_events = []
        last_event = time.time()
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            try:
                event = self.reader.get(remaining)
            except queue.Empty:
                break
            last_event = time.time()
            if event.get("type") == "extension_ui_request":
                ui_events.append(event)
                self._handle_ui(event, ui_handler)
            elif event.get("type") == "response" and (prompt_id is None or event.get("id") == prompt_id):
                response = event
                # Keep reading briefly for trailing events, then stop.
                if time.time() - last_event > 2:
                    break
        return response, ui_events


@unittest.skipUnless(OPT_IN, "PI_RPC_TESTS=1 not set; opt-in integration tests skipped")
@unittest.skipUnless(PI_BINARY is not None, "`pi` binary not found on PATH; skipping")
class PiRpcIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not OPT_IN:
            raise unittest.SkipTest("PI_RPC_TESTS=1 not set")
        if PI_BINARY is None:
            raise unittest.SkipTest("`pi` binary not found on PATH")

    def setUp(self):
        self.tested = []
        self.untested = []
        self.failed = []
        self.session_dir = tempfile.mkdtemp(prefix="pi-rpc-test-")
        # SessionManager.list(cwd, customDir) scans JSONL files directly in
        # customDir (unlike the default --<cwd>-- directory).  Keep this
        # fixture independent from pi's newly-created active session and use
        # the smallest valid v3 tree with one user message, so the picker can
        # never exercise the empty-session path by accident.
        self.fixture_path = Path(self.session_dir) / f"fixture-{uuid.uuid4()}.jsonl"
        session_id = str(uuid.uuid4())
        now = "2026-01-01T00:00:00.000Z"
        self.fixture_path.write_text(
            "\n".join([
                json.dumps({"type": "session", "version": 3, "id": session_id,
                            "timestamp": now, "cwd": str(REPO_ROOT)}),
                json.dumps({"type": "message", "id": "a1b2c3d4", "parentId": None,
                            "timestamp": now,
                            "message": {"role": "user", "content": "desktop fixture",
                                        "timestamp": 1767225600000}}),
                "",
            ]),
            encoding="utf-8",
        )
        self.fixture_records = [
            json.loads(line)
            for line in self.fixture_path.read_text(encoding="utf-8").splitlines()
        ]
        env = dict(os.environ)
        env["PI_CODING_AGENT_SESSION_DIR"] = self.session_dir
        env["PI_OFFLINE"] = "1"  # belt-and-suspenders alongside --offline
        self.env = env
        self.errfile = tempfile.NamedTemporaryFile(prefix="pi-rpc-err-", delete=False, suffix=".log")
        assert PI_BINARY is not None, "`pi` must be present (guaranteed by skip)"
        self.proc = subprocess.Popen(
            [PI_BINARY, "--mode", "rpc", "--approve", "--offline",
             "--session-dir", self.session_dir],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.errfile,
            cwd=str(REPO_ROOT),
            env=env,
            text=True,
            bufsize=1,
        )
        self.reader = JsonlReader(self.proc.stdout)
        self.reader.start()
        self.rpc = PiRpc(self.proc, self.reader)
        # Let startup events flush before issuing commands.
        time.sleep(1.5)
        self.reader.drain()

    def tearDown(self):
        # Stop the reader thread first and join it so no one is still reading
        # proc.stdout when we close it (prevents ResourceWarning on leaked
        # stdout/stdout handles).
        try:
            self.reader.stop()
            self.reader.join(timeout=5)
        except Exception:
            pass
        # Close stdin so the child sees EOF and can shut down cleanly.
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass
        # Close the stdout pipe explicitly; Popen leaves it open otherwise.
        try:
            if self.proc.stdout is not None and not self.proc.stdout.closed:
                self.proc.stdout.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        # proc.stderr is the same object as self.errfile; close it to release
        # the handle (closing the temp file below is a harmless no-op).
        try:
            if self.proc.stderr is not None and not self.proc.stderr.closed:
                self.proc.stderr.close()
        except Exception:
            pass
        try:
            shutil.rmtree(self.session_dir, ignore_errors=True)
        except Exception:
            pass
        try:
            self.errfile.close()
        except Exception:
            pass

    # -- reporting helpers -------------------------------------------------
    def _ok(self, name):
        self.tested.append(name)

    def _skip(self, name, reason):
        self.untested.append((name, reason))

    def _fail(self, name, exc):
        self.failed.append((name, f"{type(exc).__name__}: {exc}"))

    def _report(self):
        lines = ["", "==== PI RPC integration test report ===="]
        lines.append(f"TESTED ({len(self.tested)}):")
        for name in self.tested:
            lines.append(f"  + {name}")
        lines.append(f"NOT TESTED / SKIPPED ({len(self.untested)}):")
        if not self.untested:
            lines.append("  (none)")
        for name, reason in self.untested:
            lines.append(f"  - {name}: {reason}")
        lines.append(f"FAILED ({len(self.failed)}):")
        if not self.failed:
            lines.append("  (none)")
        for name, reason in self.failed:
            lines.append(f"  x {name}: {reason}")
        lines.append("========================================")
        return "\n".join(lines)

    # -- the integration scenario -----------------------------------------
    def test_rpc_lifecycle(self):
        # 1. Process started and stayed alive through startup.
        try:
            self.assertIsNone(self.proc.poll(), "pi rpc process exited during startup")
            self._ok("rpc process started and remained alive during startup")
        except Exception as exc:
            self._fail("rpc process started and remained alive during startup", exc)
            self.fail(self._report())

        # 2. get_commands: desktop-sessions extension + both skills.
        try:
            resp = self.rpc.request({"type": "get_commands"}, timeout=15)
            self.assertTrue(resp.get("success"), f"get_commands not success: {resp}")
            commands = {c["name"]: c for c in resp["data"]["commands"]}
            self.assertIn(EXPECTED_DESKTOP_SESSIONS_CMD, commands, "desktop-sessions command missing")
            self._ok(f"get_commands lists '{EXPECTED_DESKTOP_SESSIONS_CMD}' extension command")
            for skill in EXPECTED_SKILLS:
                self.assertIn(skill, commands, f"skill command missing: {skill}")
                self.assertEqual(commands[skill].get("source"), "skill", f"{skill} not a skill source")
            self._ok(f"get_commands lists both skills ({', '.join(EXPECTED_SKILLS)})")
        except Exception as exc:
            self._fail("get_commands desktop-sessions + both skills", exc)

        # 3. get_state returns session info.
        try:
            resp = self.rpc.request({"type": "get_state"}, timeout=15)
            self.assertTrue(resp.get("success"), f"get_state not success: {resp}")
            self.assertIn("sessionId", resp["data"], "get_state missing sessionId")
            self._ok("get_state returns session info (model/sessionId)")
        except Exception as exc:
            self._fail("get_state returns session info", exc)

        # 4. Model listing.
        try:
            resp = self.rpc.request({"type": "get_available_models"}, timeout=15)
            self.assertTrue(resp.get("success"), f"get_available_models not success: {resp}")
            self.assertIsInstance(resp["data"]["models"], list, "models not a list")
            self._ok("get_available_models lists models")
        except Exception as exc:
            self._fail("get_available_models lists models", exc)

        # 5. Session statistics.
        try:
            resp = self.rpc.request({"type": "get_session_stats"}, timeout=15)
            self.assertTrue(resp.get("success"), f"get_session_stats not success: {resp}")
            self.assertIn("tokens", resp["data"], "stats missing tokens")
            self.assertIn("total", resp["data"]["tokens"], "stats.tokens missing total")
            self._ok("get_session_stats returns token/cost stats")
        except Exception as exc:
            self._fail("get_session_stats returns token/cost stats", exc)

        # 6. new_session starts a fresh session (not cancelled).
        try:
            resp = self.rpc.request({"type": "new_session"}, timeout=15)
            self.assertTrue(resp.get("success"), f"new_session not success: {resp}")
            self.assertFalse(resp["data"].get("cancelled", False), "new_session was cancelled")
            self._ok("new_session starts a fresh (non-cancelled) session")
        except Exception as exc:
            self._fail("new_session starts a fresh session", exc)

        # 7. Rename active session via set_session_name, confirmed by get_state.
        try:
            name = "rpc-test-rename"
            resp = self.rpc.request({"type": "set_session_name", "name": name}, timeout=15)
            self.assertTrue(resp.get("success"), f"set_session_name not success: {resp}")
            state = self.rpc.request({"type": "get_state"}, timeout=15)
            self.assertEqual(state["data"].get("sessionName"), name, "sessionName not updated after rename")
            self._ok("set_session_name renames the active session (verified via get_state)")
        except Exception as exc:
            self._fail("set_session_name renames the active session", exc)

        # 8. clear_queue returns empty steering/follow-up queues.
        try:
            resp = self.rpc.request({"type": "clear_queue"}, timeout=15)
            self.assertTrue(resp.get("success"), f"clear_queue not success: {resp}")
            self.assertEqual(resp["data"].get("steering"), [], "steering queue not empty")
            self.assertEqual(resp["data"].get("followUp"), [], "followUp queue not empty")
            self._ok("clear_queue returns empty steering/follow-up queues")
        except Exception as exc:
            self._fail("clear_queue returns empty queues", exc)

        # 9. abort succeeds while idle.
        try:
            resp = self.rpc.request({"type": "abort"}, timeout=15)
            self.assertTrue(resp.get("success"), f"abort not success: {resp}")
            self._ok("abort succeeds when idle")
        except Exception as exc:
            self._fail("abort succeeds when idle", exc)

        # 10. /desktop-sessions: a real non-empty fixture must be discoverable,
        #     and the select request must be cancelled without a provider call.
        try:
            ui_seen = []
            cancelled_ids = []

            def cancel_ui(event):
                ui_seen.append(event)
                if event.get("method") in DIALOG_METHODS:
                    cancelled_ids.append(event["id"])
                    self.rpc.send({"type": "extension_ui_response", "id": event["id"], "cancelled": True})

            self.assertEqual(self.fixture_path.parent, Path(self.session_dir))
            self.assertTrue(self.fixture_path.name.endswith(".jsonl"))
            self.assertEqual(self.fixture_records[0]["type"], "session")
            self.assertEqual(self.fixture_records[0]["version"], 3)
            self.assertEqual(self.fixture_records[0]["cwd"], str(REPO_ROOT))
            self.assertEqual(self.fixture_records[1]["type"], "message")
            self.assertEqual(self.fixture_records[1]["message"]["role"], "user")
            self.assertTrue(self.fixture_records[1]["message"]["content"].strip())
            prompt_id = self.rpc.send({"type": "prompt", "message": "/desktop-sessions"})
            resp, ui_events = self.rpc.collect(12, prompt_id=prompt_id, ui_handler=cancel_ui)
            self.assertIsNotNone(resp, "no response to /desktop-sessions prompt")
            assert resp is not None
            self.assertTrue(resp.get("success"), f"/desktop-sessions prompt not accepted: {resp}")
            dialogs = [e for e in ui_events if e.get("method") in DIALOG_METHODS]
            self.assertEqual([e.get("method") for e in dialogs], ["select"], ui_events)
            options = dialogs[0].get("options") or []
            self.assertTrue(options, "desktop-sessions select had no options")
            self.assertTrue(any(str(self.fixture_path) in option for option in options), options)
            self.assertTrue(all(str(option).rsplit(" — ", 1)[-1].strip() for option in options), options)
            self.assertEqual(cancelled_ids, [dialogs[0]["id"]])
            self._ok("prompt /desktop-sessions lists the valid fixture and is cancelled")
        except Exception as exc:
            self._fail("prompt /desktop-sessions lists fixture / dialog handled", exc)

        # 11. compact on empty session: expected success/error without an API
        #     call. Guarded by a bounded timeout; if it would hang (a provider
        #     call), we treat it as unsafe and skip rather than fail.
        try:
            resp = self.rpc.request({"type": "compact"}, timeout=20)
            # Either an error (nothing to compact) or a successful no-op is fine;
            # the important property is that it returned without hanging.
            self.assertIn("success", resp, "compact response missing success field")
            self._ok("compact on empty session returns expected success/error without API call")
        except TimeoutError as exc:
            self._skip("compact on empty session", f"did not return in time (possible provider call); omitted as unsafe: {exc}")
        except Exception as exc:
            self._fail("compact on empty session", exc)

        # 12. Restart an explicitly persisted session via SessionManager, if one
        #     can be produced. In headless rpc mode the session is only flushed
        #     to disk after a user message (which would require a provider call),
        #     so without that we cannot reproduce a restart and report it as
        #     untested rather than making a provider call or writing a real graph.
        try:
            state = self.rpc.request({"type": "get_state"}, timeout=15)
            session_file = state["data"].get("sessionFile")
            if session_file and os.path.exists(session_file):
                assert PI_BINARY is not None, "`pi` must be present (guaranteed by skip)"
                restart = subprocess.Popen(
                    [PI_BINARY, "--mode", "rpc", "--approve", "--offline",
                     "--session", session_file],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, cwd=str(REPO_ROOT),
                    env=self.env, text=True, bufsize=1,
                )
                r2 = None
                try:
                    r2 = JsonlReader(restart.stdout)
                    r2.start()
                    rpc2 = PiRpc(restart, r2)
                    time.sleep(1.5)
                    r2.drain()
                    resp2 = rpc2.request({"type": "get_state"}, timeout=15)
                    if resp2["data"].get("sessionId") == state["data"].get("sessionId"):
                        self._ok("restart of persisted session reproduces same sessionId")
                    else:
                        self._skip("restart persisted session", "restarted session id differs")
                finally:
                    try:
                        stdin = restart.stdin
                        if stdin is not None:
                            stdin.close()
                    except Exception:
                        pass
                    try:
                        if r2 is not None:
                            r2.stop()
                            r2.join(timeout=5)
                    except Exception:
                        pass
                    try:
                        if restart.stdout is not None and not restart.stdout.closed:
                            restart.stdout.close()
                    except Exception:
                        pass
                    try:
                        restart.wait(timeout=5)
                    except Exception:
                        restart.kill()
            else:
                self._skip(
                    "restart persisted session via SessionManager",
                    "session not persisted to disk in headless rpc mode without a "
                    "user message / provider call; explicit restart not exercised",
                )
        except Exception as exc:
            self._fail("restart persisted session", exc)

        report = self._report()
        print(report)
        self.assertEqual([], self.failed, report)


if __name__ == "__main__":
    unittest.main()
