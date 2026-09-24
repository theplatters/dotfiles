"""Automatic Work -> Memory tick coordinator.

``memory_tick.py`` is the only automatic path. A resident QML scheduler
(``widgets/MemoryScheduler.qml``) runs ``memory_tick.py tick`` every
``tickSeconds``. This module performs local deterministic work every
tick only: work-log drafts, capture scans (local regex only), and daily
reviews (deterministic). There are no model calls, no budget ledger,
and no decider on the automatic path.

Scope of this module:

- Central scheduling / discovery policy lives here. Domain work
  (work-log drafts, capture scans, daily reviews) lives in domain
  helpers (``work_log.py``, ``session_capture.py``,
  ``daily_review.py``).
- This coordinator exposes testable *injected job adapters*. Missing
  helpers explicitly report ``not_implemented``; it never fabricates
  drafts itself and never marks a ``not_implemented`` domain job
  successfully done.

Adapter contract (all adapters are plain callables, duck-typed):

- Evidence adapter (session discovery)::

      list_sessions(limit, from_ms=None, to_ms=None) -> [session, ...]
      get_session(session_id) -> session | None   (optional; may be None)
      device_id() -> device id | dict | None   (optional; when absent
        no device filtering is applied)

  Each ``session`` is a dict with at least ``session_id`` (32 hex),
  ``start_ms``/``end_ms`` ints, ``status``/``effective_status`` strings,
  ``event_count`` int and ``applications`` list. The coordinator issues
  one bounded overlap-range query per tick (``end_ms >= from AND
  start_ms < to``, oldest-first by ``(start_ms, end_ms, session_id)``)
  with ``DISCOVERY_LIMIT`` (1000, the Rust maximum) and never uses an
  unbounded newest-first listing, so a truncated newest-first page
  cannot silently drop the oldest unprocessed sessions.

- Job adapters::

      draft(session, ctx) -> result
      scan(ctx) -> result
      review(ctx) -> result

  ``ctx`` provides ``conn`` (annotations sqlite connection),
  ``settings`` (validated flat dict), ``now_ms``, ``evidence`` (the
  evidence adapter), ``manual`` (bool) and the canonical selectors
  ``requested_project`` (UUID or None) and ``requested_session``
  (lowercase 32-hex or None). Manual ``run`` fetches session evidence
  via ``evidence.get_session`` and reports ``unavailable`` /
  ``evidence_unavailable`` (exit 0) without invoking the adapter when
  it is unreadable; project-only drafts receive selectors via ``ctx``
  plus a minimal row with no invented timestamps.
  Each result is a dict with ``status`` (``ok`` | ``skipped`` |
  ``deferred`` | ``not_implemented``), ``reason`` (bounded token),
  ``jev_calls`` (int, always 0) and ``changed`` (bool, default False).
  ``jev_calls`` stays always 0 since the Jev path was deleted; the key
  is kept so the tick/run payload shape stays stable for the resident
  scheduler and its tests.

Discovery / completion model (state table only, no new tables):

- ``discovery_frontier_ms``: start-ordered cursor over the overlap
  range. Saturated 1000-row pages advance it to the last observed
  ``start_ms`` (inclusive); rows already completed are filtered by the
  markers below so backlogs drain over ticks. A non-saturated page
  advances it to ``now + 1``. If a full page cannot move the frontier
  (mass overlap/ties at the cursor), the tick reports an explicit
  ``discovery_saturated`` deferral instead of advancing silently.
- Per-session/job completion: draft completes via the
  ``done_draft_markers`` JSON map (``{session_id: digest}``, capped,
  evicted oldest-first). Markers are persisted BEFORE the frontier
  advances; ``not_implemented`` results are reported visibly, never
  marked done, and re-queued by session ID so a later-landing helper
  still finds the work.
- ``observed_open_sessions``: open session IDs seen, stored bounded to
  1000 (most recent kept) so the 64 KiB state cap can never wedge the
  persist-before-advance gate; rechecked by ID each tick so late
  closes with unchanged timestamps are never missed by the frontier.
- ``deferred_sessions``: session IDs whose draft work deferred,
  rechecked first and removed on completion.

Constants shared with the QML watchdog (see ``MemoryScheduler.qml``):

- The overlap lock is an OS ``flock`` on a ``0o600`` file opened with
  ``O_NOFOLLOW`` and never truncated, held for the whole tick (no
  lease/expiry), so a legitimate long pipeline never loses its claim
  mid-flight and a killed tick releases the lock via process death.
  A legacy group/other-readable lock file we own is repaired in place;
  symlinks, non-regular and foreign-owned paths raise
  ``TickLockUnsafe`` untouched and read as a soft ``lock_unsafe`` tick,
  distinct from a genuinely held lock (``overlap``).

CLI: ``tick`` (auto path, soft-fails exit 0), ``run --job ...``
(manual/UI trigger, validation errors exit 1; jobs: ``draft``,
``scan``, ``review``), ``status`` (flags, pending counts). JSON
stdout; single-line ``error: ...`` stderr.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import time
import uuid
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import annotations as _annotations

try:
    import quickshell_settings as _settings
except ImportError:  # pragma: no cover - tests inject settings
    _settings = None  # type: ignore

try:
    import desktop_projects as _desktop_projects
except ImportError:  # pragma: no cover
    _desktop_projects = None  # type: ignore

import qscli



class TickError(ValueError):
    pass


class TickLockUnsafe(TickError):
    """Lock path rejected as unsafe, or lock acquisition failed.

    Raised (never returned) so the coordinator can report
    ``lock_unsafe`` distinctly from a genuinely held lock (``overlap``).
    """


def _error(message: str):
    raise TickError(message)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_SESSIONS_PER_TICK = 5
DISCOVERY_LIMIT = 1000  # one start-ordered range page per tick (Rust max)
MIN_GAP_FALLBACK_MS = 10_000
LOCK_SUFFIX = ".tick.lock"
DEFERRED_LIST_LIMIT = 1000
DONE_DRAFT_LIMIT = 500
# Per-list, per-tick ID recheck bound. Deferred and open IDs are each
# rechecked oldest-first up to this many per tick (deferred first), so a
# 1000-deep backlog can never turn one tick into up to 1000 evidence
# subprocess calls past the QML watchdog; the recheck loop additionally
# breaks once the tick time budget is spent. Rechecked open IDs rotate
# to the end of the stored open list for eventual coverage.
RECHECK_LIMIT = 25
INPUT_LIMIT = 1024 * 1024
# Transport cap; single source of truth is qscli.INPUT_LIMIT (same value).
I64_MAX = 9223372036854775807

STATE_TICK_LAST = "tick_last_ms"
STATE_FRONTIER = "discovery_frontier_ms"
STATE_OPEN_SET = "observed_open_sessions"
STATE_DEFERRED = "deferred_sessions"
STATE_DONE_DRAFT = "done_draft_markers"

RUN_JOBS = ("draft", "scan", "review")

GENERIC_SOFT_REASON = "tick unavailable"


# ---------------------------------------------------------------------------
# Small validation helpers
# ---------------------------------------------------------------------------

def _validate_session_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("session must be 32 hex chars (128-bit)")
    text = value.strip()
    if len(text) != 32 or any(c not in "0123456789abcdefABCDEF" for c in text):
        _error("session must be 32 hex chars (128-bit)")
    return text.lower()


def _validate_project_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("project must be a UUID string")
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        _error("project must be a UUID string")


def _validate_ms(value: object, what: str = "timestamp is invalid") -> int:
    if isinstance(value, bool):
        _error(what)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = int(value.strip(), 10)
        except ValueError:
            _error(what)
    else:
        _error(what)
    if parsed < 0 or parsed > I64_MAX:
        _error(what)
    return parsed


def _now_ms(clock=None) -> int:
    if clock is not None:
        try:
            value = clock()
        except Exception:
            _error(GENERIC_SOFT_REASON)
        return _validate_ms(value, GENERIC_SOFT_REASON)
    return int(time.time() * 1000)


def _load_settings(injected=None) -> dict:
    if injected is not None:
        if not isinstance(injected, dict):
            _error(GENERIC_SOFT_REASON)
        return injected
    if _settings is None:
        _error(GENERIC_SOFT_REASON)
    try:
        return _settings.memory_settings()
    except Exception:
        _error(GENERIC_SOFT_REASON)


def _get_state_str(conn, key: str, default: str = "") -> str:
    try:
        value = _annotations.get_state(conn, key, default=default)
    except Exception:
        return default
    return value if isinstance(value, str) else default


def _get_state_int(conn, key: str, default: int = 0) -> int:
    raw = _get_state_str(conn, key, "")
    if not raw:
        return default
    try:
        return _validate_ms(raw)
    except TickError:
        return default


def _load_id_set(conn, key: str, limit: int | None = None) -> list[str]:
    """Load a JSON ID list from state; ``None`` limit keeps every ID."""
    raw = _get_state_str(conn, key, "")
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item not in out:
            out.append(item.strip())
            if limit is not None and len(out) >= limit:
                break
    return out


def _store_id_list(conn, key: str, ids: list[str]) -> bool:
    """Persist an ID list; True on success, False when storage failed."""
    cleaned = [i for i in ids if isinstance(i, str)]
    try:
        _annotations.set_state(conn, key, json.dumps(cleaned))
        return True
    except Exception:
        return False


def _load_json_dict(conn, key: str) -> dict:
    raw = _get_state_str(conn, key, "")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _store_json_dict(conn, key: str, mapping: dict) -> bool:
    """Persist a JSON dict; True on success, False when storage failed."""
    try:
        _annotations.set_state(conn, key, json.dumps(mapping))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Session helpers (bounded, deterministic)
# ---------------------------------------------------------------------------

def _session_apps(session: dict) -> list[str]:
    apps = session.get("applications")
    if isinstance(apps, list):
        return [str(a) for a in apps if isinstance(a, str) and a.strip()][:32]
    return []


def _session_event_count(session: dict) -> int:
    count = session.get("event_count")
    if isinstance(count, bool):
        return 0
    if isinstance(count, int) and count >= 0:
        return count
    return 0


def _session_bounds(session: dict) -> tuple[int, int]:
    try:
        start = int(session.get("start_ms", 0))
        end = int(session.get("end_ms", 0))
    except (TypeError, ValueError):
        return (0, 0)
    if start < 0 or end < 0:
        return (0, 0)
    return (start, end)


def _is_closed(session: dict) -> bool:
    status = session.get("status")
    effective = session.get("effective_status")
    return status == "closed" or effective == "closed"


def _sort_key(session: dict) -> tuple[int, int, str]:
    start, end = _session_bounds(session)
    sid = session.get("session_id", "")
    return (start, end, sid if isinstance(sid, str) else "")


def evidence_digest(session: dict) -> str:
    """Stable hash of bounded session metadata for the digest cache."""
    start, end = _session_bounds(session)
    duration = max(0, end - start)
    apps = sorted(_session_apps(session))[:32]
    apps = [a[:64] for a in apps]
    payload = {
        "duration": duration,
        "apps": apps,
        "events": _session_event_count(session),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Default evidence adapter (bounded range windows only)
# ---------------------------------------------------------------------------

class EvidenceUnavailable(Exception):
    pass


class DefaultEvidence:
    """Read-only evidence over ``desktop_projects``.

    Only ever issues bounded overlap-range queries
    (``end_ms >= from AND start_ms < to``, oldest-first), never an
    unbounded newest-first listing, so a truncated page cannot silently
    drop the oldest unprocessed sessions. Missing binary/DB surfaces as
    :class:`EvidenceUnavailable` (the coordinator soft-fails generic).
    """

    def list_sessions(self, *, limit, from_ms=None, to_ms=None) -> list[dict]:
        if _desktop_projects is None:
            raise EvidenceUnavailable("evidence unavailable")
        bound = limit if isinstance(limit, int) and 1 <= limit <= 1000 else 20
        try:
            out = _desktop_projects.work_sessions(
                None, bound, from_ms, to_ms, None, None, None)
        except Exception as exc:
            raise EvidenceUnavailable("evidence unavailable") from exc
        rows = out.get("sessions", [])
        return [r for r in rows if isinstance(r, dict)]

    def get_session(self, session_id: str) -> dict | None:
        if _desktop_projects is None:
            return None
        try:
            canonical = _validate_session_id(session_id)
        except TickError:
            return None
        try:
            out = _desktop_projects.get_session(canonical)
        except Exception:
            return None
        session = out.get("session") if isinstance(out, dict) else None
        return session if isinstance(session, dict) else None

    def session_resources(self, session_id, limit=20):
        return _desktop_projects.session_resources(session_id, min(limit, 20))

    def list_projects(self):
        # Keep duplicates visible: association must reject ambiguous names/IDs.
        import projects
        return projects.list_projects()

    def current_project(self):
        """Current desktop project (dict or None; never raises)."""
        if _desktop_projects is None:
            return None
        try:
            out = _desktop_projects.current_project()
        except Exception:
            return None
        if not isinstance(out, dict):
            return None
        return out

    def device_id(self):
        if _desktop_projects is None:
            raise EvidenceUnavailable("evidence unavailable")
        try:
            return _desktop_projects.device_id()
        except Exception as exc:
            raise EvidenceUnavailable("evidence unavailable") from exc


# ---------------------------------------------------------------------------
# Default job adapters (capture/review remain optional future helpers)
# ---------------------------------------------------------------------------

def _not_implemented(ctx, job: str) -> dict:
    return {"status": "not_implemented", "reason": "not_implemented",
            "job": job, "jev_calls": 0, "changed": False}
def default_draft(session, ctx) -> dict:
    try:
        import work_log
    except Exception:
        return _not_implemented(ctx, "draft")
    try:
        return work_log.draft_for_session(
            session, conn=ctx.get("conn"), settings=ctx.get("settings") or {},
            now_ms=ctx.get("now_ms"), evidence=ctx.get("evidence"),
            allow_default=isinstance(ctx.get("evidence"), DefaultEvidence))
    except Exception:
        return {"status": "deferred", "reason": "failed", "jev_calls": 0,
                "changed": False}


def default_scan(ctx) -> dict:
    try:
        import session_capture
    except Exception:
        return _not_implemented(ctx, "scan")
    try:
        return session_capture.scan(ctx)
    except Exception:
        return {"status": "deferred", "reason": "failed", "jev_calls": 0,
                "changed": False}


def default_review(ctx) -> dict:
    try:
        import daily_review
    except Exception:
        return _not_implemented(ctx, "review")
    try:
        return daily_review.review(ctx)
    except Exception:
        return {"status": "deferred", "reason": "failed", "jev_calls": 0,
                "changed": False}
# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

def _pending_counts(conn) -> dict:
    counts = {"drafts": 0, "captures": 0, "reviews": 0}
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM drafts WHERE saved_journal_ms IS NULL"
            " AND saved_page_ms IS NULL").fetchone()
        counts["drafts"] = int(row["n"]) if row is not None else 0
    except Exception:
        pass
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM captures WHERE status = 'new'").fetchone()
        counts["captures"] = int(row["n"]) if row is not None else 0
    except Exception:
        pass
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM reviews").fetchone()
        counts["reviews"] = int(row["n"]) if row is not None else 0
    except Exception:
        pass
    return counts


def _empty_processed() -> dict:
    return {"sessions": 0, "drafts": 0, "captures": 0, "review": False}


def _settings_min_session_ms(settings: dict) -> int:
    value = settings.get("minSessionMs", 300000) \
        if isinstance(settings, dict) else 300000
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 300000
    return value


def run_tick(*, conn, settings: dict, now_ms: int, evidence,
             draft=None, scan=None, review=None, lock_fn=None,
             monotonic=None,
             discovery_limit: int = DISCOVERY_LIMIT) -> dict:
    """Run one automatic tick; return the ``tick`` payload dict.

    Pure coordinator: all IO beyond the passed ``conn`` goes through the
    injected ``evidence`` / job adapters / ``lock_fn`` /
    ``monotonic`` so tests never touch the network, the real binary, or
    the real clock.
    ``discovery_limit`` bounds the one range page (tests inject a small
    page to prove saturation draining; production uses 1000).
    ``lock_fn`` (when given) must return a held lock handle, falsy for
    a genuinely held lock (reported as ``overlap``), or raise
    :class:`TickLockUnsafe` for a rejected lock path (reported as
    ``lock_unsafe``); any other ``lock_fn`` exception also reads as
    ``overlap``. ``None`` ``lock_fn`` skips locking (single-process
    tests).
    """
    draft_fn = draft or default_draft
    scan_fn = scan or default_scan
    review_fn = review or default_review

    lock_handle = None
    if lock_fn is not None:
        try:
            lock_handle = lock_fn()
        except TickLockUnsafe:
            return _lock_unsafe_payload(settings)
        except Exception:
            lock_handle = None
        if not lock_handle:
            return {"available": True, "enabled": settings,
                    "processed": _empty_processed(), "jev_calls": 0,
                    "deferred": [{"job": "tick", "reason": "overlap"}],
                    "changed": False}
    try:
        return _run_tick_locked(
            conn=conn, settings=settings, now_ms=now_ms, evidence=evidence,
            draft_fn=draft_fn, scan_fn=scan_fn,
            review_fn=review_fn, monotonic=monotonic,
            discovery_limit=discovery_limit)
    finally:
        if lock_handle is not None:
            closer = getattr(lock_handle, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass


def _run_tick_locked(*, conn, settings, now_ms, evidence,
                     draft_fn, scan_fn, review_fn, monotonic,
                     discovery_limit) -> dict:
    if not isinstance(settings, dict) or not settings.get("enabled", True):
        return {"available": True, "enabled": settings,
                "processed": _empty_processed(), "jev_calls": 0,
                "deferred": [{"job": "tick", "reason": "disabled"}],
                "changed": False}
    tick_seconds = settings.get("tickSeconds", 60)
    if not isinstance(tick_seconds, int) or tick_seconds < 10:
        tick_seconds = 60
    last_tick = _get_state_int(conn, STATE_TICK_LAST, 0)
    if last_tick:
        delta = now_ms - last_tick
        # Only a non-negative delta inside the window is a gap: a clock
        # rollback (negative delta) must proceed and restamp tick_last
        # instead of wedging behind the stale value.
        if 0 <= delta < tick_seconds * 1000:
            return {"available": True, "enabled": settings,
                    "processed": _empty_processed(), "jev_calls": 0,
                    "deferred": [{"job": "tick", "reason": "tick_gap"}],
                    "changed": False}

    _ = monotonic
    work_log_on = settings.get("workLog", True) is True
    scan_on = settings.get("sessionCapture", False) is True
    review_on = settings.get("dailyReview", True) is True

    frontier = _get_state_int(conn, STATE_FRONTIER, 0)
    open_ids = _load_id_set(conn, STATE_OPEN_SET, None)
    deferred_ids = _load_id_set(conn, STATE_DEFERRED, DEFERRED_LIST_LIMIT)
    done_draft = _load_json_dict(conn, STATE_DONE_DRAFT)

    # One bounded start-ordered range page. ``to`` is exclusive on start
    # (Rust overlap: end >= from AND start < to); +1 includes sessions
    # starting exactly at ``now``. ``to`` never bounds ``end``, so long
    # sessions always overlap and late closes keep their timestamps.
    window_to = now_ms + 1
    if window_to > I64_MAX:
        window_to = I64_MAX
    if frontier > window_to:
        frontier = window_to
    page_limit = discovery_limit if isinstance(discovery_limit, int) \
        and 1 <= discovery_limit <= 1000 else DISCOVERY_LIMIT
    try:
        rows = evidence.list_sessions(limit=page_limit, from_ms=frontier,
                                      to_ms=window_to)
    except EvidenceUnavailable:
        raise
    except Exception as exc:
        raise EvidenceUnavailable("evidence unavailable") from exc
    if not isinstance(rows, list):
        rows = []
    rows = [r for r in rows if isinstance(r, dict)]
    saturated = len(rows) >= page_limit

    by_id: dict[str, dict] = {}
    for row in rows:
        sid = row.get("session_id")
        if isinstance(sid, str) and sid:
            by_id[sid.lower()] = row
    # Recheck previously observed open sessions (late closes keep the
    # same timestamps, so the start frontier alone would miss them) plus
    # explicitly deferred IDs first. Each list is bounded per tick
    # (oldest first, deferred before open).
    deferred_recheck = [i for i in deferred_ids if i.lower() not in by_id][
        :RECHECK_LIMIT]
    deferred_planned = {i.lower() for i in deferred_recheck}
    open_recheck = [i for i in open_ids
                    if i.lower() not in by_id
                    and i.lower() not in deferred_planned][
        :RECHECK_LIMIT]
    recheck_plan = [(i, False) for i in deferred_recheck] + [
        (i, True) for i in open_recheck]
    rechecked_open: list[str] = []
    rechecked_deferred: list[str] = []
    get_session = getattr(evidence, "get_session", None)
    if recheck_plan and callable(get_session):
        for sid, is_open in recheck_plan:
            try:
                one = get_session(sid)
            except Exception:
                one = None
            if isinstance(one, dict) and isinstance(
                    one.get("session_id"), str):
                by_id[one["session_id"].lower()] = one
            # Every actually-fetched ID rotates, even when the fetch
            # returned nothing: a transient outage must not wedge the
            # front block, and stale front entries must not starve the
            # IDs behind them. Fetches are never dropped here.
            if is_open:
                rechecked_open.append(sid)
            else:
                rechecked_deferred.append(sid)
    if rechecked_open:
        # Rotate actually-rechecked open IDs to the end (relative order
        # kept) so IDs parked behind a permanently-open front still get
        # eventual coverage on later ticks.
        lowered = {s.lower() for s in rechecked_open}
        open_ids = [o for o in open_ids if o.lower() not in lowered] + [
            o for o in open_ids if o.lower() in lowered]
    ordered = sorted(by_id.values(), key=_sort_key)
    closed = [s for s in ordered if _is_closed(s)]

    # Completion filter: draft completes via the done-marker map (ok AND
    # skipped both mark done: skipped is a final adapter decision for
    # this digest, e.g. no project; a new digest re-opens the work and
    # the manual run path never consults markers). Jobs whose flag is
    # off need nothing.
    actionable: list[dict] = []
    digests: dict[str, str] = {}
    for session in closed:
        sid = session.get("session_id", "")
        key = sid.lower() if isinstance(sid, str) else ""
        if not key:
            continue
        digest = evidence_digest(session)
        digests[key] = digest
        draft_need = work_log_on and done_draft.get(key) != digest
        if draft_need:
            actionable.append(session)
    todo = actionable[:MAX_SESSIONS_PER_TICK]

    ctx = {"conn": conn, "settings": settings, "now_ms": now_ms,
           "evidence": evidence, "manual": False}

    processed = _empty_processed()
    jev_calls = 0
    deferred: list[dict] = []
    changed = False
    # Sessions whose work deferred this tick (persisted for recheck).
    still_deferred: set[str] = set()
    # Sessions fully complete after this tick (removed from deferred).
    newly_complete: set[str] = set()

    for session in todo:
        sid = session.get("session_id", "")
        key = sid.lower()
        digest = digests.get(key, evidence_digest(session))
        session_ctx = dict(ctx)
        # Local deterministic draft. ok and skipped both mark the
        # digest done (skipped is final for this digest); only real
        # deferrals stay pending.
        draft_need = work_log_on and done_draft.get(key) != digest
        if draft_need:
            try:
                draft_result = draft_fn(session, session_ctx)
            except Exception:
                draft_result = {"status": "deferred", "reason": "failed",
                                "jev_calls": 0, "changed": False}
            if not isinstance(draft_result, dict):
                draft_result = {"status": "deferred", "reason": "failed",
                                "jev_calls": 0, "changed": False}
            try:
                jev_calls += max(0, int(draft_result.get("jev_calls", 0)))
            except (TypeError, ValueError):
                pass
            if draft_result.get("changed") is True:
                changed = True
                processed["drafts"] += 1
            draft_status = draft_result.get("status", "skipped")
            draft_reason = str(draft_result.get("reason", ""))[:64]
            if draft_status == "not_implemented":
                # Visible and re-queued by ID; never marked done.
                deferred.append({"job": "draft", "session": sid,
                                 "reason": "not_implemented"})
                still_deferred.add(key)
            elif draft_status == "deferred":
                deferred.append({"job": "draft", "session": sid,
                                 "reason": draft_reason or "deferred"})
                still_deferred.add(key)
            else:
                done_draft[key] = digest
        processed["sessions"] += 1
        if key in [o.lower() for o in open_ids]:
            open_ids = [o for o in open_ids if o.lower() != key]

    # Completion sweep: every closed session present this tick (page
    # or recheck) whose draft (marked or unneeded) is done leaves the
    # deferred set. Anything still needing work is never added.
    for session in closed:
        sid = session.get("session_id", "")
        key = sid.lower() if isinstance(sid, str) else ""
        if not key or key in still_deferred:
            continue
        digest = digests.get(key, "")
        draft_done = (not work_log_on) or done_draft.get(key) == digest
        if draft_done:
            newly_complete.add(key)
    # Prune the done-draft map oldest-first (bounded state value).
    while len(done_draft) > DONE_DRAFT_LIMIT:
        done_draft.pop(next(iter(done_draft)))

    # Scan: centrally flag-gated local pass (local regex only).
    if scan_on:
        try:
            scan_result = scan_fn(ctx)
        except Exception:
            scan_result = {"status": "deferred", "reason": "failed",
                           "jev_calls": 0, "changed": False}
        if not isinstance(scan_result, dict):
            scan_result = {"status": "deferred", "reason": "failed",
                           "jev_calls": 0, "changed": False}
        try:
            jev_calls += max(0, int(scan_result.get("jev_calls", 0)))
        except (TypeError, ValueError):
            pass
        if scan_result.get("changed") is True:
            changed = True
            processed["captures"] += 1
        scan_status = scan_result.get("status", "skipped")
        scan_reason = str(scan_result.get("reason", ""))[:64]
        if scan_status == "not_implemented":
            deferred.append({"job": "scan",
                             "reason": "not_implemented"})
        elif scan_status == "deferred":
            deferred.append({"job": "scan",
                             "reason": scan_reason or "deferred"})
    # Review: centrally flag-gated deterministic local pass.
    if review_on:
        try:
            review_result = review_fn(ctx)
        except Exception:
            review_result = {"status": "deferred", "reason": "failed",
                             "jev_calls": 0, "changed": False}
        if not isinstance(review_result, dict):
            review_result = {"status": "deferred", "reason": "failed",
                             "jev_calls": 0, "changed": False}
        try:
            jev_calls += max(0, int(review_result.get("jev_calls", 0)))
        except (TypeError, ValueError):
            pass
        if review_result.get("changed") is True:
            changed = True
            processed["review"] = True
        review_status = review_result.get("status", "skipped")
        review_reason = str(review_result.get("reason", ""))[:64]
        if review_status == "not_implemented":
            deferred.append({"job": "review",
                             "reason": "not_implemented"})
        elif review_status == "deferred":
            deferred.append({"job": "review",
                             "reason": review_reason or "deferred"})

    # Persist job state BEFORE advancing the discovery frontier.
    still_open = [r.get("session_id") for r in ordered
                  if not _is_closed(r)
                  and isinstance(r.get("session_id"), str)]
    for sid in still_open:
        if sid.lower() not in [o.lower() for o in open_ids]:
            open_ids.append(sid)
    # The stored open set stays bounded: state values are capped at
    # 64 KiB, so an unbounded list would fail persisting forever and
    # wedge the frontier via persist_failed. Keep the most recent IDs;
    # the load above may still read a longer legacy value once.
    if len(open_ids) > DEFERRED_LIST_LIMIT:
        open_ids = open_ids[-DEFERRED_LIST_LIMIT:]
    ok_open = _store_id_list(conn, STATE_OPEN_SET, open_ids)
    pending = [s for s in deferred_ids
               if s.lower() not in newly_complete]
    for session in todo:
        # Todo order is start-ordered, so the persisted list stays
        # roughly oldest-first; the cap below keeps the newest parked
        # work, and rechecked deferred IDs rotate to the end afterwards
        # for eventual coverage (see below).
        sid = session.get("session_id", "")
        key = sid.lower() if isinstance(sid, str) else ""
        if key in still_deferred and key not in [
                o.lower() for o in pending]:
            pending.append(sid if isinstance(sid, str) and sid else key)
    if rechecked_deferred:
        # Rotate actually-rechecked deferred IDs still pending to the
        # end (relative order kept) so stale/unresolvable front entries
        # cannot starve the IDs behind them. Nothing is dropped here.
        lowered = {s.lower() for s in rechecked_deferred}
        pending = [p for p in pending if p.lower() not in lowered] + [
            p for p in pending if p.lower() in lowered]
    pending = [s for s in pending
               if isinstance(s, str)][-DEFERRED_LIST_LIMIT:]
    ok_deferred = _store_id_list(conn, STATE_DEFERRED, pending)
    ok_done = _store_json_dict(conn, STATE_DONE_DRAFT, done_draft)
    persist_ok = ok_open and ok_deferred and ok_done
    if not persist_ok:
        # Held frontier: completion/deferral markers may be lost, so the
        # next tick must rediscover the same window instead of moving on.
        deferred.append({"job": "state", "reason": "persist_failed"})

    # Discovery frontier: saturated pages advance at most the last
    # observed start (inclusive) -- unseen shorter intervals are still
    # possible past the cut, so never jump to max end -- and never past
    # the smallest start still needing work, so observed-but-unprocessed
    # sessions (beyond the 5-per-tick budget) are never skipped: their
    # starts keep them overlapping the next window, while completed rows
    # are filtered and open/deferred IDs are rechecked by ID. A full
    # page that cannot move the frontier reports discovery_saturated
    # instead of advancing silently. The frontier never advances when
    # the persist step above failed (persist_failed is reported and the
    # same window is rediscovered next tick).
    cap = frontier
    if persist_ok:
        if saturated:
            starts = [_session_bounds(r)[0] for r in rows]
            cap = max(starts) if starts else frontier
        else:
            cap = window_to
        leftover = actionable[MAX_SESSIONS_PER_TICK:]
        if leftover:
            cap = min(cap, min(_session_bounds(s)[0] for s in leftover))
    if cap <= frontier:
        if persist_ok and saturated:
            deferred.append({"job": "discovery",
                             "reason": "discovery_saturated"})
    else:
        try:
            _annotations.set_state(conn, STATE_FRONTIER, str(cap))
        except Exception:
            pass
    try:
        _annotations.set_state(conn, STATE_TICK_LAST, str(now_ms))
    except Exception:
        pass
    return {"available": True, "enabled": settings, "processed": processed,
            "jev_calls": jev_calls, "deferred": deferred,
            "changed": changed}


def get_status(*, conn, settings: dict, now_ms: int) -> dict:
    """Return the ``status`` payload (evidence-free, never touches net)."""
    last_tick = _get_state_int(conn, STATE_TICK_LAST, 0)
    pending = _pending_counts(conn)
    deferred_ids = _load_id_set(conn, STATE_DEFERRED, DEFERRED_LIST_LIMIT)
    pending["sessions"] = len(deferred_ids)
    return {"available": True, "enabled": settings,
            "last_tick_ms": last_tick if last_tick else None,
            "pending": pending, "changed": False}


# ---------------------------------------------------------------------------
# Overlap lock (OS flock, no expiry)
# ---------------------------------------------------------------------------

def _lock_file_is_safe(info) -> bool:
    """Owner-only regular file owned by us; never symlink/dir/other."""
    return _lock_reject_kind(info) == ""


def _lock_reject_kind(info) -> str:
    """Classify an existing lock path: "" safe, else symlink/nonregular/foreign/mode."""
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if not stat.S_ISREG(info.st_mode):
        return "nonregular"
    try:
        owned = info.st_uid == os.geteuid()
    except AttributeError:
        owned = True
    if not owned:
        return "foreign"
    if stat.S_IMODE(info.st_mode) & 0o077 != 0:
        return "mode"
    return ""


def acquire_tick_lock(lock_path: str):
    """Acquire the tick overlap lock (non-blocking).

    Returns the held handle, ``None`` when another process genuinely
    holds the lock (``flock`` would block), and raises
    :class:`TickLockUnsafe` for rejected paths or open failures.

    The lock file is opened with ``O_CREAT | O_RDWR | O_NOFOLLOW`` mode
    ``0o600`` and never truncated, so a pre-existing lock keeps its
    content. A regular, owner-held file with legacy group/other bits
    (e.g. from the older truncating opener) is repaired in place with
    ``os.chmod(..., follow_symlinks=False)`` and re-checked. Symlinks,
    non-regular files and foreign-owned files are never touched --
    they raise. The lock is held for the whole tick and released by
    closing the returned handle (or by process death), so a legitimate
    long pipeline never loses its claim mid-flight. There is
    deliberately no lease/expiry: the QML 180 s watchdog owns timeouts.
    """
    try:
        import fcntl
    except ImportError as exc:
        raise TickLockUnsafe("tick lock unavailable") from exc
    try:
        parent = os.path.dirname(os.path.abspath(lock_path))
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        raise TickLockUnsafe("tick lock unavailable") from exc
    try:
        info = os.lstat(lock_path)
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise TickLockUnsafe("tick lock unavailable") from exc
    if info is not None:
        kind = _lock_reject_kind(info)
        if kind == "mode":
            # Legacy loose file we own: repair in place, then re-check.
            # Never chmod symlinks, non-regular or foreign-owned paths.
            # (No-follow chmod may be unimplemented on some platforms.)
            try:
                os.chmod(lock_path, 0o600, follow_symlinks=False)
                info = os.lstat(lock_path)
            except (OSError, NotImplementedError) as exc:
                raise TickLockUnsafe("tick lock unavailable") from exc
            kind = _lock_reject_kind(info)
        if kind:
            raise TickLockUnsafe("tick lock unavailable")
    try:
        fd = os.open(lock_path,
                     os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise TickLockUnsafe("tick lock unavailable") from exc
    try:
        if not _lock_file_is_safe(os.fstat(fd)):
            raise TickLockUnsafe("tick lock unavailable")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                os.close(fd)
            except OSError:
                pass
            return None
        handle = os.fdopen(fd, "r+")
    except TickLockUnsafe:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    except OSError as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        raise TickLockUnsafe("tick lock unavailable") from exc
    return handle


def _lock_unsafe_payload(settings) -> dict:
    """Soft available payload for rejected lock paths (exit 0)."""
    try:
        enabled = settings if isinstance(settings, dict) else {}
    except Exception:
        enabled = {}
    return {"available": True, "enabled": enabled,
            "processed": _empty_processed(), "jev_calls": 0,
            "deferred": [{"job": "tick", "reason": "lock_unsafe"}],
            "changed": False}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# CLI (shared plumbing lives in qscli.py; argv stays byte-identical)
# ---------------------------------------------------------------------------

# Grandfathered: session_capture.py and daily_review.py build their own
# CLIs on tick._SafeParser; it is now qscli.SafeParser (same contract).
_SafeParser = qscli.SafeParser


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = qscli.SafeParser(description=__doc__)
    qscli.add_global_flags(parser, db=True)
    sub = parser.add_subparsers(dest="command", required=True,
                                parser_class=qscli.SafeParser)
    sub.add_parser("tick")
    sub.add_parser("status")
    p_run = sub.add_parser("run")
    p_run.add_argument("--job", required=True,
                       choices=list(RUN_JOBS))
    p_run.add_argument("--project", default=None)
    p_run.add_argument("--session", default=None)
    return parser.parse_args(argv if argv is not None else sys.argv[1:])


def _dispatch(args: argparse.Namespace) -> tuple[int, dict]:
    if args.command == "tick":
        try:
            return _do_tick(args.db)
        except (TickError, OSError, ValueError):
            raise
        except Exception:
            # Grandfathered: the automatic path never fails hard; an
            # unexpected fault reads as soft unavailable (exit 0).
            return (0, _soft_unavailable())
    if args.command == "status":
        try:
            return _do_status(args.db)
        except (TickError, OSError, ValueError):
            raise
        except Exception as exc:
            raise TickError("manual run failed") from exc
    if args.command == "run":
        try:
            return _do_run(args.db, args.job, args.project, args.session)
        except (TickError, OSError, ValueError):
            raise
        except Exception as exc:
            raise TickError("manual run failed") from exc
    _error(f"unknown command {args.command}")


_BOUNDED_EXCEPTIONS = (TickError, OSError, ValueError)


def _soft_unavailable(settings=None) -> dict:
    try:
        enabled = settings if isinstance(settings, dict) else {}
    except Exception:
        enabled = {}
    return {"available": False, "enabled": enabled,
            "processed": _empty_processed(), "jev_calls": 0,
            "deferred": [], "changed": False,
            "reason": GENERIC_SOFT_REASON}


def _do_tick(db_override, settings_injected=None, evidence=None,
             clock=None, lock_fn=None) -> tuple[int, dict]:
    settings = _load_settings(settings_injected)
    now_ms = _now_ms(clock)
    try:
        conn = _annotations.connect(db_override)
    except Exception:
        return 0, _soft_unavailable(settings)
    try:
        evidence_obj = evidence or DefaultEvidence()
        if lock_fn is None:
            try:
                db_path = _annotations.resolve_db_path(db_override)
                default_lock = str(db_path) + LOCK_SUFFIX
            except Exception:
                default_lock = None
            if default_lock is None:
                # Never run the pipeline unlocked; report softly.
                return 0, _lock_unsafe_payload(settings)
            lock_fn = lambda: acquire_tick_lock(default_lock)  # noqa: E731
        try:
            payload = run_tick(conn=conn, settings=settings, now_ms=now_ms,
                               evidence=evidence_obj, lock_fn=lock_fn)
        except EvidenceUnavailable:
            try:
                _annotations.set_state(conn, STATE_TICK_LAST, str(now_ms))
            except Exception:
                pass
            return 0, _soft_unavailable(settings)
        except (TickError, OSError, ValueError):
            return 0, _soft_unavailable(settings)
        return 0, payload
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _do_status(db_override, settings_injected=None, clock=None) -> tuple[int, dict]:
    settings = _load_settings(settings_injected)
    now_ms = _now_ms(clock)
    try:
        conn = _annotations.connect(db_override)
    except Exception:
        return 0, {"available": False, "enabled": settings,
                   "reason": GENERIC_SOFT_REASON}
    try:
        payload = get_status(conn=conn, settings=settings, now_ms=now_ms)
        return 0, payload
    except (TickError, OSError, ValueError):
        return 0, {"available": False, "enabled": settings,
                   "reason": GENERIC_SOFT_REASON}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _manual_unavailable(settings: dict, job: str) -> tuple[int, dict]:
    """Bounded manual shape when session evidence is unreadable.

    Exit 0 manual shape, adapter never invoked, no paths or state
    content leak: fixed tokens only.
    """
    return 0, {"available": True, "enabled": settings, "job": job,
               "status": "unavailable", "reason": "evidence_unavailable",
               "jev_calls": 0, "changed": False}


def _do_run(db_override, job: str, project, session,
            settings_injected=None, clock=None,
            adapters=None, evidence=None) -> tuple[int, dict]:
    if job not in RUN_JOBS:
        _error(f"unknown job {job}")
    requested_project = _validate_project_id(project) \
        if project is not None else None
    requested_session = _validate_session_id(session) \
        if session is not None else None
    if job == "draft" and requested_session is None \
            and requested_project is None:
        _error("run --job draft requires --session ID or --project UUID")
    settings = _load_settings(settings_injected)
    now_ms = _now_ms(clock)
    try:
        conn = _annotations.connect(db_override)
    except Exception:
        _error(GENERIC_SOFT_REASON)
    try:
        adapters = adapters or {}
        evidence_obj = evidence if evidence is not None \
            else DefaultEvidence()
        ctx = {"conn": conn, "settings": settings, "now_ms": now_ms,
               "evidence": evidence_obj, "manual": True,
               "requested_project": requested_project,
               "requested_session": requested_session}

        def fetch_session(sid: str) -> dict | None:
            getter = getattr(evidence_obj, "get_session", None)
            if not callable(getter):
                return None
            try:
                row = getter(sid)
            except Exception:
                return None
            if not isinstance(row, dict) or not isinstance(
                    row.get("session_id"), str):
                return None
            return row

        if job == "draft":
            fn = adapters.get("draft", default_draft)
            if requested_session is not None:
                row = fetch_session(requested_session)
                if row is None:
                    return _manual_unavailable(settings, job)
                result = fn(row, ctx)
            else:
                # Project-only: selectors travel via ctx; the minimal row
                # carries no invented timestamps or metadata.
                row = {"session_id": "",
                       "project_id": requested_project,
                       "status": "unknown", "effective_status": "unknown",
                       "event_count": 0, "applications": []}
                result = fn(row, ctx)
        elif job == "scan":
            fn = adapters.get("scan", default_scan)
            result = fn(ctx)
        else:
            fn = adapters.get("review", default_review)
            result = fn(ctx)
        if not isinstance(result, dict):
            result = {"status": "deferred", "reason": "failed"}
        try:
            calls = max(0, int(result.get("jev_calls", 0)))
        except (TypeError, ValueError):
            calls = 0
        return 0, {"available": True, "enabled": settings, "job": job,
                   "status": result.get("status", "skipped"),
                   "reason": str(result.get("reason", ""))[:128],
                   "jev_calls": calls,
                   "changed": result.get("changed") is True}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    return qscli.run_main(_parse_args, _dispatch, "memory tick",
                          _BOUNDED_EXCEPTIONS, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
