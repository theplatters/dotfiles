# Logseq integration (title-only)

Logseq is title-only. A local HTTP `getCurrentPage` API path was
considered and removed: the API reports a process-global page with no
verifiable per-window binding — a configured window id cannot prove the
global value belongs to the focused window without compositor polling —
so any attributed page would be deceptive. Accuracy over coverage.

## What is stored (no config)

When the focused app is Logseq, the collector stores:

```json
{"adapter": "logseq-title", "title": "<focused window title>", "page": "<extracted page or null>"}
```

`page` follows one explicit rule: a title ending in ` - Logseq`
yields the trimmed prefix (`"My Page - Logseq"` → `"My Page"`).
Anything else (bare `"Logseq"`, other shapes) yields `page: null` —
never a guess, never a URL.

## Limitations

- Journal/daily-note titles that do not follow the suffix rule carry no
  page; the full title is still stored verbatim.
- With several Logseq windows open, each focused window gets its own
  title-derived row; nothing is shared or attributed across windows.
