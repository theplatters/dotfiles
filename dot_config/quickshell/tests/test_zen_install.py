"""Tests for the Zen XPI + native-host installer (dry-run safe, mocked I/O).

No real ``flatpak override`` is ever executed, no live browser/profile is
touched, and no signing prefs are mutated: every install targets temporary
directories and ``subprocess`` is patched to fail the test if invoked.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
ZEN_DIR = ROOT / "services" / "agent-orchestrator" / "integrations" / "zen"
INSTALL_PATH = ZEN_DIR / "install.py"
EXT_DIR = ZEN_DIR / "extension"
HOST_PATH = ZEN_DIR / "qs-zen-native-host.py"


def _load_install():
    old_flag = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(
            "zen_install_under_test", str(INSTALL_PATH)
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = old_flag


install = _load_install()

EXTENSION_ID = "qs-zen-context@quickshell.local"
HOST_NAME = "local.quickshell.zen_context"


def _run(argv):
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = install.run(list(argv))
    return code, out.getvalue(), err.getvalue()


class ContractTests(unittest.TestCase):
    def test_fixed_ids(self):
        self.assertEqual(install.EXTENSION_ID, EXTENSION_ID)
        self.assertEqual(install.HOST_NAME, HOST_NAME)

    def test_installer_never_disables_signing(self):
        source = INSTALL_PATH.read_text(encoding="utf-8")
        lowered = source.lower()
        # Documentary "never touches ..." mentions are allowed; what is
        # forbidden is any code that would mutate signing prefs/profile DB.
        for banned in (
            "prefs.js",
            "user_pref",
            "signatures.required\", false",
            "signatures.required', false",
            "signatures.required\",false",
            "signatures.required\", 0",
        ):
            self.assertNotIn(banned, lowered)
        self.assertNotIn("services.sync.prefs", lowered)
        # Must explicitly disclaim enforcement handling + unsigned status,
        # and never claim the artifact ships signed.
        self.assertRegex(lowered, r"never.*(disables|touches).*sign")
        self.assertRegex(source, r"[Uu]nsigned")
        for false_claim in ("already signed", "is signed by", "pre-signed",
                            "ships signed", "signed artifact"):
            self.assertNotIn(false_claim, lowered)

    def test_installer_never_executes_flatpak_override(self):
        source = INSTALL_PATH.read_text(encoding="utf-8")
        # The only subprocess ever allowed is `npx web-ext sign` for
        # explicit --sign-amo; flatpak override must only be printed.
        self.assertNotIn("os.system", source)
        self.assertNotIn("os.exec", source)
        self.assertNotIn("os.spawn", source)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("shell = True", source)
        # subprocess must exist solely for web-ext signing.
        self.assertIn("subprocess.run", source)
        self.assertIn("web-ext", source)
        self.assertIn("run_web_ext_sign", source)
        # The flatpak override builder must never be wired to subprocess:
        # no line passes flatpak argv to subprocess/Popen, and no
        # `flatpak ... override` execution string is constructed for run.
        for line in source.splitlines():
            lowered = line.lower()
            if "subprocess" in lowered or "popen" in lowered:
                self.assertNotIn("flatpak", lowered,
                                 msg=f"subprocess must not run flatpak: {line!r}")
                self.assertNotIn("override", lowered,
                                 msg=f"subprocess must not run override: {line!r}")
        # web-ext invocation itself must be list-only, unlisted channel.
        self.assertIn('"--channel"', source)
        self.assertIn('"unlisted"', source)

    def test_default_is_dry_run(self):
        parser = install.build_parser()
        args = parser.parse_args([])
        self.assertFalse(args.install)
        # Without --install, run() must not write even with valid dirs.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            xpi = tmp_path / "out.xpi"
            code, _, _ = _run([
                "--host-path", str(HOST_PATH),
                "--extension-dir", str(EXT_DIR),
                "--hosts-dir", str(tmp_path / "hosts"),
                "--xpi", str(xpi),
            ])
            self.assertEqual(code, 0)
            self.assertFalse(xpi.exists())
            self.assertFalse((tmp_path / "hosts").exists())


class PackagingTests(unittest.TestCase):
    def test_build_xpi_contains_manifest_and_background(self):
        with tempfile.TemporaryDirectory() as tmp:
            xpi = Path(tmp) / "qs-zen-context.xpi"
            install.build_xpi(EXT_DIR, xpi)
            with zipfile.ZipFile(str(xpi)) as archive:
                self.assertEqual(sorted(archive.namelist()),
                                 ["background.js", "manifest.json"])
                manifest = json.loads(archive.read("manifest.json"))
                self.assertNotIn("applications", manifest)
                gecko = manifest["browser_specific_settings"]["gecko"]
                self.assertEqual(gecko["id"], EXTENSION_ID)
                self.assertEqual(
                    gecko["data_collection_permissions"]["required"],
                    ["none"])
                major = int(str(gecko["strict_min_version"]).split(".")[0])
                self.assertGreaterEqual(major, 115)
                self.assertEqual(manifest["version"], "0.1.1")
                background = archive.read("background.js").decode("utf-8")
                self.assertIn(HOST_NAME, background)

    def test_build_xpi_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.xpi"
            second = Path(tmp) / "b.xpi"
            install.build_xpi(EXT_DIR, first)
            install.build_xpi(EXT_DIR, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_build_xpi_rejects_wrong_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "ext"
            fake.mkdir()
            manifest = json.loads((EXT_DIR / "manifest.json").read_text())
            manifest["browser_specific_settings"]["gecko"]["id"] = "evil@example.com"
            (fake / "manifest.json").write_text(json.dumps(manifest))
            (fake / "background.js").write_text(
                (EXT_DIR / "background.js").read_text())
            with self.assertRaises(ValueError):
                install.build_xpi(fake, Path(tmp) / "out.xpi")

    def test_build_xpi_rejects_legacy_applications(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "ext"
            fake.mkdir()
            manifest = json.loads((EXT_DIR / "manifest.json").read_text())
            manifest["applications"] = {
                "gecko": {"id": EXTENSION_ID}}
            (fake / "manifest.json").write_text(json.dumps(manifest))
            (fake / "background.js").write_text(
                (EXT_DIR / "background.js").read_text())
            with self.assertRaises(ValueError):
                install.build_xpi(fake, Path(tmp) / "out.xpi")

    def test_build_xpi_rejects_missing_or_non_none_data_collection(self):
        for mutate in (
            lambda m: m["browser_specific_settings"]["gecko"].pop(
                "data_collection_permissions"),
            lambda m: m["browser_specific_settings"]["gecko"].__setitem__(
                "data_collection_permissions", {"required": []}),
            lambda m: m["browser_specific_settings"]["gecko"].__setitem__(
                "data_collection_permissions",
                {"required": ["none", "technical"]}),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                fake = Path(tmp) / "ext"
                fake.mkdir()
                manifest = json.loads(
                    (EXT_DIR / "manifest.json").read_text())
                mutate(manifest)
                (fake / "manifest.json").write_text(json.dumps(manifest))
                (fake / "background.js").write_text(
                    (EXT_DIR / "background.js").read_text())
                with self.assertRaises(ValueError):
                    install.build_xpi(fake, Path(tmp) / "out.xpi")

    def test_build_xpi_rejects_missing_gecko_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "ext"
            fake.mkdir()
            manifest = json.loads((EXT_DIR / "manifest.json").read_text())
            manifest["browser_specific_settings"]["gecko"].pop(
                "strict_min_version")
            (fake / "manifest.json").write_text(json.dumps(manifest))
            (fake / "background.js").write_text(
                (EXT_DIR / "background.js").read_text())
            with self.assertRaises(ValueError):
                install.build_xpi(fake, Path(tmp) / "out.xpi")

    def test_install_writes_xpi_and_native_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            hosts = tmp_path / "hosts"
            xpi = tmp_path / "opencode" / "qs-zen-context.xpi"
            with mock.patch.object(
                subprocess, "run",
                side_effect=AssertionError("must not shell out"),
            ):
                code, out, _ = _run([
                    "--install",
                    "--host-path", str(HOST_PATH),
                    "--extension-dir", str(EXT_DIR),
                    "--hosts-dir", str(hosts),
                    "--xpi", str(xpi),
                ])
            self.assertEqual(code, 0, msg=out)
            self.assertTrue(xpi.is_file())
            manifest_path = hosts / f"{HOST_NAME}.json"
            self.assertTrue(manifest_path.is_file())
            payload = json.loads(manifest_path.read_text())
            self.assertEqual(payload["name"], HOST_NAME)
            self.assertEqual(payload["allowed_extensions"], [EXTENSION_ID])
            # Native mode points at the real host executable.
            self.assertEqual(payload["path"], str(HOST_PATH.resolve()))
            self.assertEqual(payload["type"], "stdio")


class FlatpakTests(unittest.TestCase):
    def test_wrapper_uses_flatpak_spawn_host_and_quotes_paths(self):
        tricky = "/tmp/opencode dir/with 'quote'/qs-zen-native-host.py"
        text = install.wrapper_script_text(tricky, "/usr/bin/python3")
        self.assertTrue(text.startswith("#!/bin/sh"))
        self.assertIn("/usr/bin/flatpak-spawn", text)
        self.assertIn("--host", text)
        self.assertIn("/usr/bin/python3", text)
        self.assertIn('"$@"', text)
        # Quoting must round-trip through shlex: last tokens are the
        # interpreter, host, and "$@" (no word splitting on spaces/quotes).
        import shlex as _shlex
        last_line = text.strip().splitlines()[-1]
        # `exec A --host B C "$@"` -> tokens [exec, A, --host, B, C, $@]
        tokens = _shlex.split(last_line)
        self.assertEqual(tokens[0], "exec")
        self.assertEqual(tokens[1], "/usr/bin/flatpak-spawn")
        self.assertEqual(tokens[2], "--host")
        self.assertEqual(tokens[3], "/usr/bin/python3")
        self.assertEqual(tokens[4], tricky)
        self.assertEqual(tokens[5], "$@")

    def test_override_command_is_narrow_and_talk_name_warned(self):
        argv = install.flatpak_override_argv(
            "/home/u/.local/share/qs dir",
            "/home/u/.mozilla/native-messaging-hosts",
            "app.zen_browser.zen",
        )
        self.assertIn("--talk-name=org.freedesktop.Flatpak", argv)
        self.assertIn("--filesystem=/home/u/.local/share/qs dir:ro", argv)
        self.assertIn(
            "--filesystem=/home/u/.mozilla/native-messaging-hosts:ro", argv
        )
        self.assertIn("app.zen_browser.zen", argv)
        self.assertIn("--user", argv)
        self.assertEqual(argv[0:2], ["flatpak", "override"])
        # --flatpak shorthand maps to flatpak mode in the plan (never implicit:
        # explicit flag required).
        parser = install.build_parser()
        args = parser.parse_args(["--flatpak"])
        self.assertTrue(args.flatpak)
        plan = install.plan_install(args, HOST_PATH.resolve(), EXT_DIR.resolve())
        self.assertEqual(plan["mode"], "flatpak")

    def test_override_exposes_hosts_dir_for_manifest_visibility(self):
        # Regression: exposing only wrapper_dir leaves the sandbox unable
        # to find the manifest. The printed command must grant both dirs.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            code, out, _ = _run([
                "--mode", "flatpak",
                "--host-path", str(HOST_PATH),
                "--extension-dir", str(EXT_DIR),
                "--hosts-dir", str(tmp_path / "hosts"),
                "--wrapper-dir", str(tmp_path / "wrap"),
                "--xpi", str(tmp_path / "out.xpi"),
            ])
            self.assertEqual(code, 0)
            # Both narrow read-only grants appear in the visibility command.
            self.assertIn(f"--filesystem={tmp_path.resolve() / 'wrap'}:ro", out)
            self.assertIn(f"--filesystem={tmp_path.resolve() / 'hosts'}:ro", out)
            self.assertIn("--talk-name=org.freedesktop.Flatpak", out)

    def test_no_combined_both_mode(self):
        # Native and Flatpak need different manifest targets sharing one
        # filename; a combined mode would silently overwrite one with the
        # other. The parser must reject it.
        parser = install.build_parser()
        self.assertNotIn("both", parser.parse_args(["--mode", "native"]).mode)
        with self.assertRaises(SystemExit):
            parser.parse_args(["--mode", "both"])
        source = INSTALL_PATH.read_text(encoding="utf-8")
        self.assertNotIn('"both"', source)
        self.assertNotIn("'both'", source)
        self.assertNotIn("manifest_native", source)
        self.assertNotIn("manifest_flatpak_path", source)

    def test_flatpak_install_writes_wrapper_and_manifest_without_live_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            hosts = tmp_path / "hosts"
            wrapper_dir = tmp_path / "wrap dir"
            xpi = tmp_path / "qs-zen-context.xpi"
            with mock.patch.object(
                subprocess, "run",
                side_effect=AssertionError("flatpak override must not run"),
            ), mock.patch.object(
                subprocess, "Popen",
                side_effect=AssertionError("flatpak override must not run"),
            ):
                code, out, _ = _run([
                    "--install", "--mode", "flatpak",
                    "--host-path", str(HOST_PATH),
                    "--extension-dir", str(EXT_DIR),
                    "--hosts-dir", str(hosts),
                    "--wrapper-dir", str(wrapper_dir),
                    "--xpi", str(xpi),
                ])
            self.assertEqual(code, 0, msg=out)
            wrapper = wrapper_dir.resolve() / "qs-zen-native-host-flatpak.sh"
            self.assertTrue(wrapper.is_file())
            text = wrapper.read_text()
            self.assertIn("flatpak-spawn --host", text)
            payload = json.loads(
                (hosts.resolve() / f"{HOST_NAME}.json").read_text())
            # Flatpak manifest path is the sandbox-visible wrapper.
            self.assertEqual(payload["path"], str(wrapper))
            # The override is printed for manual review, never executed.
            self.assertIn("flatpak override", out)
            self.assertIn("--talk-name=org.freedesktop.Flatpak", out)
            self.assertIn(f"--filesystem={wrapper_dir.resolve()}:ro", out)
            self.assertIn(f"--filesystem={hosts.resolve()}:ro", out)
            self.assertIn("WARNING", out)
            self.assertIn("NOT applied", out)

    def test_flatpak_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            code, _, _ = _run([
                "--mode", "flatpak",
                "--host-path", str(HOST_PATH),
                "--extension-dir", str(EXT_DIR),
                "--hosts-dir", str(tmp_path / "hosts"),
                "--wrapper-dir", str(tmp_path / "wrap"),
                "--xpi", str(tmp_path / "out.xpi"),
            ])
            self.assertEqual(code, 0)
            self.assertFalse((tmp_path / "out.xpi").exists())
            self.assertFalse((tmp_path / "hosts").exists())
            self.assertFalse((tmp_path / "wrap").exists())


class CliTests(unittest.TestCase):
    def test_missing_host_reports_explicit_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = _run([
                "--host-path", str(Path(tmp) / "nope.py"),
                "--extension-dir", str(EXT_DIR),
            ])
            self.assertEqual(code, 2)
            self.assertIn("--host-path", err)

    def test_missing_extension_reports_explicit_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = _run([
                "--host-path", str(HOST_PATH),
                "--extension-dir", str(Path(tmp) / "nope"),
            ])
            self.assertEqual(code, 2)
            self.assertIn("--extension-dir", err)

    def test_unsigned_limitation_is_surfaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, out, _ = _run([
                "--host-path", str(HOST_PATH),
                "--extension-dir", str(EXT_DIR),
                "--hosts-dir", str(Path(tmp) / "h"),
            ])
            self.assertIn("about:debugging", out)
            self.assertIn("AMO", out)

    def test_plan_paths_are_absolute_and_resolved(self):
        # Defaults and user-supplied dirs (incl. ~ and relative forms)
        # must resolve to absolute paths so the manifest/wrapper/override
        # never carry a relative or unexpanded entry.
        parser = install.build_parser()
        args = parser.parse_args([
            "--host-path", str(HOST_PATH),
            "--extension-dir", str(EXT_DIR),
            "--hosts-dir", str(Path("~/zen-hosts-test")),
            "--wrapper-dir", str(Path("~/zen-wrap-test")),
        ])
        plan = install.plan_install(
            args, HOST_PATH.resolve(), EXT_DIR.resolve())
        for key in ("hosts_dir", "wrapper_dir", "xpi_path",
                    "manifest_path", "wrapper_path"):
            self.assertTrue(Path(plan[key]).is_absolute(), msg=key)
            self.assertNotIn("~", plan[key])
        self.assertEqual(
            plan["hosts_dir"], str(Path("~/zen-hosts-test").expanduser().resolve()))
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            args2 = parser.parse_args([
                "--host-path", str(HOST_PATH),
                "--extension-dir", str(EXT_DIR),
                "--hosts-dir", str(tmp_path / "h"),
                "--wrapper-dir", str(tmp_path / "w"),
                "--xpi", str(tmp_path / "o.xpi"),
            ])
            plan2 = install.plan_install(
                args2, HOST_PATH.resolve(), EXT_DIR.resolve())
            self.assertEqual(plan2["hosts_dir"], str((tmp_path / "h").resolve()))
            self.assertEqual(plan2["wrapper_dir"], str((tmp_path / "w").resolve()))
            self.assertEqual(plan2["manifest_path"],
                             str((tmp_path / "h").resolve() / f"{HOST_NAME}.json"))


class SigningTests(unittest.TestCase):
    FAKE_ISSUER = "TEST-AMO-ISSUER-9f8b7a6c5d"
    FAKE_SECRET = "TEST-AMO-SECRET-1a2b3c4d5e6f"

    def _assert_no_secrets(self, *texts):
        for text in texts:
            self.assertNotIn(self.FAKE_ISSUER, text)
            self.assertNotIn(self.FAKE_SECRET, text)

    def _scan_tree_for_secrets(self, root: Path):
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    content = path.read_text(encoding="utf-8", errors="strict")
                except (UnicodeDecodeError, OSError):
                    # Binary XPI payloads in these tests are ASCII-safe;
                    # fall back to bytes search.
                    try:
                        raw = path.read_bytes()
                    except OSError:
                        continue
                    self.assertNotIn(self.FAKE_ISSUER.encode(), raw)
                    self.assertNotIn(self.FAKE_SECRET.encode(), raw)
                    continue
                self.assertNotIn(self.FAKE_ISSUER, content,
                                 msg=f"secret leak in {path}")
                self.assertNotIn(self.FAKE_SECRET, content,
                                 msg=f"secret leak in {path}")

    def test_web_ext_argv_is_list_no_shell_unlisted(self):
        argv = install.web_ext_sign_argv(
            "/tmp/ext", "/tmp/art", self.FAKE_ISSUER, self.FAKE_SECRET)
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[:3], ["npx", "web-ext", "sign"])
        self.assertIn("--channel", argv)
        self.assertEqual(argv[argv.index("--channel") + 1], "unlisted")
        self.assertIn("--source-dir", argv)
        self.assertIn("--artifacts-dir", argv)
        # Secrets are separate argv items (no shell interpolation).
        self.assertIn(self.FAKE_ISSUER, argv)
        self.assertIn(self.FAKE_SECRET, argv)
        joined = " ".join(argv)
        # No shell metacharacters introduced by the builder itself.
        self.assertNotIn(";", joined)
        self.assertNotIn("&&", joined)

    def test_redact_secrets_replaces_and_ignores_empty(self):
        text = f"key={self.FAKE_ISSUER} secret={self.FAKE_SECRET} ok"
        redacted = install._redact_secrets(
            text, (self.FAKE_ISSUER, self.FAKE_SECRET))
        self.assertNotIn(self.FAKE_ISSUER, redacted)
        self.assertNotIn(self.FAKE_SECRET, redacted)
        self.assertIn(install.REDACTED, redacted)
        # Empty values must not wipe the output.
        self.assertEqual(
            install._redact_secrets("hello", ("",)), "hello")

    def test_sign_amo_dry_run_needs_no_credentials_and_leaks_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = {k: v for k, v in os.environ.items()
                   if k not in ("AMO_JWT_ISSUER", "AMO_JWT_SECRET")}
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                subprocess, "run",
                side_effect=AssertionError("dry-run must not shell out"),
            ):
                code, out, err = _run([
                    "--host-path", str(HOST_PATH),
                    "--extension-dir", str(EXT_DIR),
                    "--hosts-dir", str(tmp_path / "hosts"),
                    "--xpi", str(tmp_path / "out.xpi"),
                    "--sign-amo",
                ])
            self.assertEqual(code, 0)
            self.assertIn("signing", out.lower())
            self.assertIn("amo", out.lower())
            self.assertFalse((tmp_path / "out.xpi").exists())
            self.assertFalse((tmp_path / "hosts").exists())
            # Dry-run with explicit secrets must not echo them either.
            with mock.patch.object(
                subprocess, "run",
                side_effect=AssertionError("dry-run must not shell out"),
            ):
                code2, out2, err2 = _run([
                    "--host-path", str(HOST_PATH),
                    "--extension-dir", str(EXT_DIR),
                    "--hosts-dir", str(tmp_path / "hosts2"),
                    "--xpi", str(tmp_path / "out2.xpi"),
                    "--sign-amo",
                    "--amo-issuer", self.FAKE_ISSUER,
                    "--amo-secret", self.FAKE_SECRET,
                ])
            self.assertEqual(code2, 0)
            self._assert_no_secrets(out2, err2)
            parser = install.build_parser()
            args = parser.parse_args([
                "--sign-amo",
                "--amo-issuer", self.FAKE_ISSUER,
                "--amo-secret", self.FAKE_SECRET,
            ])
            plan = install.plan_install(
                args, HOST_PATH.resolve(), EXT_DIR.resolve())
            self._assert_no_secrets(
                json.dumps(plan), install.describe_plan(plan))

    def test_sign_amo_missing_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = {k: v for k, v in os.environ.items()
                   if k not in ("AMO_JWT_ISSUER", "AMO_JWT_SECRET")}
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                subprocess, "run",
                side_effect=AssertionError("must not shell out without creds"),
            ):
                code, out, err = _run([
                    "--install",
                    "--sign-amo",
                    "--host-path", str(HOST_PATH),
                    "--extension-dir", str(EXT_DIR),
                    "--hosts-dir", str(tmp_path / "hosts"),
                    "--xpi", str(tmp_path / "out.xpi"),
                ])
            self.assertEqual(code, 2)
            self.assertIn("missing credentials", err.lower())
            self._assert_no_secrets(out, err)
            self.assertFalse((tmp_path / "out.xpi").exists())

    def _fake_success_run(self, payload: bytes = b"SIGNED-XPI-BYTES"):
        issuer, secret = self.FAKE_ISSUER, self.FAKE_SECRET

        def _fake(argv, **kwargs):
            self.assertIsInstance(argv, list)
            self.assertEqual(argv[:3], ["npx", "web-ext", "sign"])
            self.assertIn("--channel", argv)
            self.assertEqual(argv[argv.index("--channel") + 1], "unlisted")
            self.assertTrue(kwargs.get("capture_output"))
            self.assertTrue(kwargs.get("text"))
            self.assertNotIn("shell", kwargs)
            artifacts = Path(argv[argv.index("--artifacts-dir") + 1])
            artifacts.mkdir(parents=True, exist_ok=True)
            (artifacts / "qs-zen-context-0.1.1.xpi").write_bytes(payload)
            # Echo secrets to prove redaction downstream.
            return subprocess.CompletedProcess(
                argv, 0,
                stdout=f"Signed {issuer} ok\n",
                stderr=f"token {secret}\n",
            )
        return _fake

    def test_sign_amo_success_copies_signed_xpi_and_manifest_unchanged(self):
        payload = b"SIGNED-XPI-BY-AMO-TEST"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            hosts = tmp_path / "hosts"
            xpi = tmp_path / "out.xpi"
            with mock.patch.object(
                subprocess, "run", side_effect=self._fake_success_run(payload)
            ):
                code, out, err = _run([
                    "--install",
                    "--sign-amo",
                    "--amo-issuer", self.FAKE_ISSUER,
                    "--amo-secret", self.FAKE_SECRET,
                    "--host-path", str(HOST_PATH),
                    "--extension-dir", str(EXT_DIR),
                    "--hosts-dir", str(hosts),
                    "--xpi", str(xpi),
                ])
            self.assertEqual(code, 0, msg=f"{out}\n{err}")
            self.assertTrue(xpi.is_file())
            self.assertEqual(xpi.read_bytes(), payload)
            self.assertIn("wrote signed XPI (AMO unlisted)", out)
            self.assertNotIn("NOT signed", out + err)
            self._assert_no_secrets(out, err)
            # Host manifest is identical to the unsigned flow (mode native).
            manifest_path = hosts.resolve() / f"{HOST_NAME}.json"
            self.assertTrue(manifest_path.is_file())
            got = json.loads(manifest_path.read_text())
            self.assertEqual(got, install.host_manifest_dict(
                str(HOST_PATH.resolve())))
            self._scan_tree_for_secrets(tmp_path)

    def test_sign_amo_success_via_env(self):
        payload = b"SIGNED-VIA-ENV-TEST"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            hosts = tmp_path / "hosts"
            xpi = tmp_path / "out.xpi"
            env = dict(os.environ)
            env["AMO_JWT_ISSUER"] = self.FAKE_ISSUER
            env["AMO_JWT_SECRET"] = self.FAKE_SECRET
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                subprocess, "run", side_effect=self._fake_success_run(payload)
            ):
                code, out, err = _run([
                    "--install", "--sign-amo",
                    "--host-path", str(HOST_PATH),
                    "--extension-dir", str(EXT_DIR),
                    "--hosts-dir", str(hosts),
                    "--xpi", str(xpi),
                ])
            self.assertEqual(code, 0, msg=f"{out}\n{err}")
            self.assertEqual(xpi.read_bytes(), payload)
            self._assert_no_secrets(out, err)
            self._scan_tree_for_secrets(tmp_path)

    def test_sign_amo_failure_redacts_and_never_claims_signed(self):
        issuer, secret = self.FAKE_ISSUER, self.FAKE_SECRET

        def _fail(argv, **kwargs):
            self.assertIsInstance(argv, list)
            return subprocess.CompletedProcess(
                argv, 1,
                stdout=f"401 bad key {issuer}\n" + "x" * 5000,
                stderr=f"denied {secret}\n",
            )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            hosts = tmp_path / "hosts"
            xpi = tmp_path / "out.xpi"
            with mock.patch.object(subprocess, "run", side_effect=_fail):
                code, out, err = _run([
                    "--install", "--sign-amo",
                    "--amo-issuer", issuer,
                    "--amo-secret", secret,
                    "--host-path", str(HOST_PATH),
                    "--extension-dir", str(EXT_DIR),
                    "--hosts-dir", str(hosts),
                    "--xpi", str(xpi),
                ])
            self.assertEqual(code, 1)
            self._assert_no_secrets(out, err)
            self.assertIn(install.REDACTED, err)
            self.assertIn("web-ext sign failed", err)
            self.assertIn("NOT signed", err)
            combined = out + err
            self.assertNotIn("wrote signed XPI", combined)
            # Bounded error: surfaced text stays small despite 5k output.
            self.assertLess(len(combined), 6000)
            self._scan_tree_for_secrets(tmp_path)

    def test_signed_xpi_external_copies_without_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "signed-src.xpi"
            src.write_bytes(b"EXTERNAL-SIGNED-TEST")
            hosts = tmp_path / "hosts"
            dst = tmp_path / "nested" / "qs-zen-context.xpi"
            with mock.patch.object(
                subprocess, "run",
                side_effect=AssertionError("--signed-xpi must not shell out"),
            ), mock.patch.object(
                subprocess, "Popen",
                side_effect=AssertionError("--signed-xpi must not shell out"),
            ):
                code, out, err = _run([
                    "--install",
                    "--signed-xpi", str(src),
                    "--host-path", str(HOST_PATH),
                    "--extension-dir", str(EXT_DIR),
                    "--hosts-dir", str(hosts),
                    "--xpi", str(dst),
                ])
            self.assertEqual(code, 0, msg=f"{out}\n{err}")
            self.assertTrue(dst.is_file())
            self.assertEqual(dst.read_bytes(), b"EXTERNAL-SIGNED-TEST")
            self.assertIn("wrote signed XPI (external)", out)
            payload = json.loads(
                (hosts.resolve() / f"{HOST_NAME}.json").read_text())
            self.assertEqual(payload, install.host_manifest_dict(
                str(HOST_PATH.resolve())))
            # Dry-run variant writes nothing and never shells out.
            with mock.patch.object(
                subprocess, "run",
                side_effect=AssertionError("dry-run must not shell out"),
            ):
                code2, _, _ = _run([
                    "--signed-xpi", str(src),
                    "--host-path", str(HOST_PATH),
                    "--extension-dir", str(EXT_DIR),
                    "--hosts-dir", str(tmp_path / "hosts-dry"),
                    "--xpi", str(tmp_path / "dry.xpi"),
                ])
            self.assertEqual(code2, 0)
            self.assertFalse((tmp_path / "dry.xpi").exists())

    def test_plan_never_contains_secrets_and_mutual_exclusion(self):
        parser = install.build_parser()
        args = parser.parse_args([
            "--sign-amo",
            "--amo-issuer", self.FAKE_ISSUER,
            "--amo-secret", self.FAKE_SECRET,
        ])
        plan = install.plan_install(
            args, HOST_PATH.resolve(), EXT_DIR.resolve())
        self.assertEqual(plan["signing"], "amo")
        self._assert_no_secrets(
            json.dumps(plan), install.describe_plan(plan))
        # External mode plan carries the path but never secrets.
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "s.xpi"
            src.write_bytes(b"x")
            args2 = parser.parse_args(["--signed-xpi", str(src)])
            plan2 = install.plan_install(
                args2, HOST_PATH.resolve(), EXT_DIR.resolve())
            self.assertEqual(plan2["signing"], "external")
            self._assert_no_secrets(
                json.dumps(plan2), install.describe_plan(plan2))
        # Mutually exclusive flags are rejected without leaking values.
        args3 = parser.parse_args([
            "--sign-amo",
            "--amo-issuer", self.FAKE_ISSUER,
            "--amo-secret", self.FAKE_SECRET,
            "--signed-xpi", str(EXT_DIR / "manifest.json"),
        ])
        with self.assertRaises(ValueError):
            install.plan_install(
                args3, HOST_PATH.resolve(), EXT_DIR.resolve())
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            code, out, err = _run([
                "--install", "--sign-amo",
                "--signed-xpi", str(EXT_DIR / "manifest.json"),
                "--host-path", str(HOST_PATH),
                "--extension-dir", str(EXT_DIR),
                "--hosts-dir", str(tmp_path / "h"),
                "--xpi", str(tmp_path / "o.xpi"),
            ])
            self.assertEqual(code, 2)
            self._assert_no_secrets(out, err)


if __name__ == "__main__":
    unittest.main()
