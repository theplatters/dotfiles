#!/usr/bin/env python3
"""Shared sensitive-path text gate (Phase 2c write paths).

Local only: never leaves the box, no network, no model call. Provenance
identities (``quickshell-ref::`` values) and scanned candidates pass
through :func:`safe_text_or_none` before they are written to markdown or
indexed: over-long text, control characters, and paths/words that look
like secrets or private state (``.ssh``, ``.gnupg``, ``.aws``, ``.env``,
``.pi/`` auth material, tokens, passwords, keys) are dropped
(fail-closed ``None``), never truncated or rewritten.

Conventions match the other helpers: no tracebacks, no state content in
diagnostics, bounded inputs. This module was factored out of
``session_capture.py`` (whose wrappers delegate here so existing imports
keep working); ``journal_assistant.py`` thought provenance uses it
directly.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

SENSITIVE_DIRS = (".ssh", ".gnupg", ".aws", ".env", ".pi")
SENSITIVE_WORDS = ("token", "secret", "password", "passwd", "credential",
                   "api-key", "api_key", "apikey", "id_rsa", "id_ed25519",
                   "private-key", "private_key", "authorization", "bearer")
_SENSITIVE_SPLIT = re.compile(r"[/\\]")


def is_sensitive(decoded: str) -> bool:
    """Return True when *decoded* names sensitive words or directories."""
    lowered = decoded.casefold()
    if any(word in lowered for word in SENSITIVE_WORDS):
        return True
    return any(segment.startswith(SENSITIVE_DIRS)
               for segment in _SENSITIVE_SPLIT.split(lowered))


def safe_text_or_none(text, limit) -> str | None:
    """Bounded fail-closed text gate; None when the text is unusable.

    *limit* is a byte... character bound on the raw input (callers pass
    160 for provenance identities, 400 for scan candidates). Double
    URL-decoding precedes the sensitive check so ``%2E%73%73%68``-style
    evasions fail closed too. Returns the stripped text on success.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return None
    if len(text) > limit:
        return None
    try:
        decoded = unquote(unquote(text))
    except Exception:
        return None
    if is_sensitive(decoded) or any(ord(c) < 32 for c in decoded):
        return None
    return text.strip()
