#!/usr/bin/env python3
"""Repeatable Zen extension (XPI) + native-host installer (stdlib only).

Packages ``extension/`` (``manifest.json`` + ``background.js``) into a
deterministic unsigned ``.xpi`` and registers the per-user native-messaging
host manifest for native Zen *or* the Flatpak ``app.zen_browser.zen``
sandbox (explicit ``--mode native`` vs ``--mode flatpak``; there is no
combined mode — native and Flatpak need different manifest targets).

Fixed contract (never parametrised):

- extension id: ``qs-zen-context@quickshell.local``
- native host name: ``local.quickshell.zen_context``

Safety / scope:

- Default is a dry-run plan: nothing is written unless ``--install`` is
  passed explicitly.
- Never touches signing prefs (no
  ``xpinstall.signatures.required`` handling anywhere) and never claims the
  XPI is signed. An unsigned XPI loads only as a temporary add-on via
  ``about:debugging`` (session-scoped); a permanent install requires AMO
  signing (see optional ``--sign-amo`` / ``--signed-xpi`` below).
- Never executes ``flatpak override`` (or any live browser/manifest
  mutation beyond the requested per-user files). For ``--flatpak`` the
  exact override command is printed for the operator to review and run
  manually. The only subprocess ever spawned is ``npx web-ext sign`` when
  ``--sign-amo`` is explicitly requested; unsigned installs and
  ``--signed-xpi`` copies never shell out.

Optional AMO signing (unlisted, operator-supplied credentials only):

- ``--sign-amo`` runs ``npx web-ext sign --channel unlisted`` against the
  validated ``extension-dir`` source (same ``manifest.json`` +
  ``background.js`` contract) and copies the resulting signed XPI to the
  requested ``--xpi`` destination. Credentials are accepted as
  ``--amo-issuer`` / ``--amo-secret`` (aliases ``--api-key`` /
  ``--api-secret``) or via the ``AMO_JWT_ISSUER`` / ``AMO_JWT_SECRET``
  environment, with CLI flags taking precedence. Secrets are never
  printed, logged, written to disk, persisted, or included in the plan /
  diagnostics; captured ``web-ext`` output is redacted (``[REDACTED]``)
  and truncated before display. A ``web-ext`` failure returns a bounded
  error and never claims the artifact is signed.
- ``--signed-xpi PATH`` skips ``web-ext`` entirely and copies an
  externally obtained signed XPI to the ``--xpi`` destination (useful
  when signing happens on another host). ``--sign-amo`` and
  ``--signed-xpi`` are mutually exclusive.
- After a successful sign/copy, host manifest/wrapper registration
  proceeds exactly as the current ``--mode`` (native vs flatpak).
- Permanent deployment of a signed XPI still needs the operator to
  install it (``about:addons`` -> Install Add-on From File) and, for
  force-install, an ``ExtensionSettings`` policy entry mapping the fixed
  extension id to the signed ``file:///`` URL. Flatpak system policy
  (``policies.json`` under the sandbox) is intentionally left as a
  documented manual step and is never written by this installer.

Flatpak background (known environment):

- The host ``~/.mozilla/native-messaging-hosts/`` directory is absent by
  default; the sandbox profile persists at
  ``~/.var/app/app.zen_browser.zen/.zen`` (mapped ``~/.zen``); Gecko looks
  for hosts under ``~/.mozilla/native-messaging-hosts`` inside the sandbox,
  so the sandbox must see the manifest directory too.
- ``flatpak-spawn --host`` is denied by default, so the sandbox needs
  ``--talk-name=org.freedesktop.Flatpak`` (WARNING: this grants the
  sandboxed browser full host-command execution — review before applying)
  plus narrow read-only exposures of *both* the wrapper directory
  (``--filesystem=<wrapper-dir>:ro``) *and* the hosts directory
  (``--filesystem=<hosts-dir>:ro``) so the manifest is visible and the
  wrapper path resolves to the same absolute location.
- This installer writes a small ``sh`` wrapper on the host that calls
  ``/usr/bin/flatpak-spawn --host /usr/bin/python3 <host> "$@"`` with
  shell-quoted absolute paths, and points the registered manifest ``path``
  at that sandbox-visible wrapper. ``--mode native`` instead points the
  manifest directly at the real native-host executable. The two modes share
  one manifest filename, so only one mode may be installed at a time.

Typical usage::

    # Dry-run (default, writes nothing):
    python3 install.py
    python3 install.py --mode flatpak
    python3 install.py --mode flatpak --xpi /tmp/opencode/qs-zen-context.xpi

    # Actually write files:
    python3 install.py --install
    python3 install.py --install --mode flatpak
    python3 install.py --install --mode flatpak --output-dir /tmp/opencode

    # Explicit locations (when the host filename/layout is not final):
    python3 install.py --install \\
        --host-path /abs/path/to/qs-zen-native-host.py \\
        --extension-dir /abs/path/to/extension \\
        --hosts-dir ~/.mozilla/native-messaging-hosts \\
        --wrapper-dir ~/.local/share/quickshell/zen-native-host \\
        --python-bin /usr/bin/python3 \\
        --flatpak-app app.zen_browser.zen

    # Optional AMO unlisted signing (credentials supplied at runtime only,
    # never hardcoded; secrets are never echoed or stored):
    AMO_JWT_ISSUER='...' AMO_JWT_SECRET='...' \\
        python3 install.py --install --sign-amo
    python3 install.py --install --sign-amo \\
        --amo-issuer "$AMO_JWT_ISSUER" --amo-secret "$AMO_JWT_SECRET"
    python3 install.py --install --signed-xpi /path/to/signed.xpi

    # With credentials exported, the operator runs e.g.:
    #   AMO_JWT_ISSUER=... AMO_JWT_SECRET=... python3 install.py \\
    #       --install --sign-amo --xpi /tmp/opencode/qs-zen-context.xpi
    # Secrets must be provided via environment or flags at runtime; see
    # --help for redaction guarantees.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

EXTENSION_ID = "qs-zen-context@quickshell.local"
HOST_NAME = "local.quickshell.zen_context"
FLATPAK_APP_ID = "app.zen_browser.zen"

PYTHON_BIN = "/usr/bin/python3"
FLATPAK_SPAWN = "/usr/bin/flatpak-spawn"

DEFAULT_XPI_NAME = "qs-zen-context.xpi"
DEFAULT_OUTPUT_DIR = Path("/tmp/opencode")

HERE = Path(__file__).resolve().parent
DEFAULT_HOST_PATH = HERE / "qs-zen-native-host.py"
DEFAULT_EXTENSION_DIR = HERE / "extension"
DEFAULT_TEMPLATE_PATH = HERE / "local.quickshell.zen_context.json"

# Files that must be present in extension/ and packaged into the XPI.
XPI_MEMBERS = ("manifest.json", "background.js")

# Fixed timestamp for deterministic zips (ZIP epoch-safe, repeatable bytes
# modulo compression level which is fixed to ZIP_DEFLATED here).
_STABLE_DATE = (2020, 1, 1, 0, 0, 0)

# AMO credential sources (never logged/persisted; CLI takes precedence).
AMO_ENV_ISSUER = "AMO_JWT_ISSUER"
AMO_ENV_SECRET = "AMO_JWT_SECRET"

# Placeholder substituted for every secret value in captured output.
REDACTED = "[REDACTED]"

# Upper bound for surfaced web-ext output (chars, after redaction).
_WEBEXT_OUTPUT_LIMIT = 4000

# web-ext invocation is fixed: unlisted channel only, args list only.
_WEBEXT_CMD = ("npx", "web-ext", "sign")


def _redact_secrets(text: str, secrets: tuple[str, ...] | list[str]) -> str:
    """Return *text* with every non-empty secret value replaced.

    Pure string replacement (no logging, no I/O). Empty values are
    ignored so ``str.replace`` can never wipe the whole output.
    """
    redacted = text if isinstance(text, str) else str(text)
    for secret in secrets or ():
        if secret:
            redacted = redacted.replace(secret, REDACTED)
    return redacted


def _truncate(text: str, limit: int = _WEBEXT_OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]..."


def resolve_amo_credentials(args: argparse.Namespace) -> tuple[str, str]:
    """Resolve AMO JWT credentials without ever exposing their values.

    CLI flags win over ``AMO_JWT_ISSUER`` / ``AMO_JWT_SECRET``. Raises
    ``ValueError`` with a bounded message that contains no secret material
    when either value is missing.
    """
    issuer = getattr(args, "amo_issuer", None) or os.environ.get(AMO_ENV_ISSUER)
    secret = getattr(args, "amo_secret", None) or os.environ.get(AMO_ENV_SECRET)
    if not issuer or not secret:
        missing: list[str] = []
        if not issuer:
            missing.append("--amo-issuer/AMO_JWT_ISSUER")
        if not secret:
            missing.append("--amo-secret/AMO_JWT_SECRET")
        raise ValueError(
            "AMO signing requested but missing credentials: "
            + ", ".join(missing)
            + " (pass flags or export env at runtime; values are never logged)"
        )
    return issuer, secret


def web_ext_sign_argv(extension_dir: str, artifacts_dir: str,
                      issuer: str, secret: str) -> list[str]:
    """Build the fixed ``web-ext sign`` argv (list only, no shell)."""
    return [
        *_WEBEXT_CMD,
        "--channel", "unlisted",
        "--api-key", issuer,
        "--api-secret", secret,
        "--source-dir", extension_dir,
        "--artifacts-dir", artifacts_dir,
    ]


def find_signed_xpi(artifacts_dir: Path) -> Path | None:
    """Locate the signed XPI produced by ``web-ext sign``.

    Picks the lexicographically last ``*.xpi`` so repeated signs are
    deterministic; returns ``None`` when no artifact exists.
    """
    candidates = sorted(Path(artifacts_dir).glob("*.xpi"))
    if not candidates:
        return None
    return candidates[-1]


def copy_xpi_to(src: Path, dst: Path) -> Path:
    """Copy an XPI to its destination (mkdir -p, 0644 best-effort)."""
    src = Path(src)
    dst = Path(dst)
    if not src.is_file():
        raise ValueError(f"signed XPI not found: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(src), str(dst))
    try:
        dst.chmod(0o644)
    except OSError:
        pass
    return dst


def run_web_ext_sign(extension_dir: Path, artifacts_dir: Path,
                     issuer: str, secret: str,
                     timeout: int = 300) -> tuple[int, str, Path | None]:
    """Run ``web-ext sign`` and return ``(code, redacted_output, xpi|None)``.

    Args are passed as a list (no shell interpolation). Both stdout and
    stderr are captured, redacted, truncated, and combined; secrets never
    appear in the returned text. Only ``FileNotFoundError``/``Timeout``/
    non-zero exit are surfaced as failures — this function never claims
    the artifact is signed.
    """
    argv = web_ext_sign_argv(
        str(extension_dir), str(artifacts_dir), issuer, secret)
    secrets = (issuer, secret)
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return 127, "web-ext not available (npx/web-ext missing)", None
    except subprocess.TimeoutExpired as exc:
        partial = ""
        if exc.stdout:
            partial += str(exc.stdout) + "\n"
        if exc.stderr:
            partial += str(exc.stderr)
        partial = _redact_secrets(partial.strip(), secrets)
        return 124, _truncate(f"web-ext sign timed out\n{partial}".strip()), None
    except OSError as exc:
        return 1, _truncate(f"web-ext sign failed to start: {exc}"), None
    combined = ""
    if completed.stdout:
        combined += completed.stdout
    if completed.stderr:
        if combined and not combined.endswith("\n"):
            combined += "\n"
        combined += completed.stderr
    combined = _redact_secrets(combined.strip(), secrets)
    combined = _truncate(combined)
    if completed.returncode != 0:
        return completed.returncode, combined, None
    signed = find_signed_xpi(artifacts_dir)
    if signed is None:
        hint = combined or "(no web-ext output)"
        return 1, _truncate(
            f"web-ext sign reported success but no *.xpi found in "
            f"artifacts dir\n{hint}"
        ), None
    return 0, combined, signed


def default_hosts_dir() -> Path:
    return Path.home() / ".mozilla" / "native-messaging-hosts"


def default_wrapper_dir() -> Path:
    return Path.home() / ".local" / "share" / "quickshell" / "zen-native-host"


def host_manifest_filename() -> str:
    return f"{HOST_NAME}.json"


def load_extension_manifest(extension_dir: Path) -> dict:
    manifest_path = Path(extension_dir) / "manifest.json"
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(
            f"extension manifest not found: {manifest_path} "
            "(pass --extension-dir explicitly)"
        ) from exc
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read extension manifest: {exc}") from exc


def validate_extension_manifest(manifest: object) -> None:
    """Ensure the extension manifest pins the fixed id and MV2 contract.

    Current AMO schema (no legacy ``applications`` key):

    - ``browser_specific_settings.gecko.id`` pins ``EXTENSION_ID``
    - ``browser_specific_settings.gecko.strict_min_version`` requires
      Gecko 115+ (e.g. ``"115.0"``)
    - ``browser_specific_settings.gecko.data_collection_permissions``
      requires exactly ``{"required": ["none"]}`` (exclusive)
    """
    if not isinstance(manifest, dict):
        raise ValueError("extension manifest must be a JSON object")
    if "applications" in manifest:
        raise ValueError(
            "extension manifest must not use legacy 'applications'; "
            "use 'browser_specific_settings.gecko' "
            f"with id {EXTENSION_ID!r}"
        )
    try:
        gecko = manifest["browser_specific_settings"]["gecko"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            "extension manifest must pin browser_specific_settings.gecko.id "
            f"to {EXTENSION_ID!r}"
        ) from exc
    if not isinstance(gecko, dict):
        raise ValueError(
            "extension manifest browser_specific_settings.gecko "
            "must be an object"
        )
    gecko_id = gecko.get("id")
    if gecko_id != EXTENSION_ID:
        raise ValueError(
            f"extension id mismatch: {gecko_id!r} != {EXTENSION_ID!r}"
        )
    min_version = gecko.get("strict_min_version")
    if not isinstance(min_version, str) or not min_version:
        raise ValueError(
            "extension manifest must pin "
            "browser_specific_settings.gecko.strict_min_version "
            "to Gecko 115+ (e.g. '115.0')"
        )
    try:
        major = int(str(min_version).split(".")[0])
    except (ValueError, IndexError) as exc:
        raise ValueError(
            "extension manifest strict_min_version must start "
            f"with a numeric major version >= 115 (got {min_version!r})"
        ) from exc
    if major < 115:
        raise ValueError(
            "extension manifest strict_min_version must be Gecko 115+ "
            f"(got {min_version!r})"
        )
    data_perms = gecko.get("data_collection_permissions")
    if not isinstance(data_perms, dict):
        raise ValueError(
            "extension manifest must declare "
            "browser_specific_settings.gecko.data_collection_permissions "
            "{'required': ['none']}"
        )
    required = data_perms.get("required")
    if required != ["none"]:
        raise ValueError(
            "extension manifest data_collection_permissions.required "
            "must be exactly ['none'] "
            f"(got {required!r})"
        )
    permissions = manifest.get("permissions", [])
    for required in ("tabs", "nativeMessaging"):
        if required not in permissions:
            raise ValueError(
                f"extension manifest must request {required!r} permission"
            )
    scripts = manifest.get("background", {}).get("scripts", [])
    if "background.js" not in scripts:
        raise ValueError("extension manifest background must list background.js")


def check_extension_dir(extension_dir: Path) -> None:
    """Validate layout: manifest id allowlist + required member files."""
    manifest = load_extension_manifest(extension_dir)
    validate_extension_manifest(manifest)
    for member in XPI_MEMBERS:
        candidate = Path(extension_dir) / member
        if not candidate.is_file():
            raise ValueError(f"extension file missing: {candidate}")
    # background.js must reference the fixed host name so connectNative
    # reaches this installer’s registered host.
    background = (Path(extension_dir) / "background.js").read_text(
        encoding="utf-8"
    )
    if HOST_NAME not in background:
        raise ValueError(
            f"background.js must reference host {HOST_NAME!r}"
        )


def build_xpi(extension_dir: Path, xpi_path: Path) -> Path:
    """Package a deterministic unsigned XPI (sorted members, fixed time)."""
    extension_dir = Path(extension_dir)
    xpi_path = Path(xpi_path)
    check_extension_dir(extension_dir)
    xpi_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        str(xpi_path), "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for member in sorted(XPI_MEMBERS):
            data = (extension_dir / member).read_bytes()
            info = zipfile.ZipInfo(filename=member, date_time=_STABLE_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            # Regular file, 0644 — deterministic permissions.
            info.external_attr = (0o644 & 0xFFFF) << 16
            archive.writestr(info, data)
    return xpi_path


def host_manifest_dict(executable_path: str) -> dict:
    """Render the native-messaging host manifest for one executable path."""
    return {
        "name": HOST_NAME,
        "description": (
            "QS Zen Context native host: marker-verified focused-tab "
            "publisher for the desktop-activity collector. No network, "
            "no clipboard."
        ),
        "path": executable_path,
        "type": "stdio",
        "allowed_extensions": [EXTENSION_ID],
    }


def write_host_manifest(manifest_path: Path, executable_path: str) -> Path:
    manifest_path = Path(manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = host_manifest_dict(executable_path)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    manifest_path.write_text(text, encoding="utf-8")
    try:
        manifest_path.chmod(0o644)
    except OSError:
        pass
    return manifest_path


def wrapper_script_text(host_path: str, python_bin: str = PYTHON_BIN) -> str:
    """Render the Flatpak sandbox wrapper.

    The wrapper runs on the *sandbox* side and re-executes the real host
    on the *host* side::

        exec /usr/bin/flatpak-spawn --host /usr/bin/python3 <host> "$@"

    Both the interpreter and the host path are absolute and shell-quoted;
    no caller-controlled interpolation is possible.
    """
    quoted_python = shlex.quote(python_bin)
    quoted_host = shlex.quote(host_path)
    return (
        "#!/bin/sh\n"
        "# Generated by services/agent-orchestrator/integrations/zen/install.py\n"
        "# Flatpak sandbox wrapper: re-executes the real native host on the host.\n"
        f"exec {shlex.quote(FLATPAK_SPAWN)} --host {quoted_python} {quoted_host} \"$@\"\n"
    )


def write_wrapper(wrapper_path: Path, host_path: str,
                  python_bin: str = PYTHON_BIN) -> Path:
    wrapper_path = Path(wrapper_path)
    wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    wrapper_path.write_text(
        wrapper_script_text(host_path, python_bin), encoding="utf-8"
    )
    try:
        wrapper_path.chmod(0o755)
    except OSError:
        pass
    return wrapper_path


def _abspath(user_path: str | Path) -> Path:
    """Expand ``~`` and resolve to an absolute path (non-strict)."""
    return Path(user_path).expanduser().resolve()


def flatpak_override_argv(wrapper_dir: str, hosts_dir: str,
                           app_id: str = FLATPAK_APP_ID) -> list[str]:
    """Exact override command (printed, never executed by this installer).

    Both the wrapper directory *and* the hosts directory are exposed
    read-only so the sandbox can find the manifest *and* resolve the
    wrapper path at the same absolute location. ``--talk-name`` grants
    full host-command execution — the installer only prints this.
    """
    return [
        "flatpak",
        "override",
        "--user",
        "--talk-name=org.freedesktop.Flatpak",
        f"--filesystem={wrapper_dir}:ro",
        f"--filesystem={hosts_dir}:ro",
        app_id,
    ]


def resolve_host_path(raw: str | None) -> Path:
    candidate = Path(raw).expanduser() if raw else DEFAULT_HOST_PATH
    if not candidate.is_file():
        raise ValueError(
            f"native host not found: {candidate} (pass --host-path explicitly)"
        )
    return candidate.resolve()


def resolve_extension_dir(raw: str | None) -> Path:
    candidate = Path(raw).expanduser() if raw else DEFAULT_EXTENSION_DIR
    if not candidate.is_dir():
        raise ValueError(
            f"extension dir not found: {candidate} "
            "(pass --extension-dir explicitly)"
        )
    return candidate.resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Package the QS Zen Context XPI + register the native host."
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Actually write files. Without it, only a dry-run plan is printed.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Force dry-run even if --install was passed (safe default).",
    )
    parser.add_argument(
        "--mode",
        choices=("native", "flatpak"),
        default="native",
        help="Which host registration to prepare: native Zen or Flatpak "
             "app.zen_browser.zen (default: native). No combined mode: the "
             "two need different manifest targets sharing one filename.",
    )
    parser.add_argument(
        "--flatpak",
        action="store_true",
        default=False,
        help="Shorthand for --mode flatpak (explicit Flatpak grants).",
    )
    parser.add_argument("--host-path", default=None,
                        help="Path to qs-zen-native-host.py (default: sibling file).")
    parser.add_argument("--extension-dir", default=None,
                        help="Path to extension/ dir (default: sibling extension/).")
    parser.add_argument("--hosts-dir", default=None,
                        help="Native-messaging hosts dir "
                             "(default: ~/.mozilla/native-messaging-hosts).")
    parser.add_argument("--wrapper-dir", default=None,
                        help="Flatpak wrapper dir "
                             "(default: ~/.local/share/quickshell/zen-native-host).")
    parser.add_argument("--python-bin", default=PYTHON_BIN,
                        help=f"Host python interpreter (default: {PYTHON_BIN}).")
    parser.add_argument("--flatpak-app", default=FLATPAK_APP_ID,
                        help=f"Flatpak app id (default: {FLATPAK_APP_ID}).")
    parser.add_argument("--xpi", default=None,
                        help="Exact XPI output path "
                             "(default: <output-dir>/qs-zen-context.xpi).")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR),
                        help="Stable XPI output directory "
                             "(default: /tmp/opencode). Used when --xpi is absent.")
    parser.add_argument(
        "--sign-amo",
        action="store_true",
        default=False,
        help="Sign via AMO unlisted (npx web-ext sign --channel unlisted). "
             "Credentials come from --amo-issuer/--amo-secret (aliases "
             "--api-key/--api-secret) or AMO_JWT_ISSUER/AMO_JWT_SECRET env; "
             "CLI wins. Secrets are never printed, logged, or written. "
             "On failure returns a bounded redacted error and never claims "
             "the XPI is signed. Mutually exclusive with --signed-xpi.",
    )
    parser.add_argument(
        "--amo-issuer", "--api-key",
        dest="amo_issuer",
        default=None,
        help="AMO JWT issuer (or AMO_JWT_ISSUER env). Value never logged.",
    )
    parser.add_argument(
        "--amo-secret", "--api-secret",
        dest="amo_secret",
        default=None,
        help="AMO JWT secret (or AMO_JWT_SECRET env). Value never logged "
             "or persisted.",
    )
    parser.add_argument(
        "--signed-xpi",
        default=None,
        help="Copy an externally obtained signed XPI directly to the --xpi "
             "destination (skips web-ext; no subprocess, no network). "
             "Mutually exclusive with --sign-amo.",
    )
    return parser


def plan_install(args: argparse.Namespace, host_abs: Path,
                 extension_dir: Path) -> dict:
    """Compute the file plan without touching the filesystem.

    All paths are absolute + resolved (``~``-expanded), including
    user-supplied ``--hosts-dir``/``--wrapper-dir``/``--xpi``.
    Credential values are never included in the plan.
    """
    hosts_dir = (
        _abspath(args.hosts_dir)
        if args.hosts_dir else default_hosts_dir().resolve()
    )
    wrapper_dir = (
        _abspath(args.wrapper_dir)
        if args.wrapper_dir else default_wrapper_dir().resolve()
    )
    mode = args.mode
    if args.flatpak:
        mode = "flatpak"
    if mode not in ("native", "flatpak"):
        raise ValueError(f"unsupported mode: {mode!r}")
    if args.xpi:
        xpi_path = _abspath(args.xpi)
    else:
        xpi_path = _abspath(args.output_dir) / DEFAULT_XPI_NAME
    manifest_path = hosts_dir / host_manifest_filename()
    wrapper_path = wrapper_dir / "qs-zen-native-host-flatpak.sh"
    sign_amo = bool(getattr(args, "sign_amo", False))
    signed_xpi_raw = getattr(args, "signed_xpi", None)
    signed_xpi_src = (
        str(_abspath(signed_xpi_raw)) if signed_xpi_raw else None
    )
    if sign_amo and signed_xpi_src:
        raise ValueError("--sign-amo and --signed-xpi are mutually exclusive")
    if sign_amo:
        signing = "amo"
    elif signed_xpi_src:
        signing = "external"
    else:
        signing = "unsigned"
    return {
        "mode": mode,
        "host_abs": str(host_abs),
        "extension_dir": str(extension_dir),
        "xpi_path": str(xpi_path),
        "hosts_dir": str(hosts_dir),
        "wrapper_dir": str(wrapper_dir),
        "manifest_path": str(manifest_path),
        "wrapper_path": str(wrapper_path),
        "python_bin": args.python_bin,
        "flatpak_app": args.flatpak_app,
        "signing": signing,
        "sign_amo": sign_amo,
        "signed_xpi_src": signed_xpi_src,
    }


def describe_plan(plan: dict) -> str:
    signing = plan.get("signing", "unsigned")
    if signing == "amo":
        xpi_line = f"  XPI output   : {plan['xpi_path']} (AMO signed via web-ext unlisted)"
    elif signing == "external":
        xpi_line = (
            f"  XPI output   : {plan['xpi_path']} "
            f"(signed copy from {plan.get('signed_xpi_src')})"
        )
    else:
        xpi_line = f"  XPI output   : {plan['xpi_path']} (unsigned!)"
    lines = [
        "QS Zen Context install plan (dry-run: nothing written)",
        f"  extension id : {EXTENSION_ID}",
        f"  host name    : {HOST_NAME}",
        f"  mode         : {plan['mode']}",
        f"  signing      : {signing}",
        f"  extension dir: {plan['extension_dir']}",
        f"  host path    : {plan['host_abs']}",
        xpi_line,
    ]
    if signing == "amo":
        lines += [
            "",
            "  AMO signing (unlisted, credentials at runtime only):",
            "    web-ext: npx web-ext sign --channel unlisted "
            "--source-dir <extension-dir> --artifacts-dir <temp> "
            "(api-key/api-secret from flags/env, never shown)",
            "    credentials: --amo-issuer/AMO_JWT_ISSUER + "
            "--amo-secret/AMO_JWT_SECRET (values never logged)",
            "    output is copied to the XPI path above; web-ext output is",
            "    redacted and truncated on failure (never claims signed).",
        ]
    elif signing == "external":
        lines += [
            "",
            "  Signed XPI (external): the file above is copied from the",
            "  operator-supplied signed file (no web-ext, no network).",
        ]
    else:
        lines += [
            "",
            "  Unsigned XPI limitation: install requires either temporary",
            "  loading via about:debugging -> This Firefox -> Load Temporary",
            "  Add-on (session-scoped, manual reload after restart), or",
            "  permanent signing via AMO (addons.mozilla.org). This installer",
            "  never disables signature enforcement.",
        ]
    if signing in ("amo", "external"):
        lines += [
            "",
            "  Signed XPI deployment: install via about:addons -> Install",
            "  Add-on From File, or force-install with an ExtensionSettings",
            "  policy mapping "
            f"'{EXTENSION_ID}' to the signed file:/// URL. Flatpak",
            "  policies.json placement is a documented manual step (never",
            "  written by this installer). This installer never disables",
            "  signature enforcement.",
        ]
    if plan["mode"] == "native":
        lines += [
            "",
            "  native Zen:",
            f"    manifest: {plan['manifest_path']}",
            f"      path -> {plan['host_abs']} (real native-host executable)",
        ]
    if plan["mode"] == "flatpak":
        override = " ".join(
            shlex.quote(part)
            for part in flatpak_override_argv(
                plan["wrapper_dir"], plan["hosts_dir"], plan["flatpak_app"]
            )
        )
        lines += [
            "",
            "  Flatpak app.zen_browser.zen:",
            f"    wrapper : {plan['wrapper_path']} (0755, host-side script)",
            f"    manifest: {plan['manifest_path']}",
            f"      path -> {plan['wrapper_path']} (sandbox-visible wrapper)",
            "    sandbox needs (NOT applied by this installer — run manually):",
            f"      {override}",
            "    WARNING: --talk-name=org.freedesktop.Flatpak grants the",
            "    sandboxed browser full host-command execution via",
            "    flatpak-spawn --host. Both filesystem grants are narrow",
            "    and read-only (<wrapper-dir>:ro and <hosts-dir>:ro) so the",
            "    sandbox can find the manifest and resolve the wrapper.",
            "    Review before applying.",
        ]
    lines += ["", "  Re-run with --install to write files."]
    return "\n".join(lines)


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        host_abs = resolve_host_path(args.host_path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        extension_dir = resolve_extension_dir(args.extension_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        check_extension_dir(extension_dir)
    except ValueError as exc:
        print(f"error: invalid extension: {exc}", file=sys.stderr)
        return 2
    if not Path(args.python_bin).is_absolute():
        print("error: --python-bin must be an absolute path", file=sys.stderr)
        return 2

    try:
        plan = plan_install(args, host_abs, extension_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    do_write = bool(args.install) and not bool(args.dry_run)
    if not do_write:
        print(describe_plan(plan))
        return 0

    # --- explicit install: write only the requested per-user files ---
    signing = plan.get("signing", "unsigned")
    if signing == "external":
        try:
            copy_xpi_to(
                Path(str(plan["signed_xpi_src"])), Path(plan["xpi_path"]))
        except (OSError, ValueError) as exc:
            print(f"error: cannot copy signed XPI: {exc}", file=sys.stderr)
            return 1
        print(f"wrote signed XPI (external): {plan['xpi_path']}")
    elif signing == "amo":
        try:
            issuer, secret = resolve_amo_credentials(args)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        # web-ext signs from the validated source dir; artifacts go to a
        # temp dir and the signed XPI is copied to the requested path.
        try:
            with tempfile.TemporaryDirectory(
                    prefix="qs-zen-sign-") as artifacts_tmp:
                artifacts_dir = Path(artifacts_tmp)
                code, redacted, signed = run_web_ext_sign(
                    extension_dir, artifacts_dir, issuer, secret)
                if code != 0 or signed is None:
                    print("error: web-ext sign failed "
                          f"(exit {code})", file=sys.stderr)
                    if redacted:
                        print(redacted, file=sys.stderr)
                    print("NOTE: XPI was NOT signed; "
                          "nothing was copied.", file=sys.stderr)
                    return 1
                try:
                    copy_xpi_to(signed, Path(plan["xpi_path"]))
                except (OSError, ValueError) as exc:
                    print(f"error: cannot copy signed XPI: {exc}",
                          file=sys.stderr)
                    return 1
                print(f"wrote signed XPI (AMO unlisted): {plan['xpi_path']}")
                if redacted:
                    print(redacted)
        finally:
            # Best-effort: drop credential references immediately.
            issuer = ""
            secret = ""
    else:
        try:
            build_xpi(extension_dir, Path(plan["xpi_path"]))
        except (OSError, ValueError) as exc:
            print(f"error: cannot build XPI: {exc}", file=sys.stderr)
            return 1
        print(f"wrote XPI (unsigned): {plan['xpi_path']}")
        print("NOTE: unsigned XPI needs about:debugging temporary load "
              "or AMO signing.")

    if plan["mode"] == "native":
        try:
            write_host_manifest(
                Path(plan["manifest_path"]), plan["host_abs"]
            )
        except OSError as exc:
            print(f"error: cannot write native manifest: {exc}", file=sys.stderr)
            return 1
        print(f"wrote native manifest: {plan['manifest_path']}")
        print(f"  host path -> {plan['host_abs']}")

    if plan["mode"] == "flatpak":
        try:
            write_wrapper(
                Path(plan["wrapper_path"]),
                plan["host_abs"],
                plan["python_bin"],
            )
        except OSError as exc:
            print(f"error: cannot write wrapper: {exc}", file=sys.stderr)
            return 1
        try:
            # The registered manifest path is the sandbox-visible wrapper;
            # host and sandbox share the absolute path via the :ro overrides
            # (both wrapper-dir and hosts-dir are exposed read-only).
            write_host_manifest(
                Path(plan["manifest_path"]), plan["wrapper_path"]
            )
        except OSError as exc:
            print(f"error: cannot write flatpak manifest: {exc}", file=sys.stderr)
            return 1
        print(f"wrote wrapper: {plan['wrapper_path']}")
        print(f"wrote flatpak manifest: {plan['manifest_path']}")
        override = " ".join(
            shlex.quote(part)
            for part in flatpak_override_argv(
                plan["wrapper_dir"], plan["hosts_dir"], plan["flatpak_app"]
            )
        )
        print("Flatpak sandbox setup (NOT applied — run manually after review):")
        print(f"  {override}")
        print(
            "WARNING: --talk-name=org.freedesktop.Flatpak grants the "
            "sandboxed browser full host-command execution."
        )
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
