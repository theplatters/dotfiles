"""Small, deliberately conservative readers and page-text helpers for a Logseq graph."""
import os
import re
import stat
from pathlib import Path
from urllib.parse import unquote

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_FILES = 5000
MAX_RESULTS = 1000
MARKERS = "TODO|NOW|LATER|DOING|WAITING"
TASK = re.compile(r"^\s*-\s*(?:(\[\s*[ xX]\s*\])\s*)?(" + MARKERS + r")\b\s*:?[ \t]*(.*)$")
SENSITIVE_NAMES = {".env", ".credentials", ".netrc", "credentials", "secrets"}
SESSION_LOG_HEADING_TITLES = frozenset({"session log", "session logs"})
SESSION_LOG_HEADING = "- ## Session logs"
_TAB_WIDTH = 4
_BULLETED_HEADING = re.compile(r"^(?P<indent>[ \t]*)[-*+][ \t]+(?P<text>.*)$")
_RAW_HEADING = re.compile(r"^(?P<text>#{1,6}[ \t]+.*)$")
_HASH_PREFIX = re.compile(r"^#{1,6}[ \t]*")


class GraphError(ValueError):
    pass


def resolve_graph(value=None):
    """Resolve a graph from CLI arg, env, or settings.json (in that order).

    Empty or whitespace-only *value* counts as missing so QML callers can
    pass ``""`` as "no override" and let ``LOGSEQ_GRAPH`` / settings.json
    decide. Raises :class:`GraphError` with a clear hint when nothing is
    configured.
    """
    if value is not None and str(value).strip():
        return graph_path(value)
    env = os.environ.get("LOGSEQ_GRAPH", "")
    if isinstance(env, str) and env.strip():
        return graph_path(env)
    try:
        from quickshell_settings import resolve_graph_raw
    except ImportError as exc:
        raise GraphError(
            "logseq graph is not configured; set LOGSEQ_GRAPH "
            "or logseqGraph in settings.json"
        ) from exc
    raw = resolve_graph_raw(None)
    if raw:
        return graph_path(raw)
    raise GraphError(
        "logseq graph is not configured; set LOGSEQ_GRAPH "
        "or logseqGraph in settings.json"
    )


def graph_path(value):
    """Resolve a graph, rejecting paths which are unsafe to inspect."""
    path = Path(value).expanduser()
    try:
        graph = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GraphError("graph is not accessible") from exc
    sensitive = (Path("/etc"), Path("/proc"), Path("/sys"), Path("/dev"), Path("/boot"))
    if (not graph.is_dir() or graph == Path("/") or graph.name.casefold() in SENSITIVE_NAMES
            or any(_inside(graph, root) for root in sensitive)):
        raise GraphError("invalid or sensitive graph path")
    # A graph may itself be a symlink, but never silently walk a symlink out of it.
    return graph


def _inside(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def markdown_files(graph):
    count = 0
    for dirname in ("pages", "journals"):
        root = graph / dirname
        if not root.is_dir() or root.is_symlink():
            continue
        for base, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(base, d).is_symlink())]
            for name in sorted(names, key=str.casefold):
                path = Path(base, name)
                if (path.suffix.casefold() != ".md" or path.is_symlink()
                        or path.name.casefold() in SENSITIVE_NAMES
                        or path.stem.casefold() in SENSITIVE_NAMES
                        or path.name.casefold().startswith((".env.", ".credentials."))):
                    continue
                try:
                    resolved = path.resolve(strict=True)
                    resolved_stat = resolved.stat()
                except OSError:
                    continue
                if not stat.S_ISREG(resolved_stat.st_mode):
                    continue
                size = resolved_stat.st_size
                if not _inside(resolved, graph) or size > MAX_FILE_BYTES:
                    continue
                if "logseq" in path.parts and "version-files" in path.parts:
                    continue
                count += 1
                if count > MAX_FILES:
                    return
                yield resolved


def read_lines(path):
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None


def page_name(path, graph):
    try:
        relative = path.relative_to(graph / "journals").with_suffix("")
    except ValueError:
        relative = path.relative_to(graph / "pages").with_suffix("")
    return unquote(str(relative)).replace("___", "/")


def _leading_ws(line):
    count = 0
    for char in line:
        if char == " " or char == "\t":
            count += 1
        else:
            break
    return line[:count]


def _indent_width(indent):
    return len(indent.expandtabs(_TAB_WIDTH))


def _normalize_session_heading(text):
    cleaned = text.strip()
    cleaned = _HASH_PREFIX.sub("", cleaned)
    cleaned = cleaned.strip()
    if cleaned.endswith(":"):
        cleaned = cleaned[:-1].strip()
    return cleaned.casefold()


def _indent_block(block, prefix):
    parts = []
    for line in block.split("\n"):
        if line == "":
            parts.append("")
        else:
            parts.append(prefix + line)
    return "\n".join(parts)


def _fence_protected(lines: list[str]) -> list[bool]:
    """Mark lines lying inside a properly closed ``` / ~~~ fence.

    Unterminated trailing fences are left unprotected so their content is
    treated as normal text.
    """
    protected = [False] * len(lines)
    fence: tuple[str, int, int] | None = None
    for index, line in enumerate(lines):
        stripped = line.lstrip(" \t")
        if fence is None:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fence_char = stripped[0]
                run = 0
                for char in stripped:
                    if char == fence_char:
                        run += 1
                    else:
                        break
                if run >= 3:
                    fence = (fence_char, run, index)
                    protected[index] = True
        else:
            protected[index] = True
            fence_char, fence_len, _start = fence
            if stripped and stripped[0] == fence_char:
                run = 0
                for char in stripped:
                    if char == fence_char:
                        run += 1
                    else:
                        break
                if run >= fence_len:
                    fence = None
    if fence is not None:
        _char, _length, start = fence
        for j in range(start, len(lines)):
            protected[j] = False
    return protected


def append_session_log(content: str, block: str) -> tuple[str, str]:
    """Insert a rendered top-level Logseq *block* under the page's Session log(s) heading.

    Returns ``(new_content, inserted_block)``: the full new page text and the
    exact text added to the page: the indented block, prefixed with the
    created heading when one had to be added (callers show it as the exact
    write preview). When no matching heading exists, a top-level
    ``- ## Session logs`` heading is created at the end of the page first.
    """
    if content == "":
        lines: list[str] = []
    else:
        lines = content.split("\n")
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    protected = _fence_protected(lines)
    heading_index: int | None = None
    heading_indent = ""
    for index, line in enumerate(lines):
        if protected[index]:
            continue
        match = _BULLETED_HEADING.match(line)
        if match is not None:
            if _normalize_session_heading(match.group("text")) in SESSION_LOG_HEADING_TITLES:
                heading_index = index
                heading_indent = match.group("indent")
                break
            continue
        raw = _RAW_HEADING.match(line)
        if raw is not None:
            if _normalize_session_heading(raw.group("text")) in SESSION_LOG_HEADING_TITLES:
                heading_index = index
                heading_indent = ""
                break
    if heading_index is None:
        indented_block = _indent_block(block, "\t")
        inserted_block = SESSION_LOG_HEADING + "\n" + indented_block
        if content == "":
            new_content = inserted_block + "\n"
        elif content.endswith("\n"):
            new_content = content + inserted_block + "\n"
        else:
            new_content = content + "\n" + inserted_block + "\n"
        return (new_content, inserted_block)
    prefix = heading_indent + "\t"
    inserted_block = _indent_block(block, prefix)
    insertion = heading_index + 1
    heading_width = _indent_width(heading_indent)
    cursor = insertion
    while cursor < len(lines):
        line = lines[cursor]
        if line.strip() == "":
            cursor += 1
            continue
        if _indent_width(_leading_ws(line)) > heading_width:
            insertion = cursor + 1
            cursor += 1
            continue
        break
    if insertion < len(offsets):
        offset = offsets[insertion]
    else:
        offset = len(content)
    piece = inserted_block + "\n"
    if offset == len(content) and offset > 0 and not content.endswith("\n"):
        piece = "\n" + piece
    new_content = content[:offset] + piece + content[offset:]
    return (new_content, inserted_block)
