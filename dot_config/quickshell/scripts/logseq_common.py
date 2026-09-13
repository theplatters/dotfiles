"""Small, deliberately conservative readers for a Logseq graph."""
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


class GraphError(ValueError):
    pass


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
