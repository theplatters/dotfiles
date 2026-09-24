"""Tests for the shared CLI library (scripts/qscli.py).

Pure stdlib, offline: parser non-echo, emit caps, read_input
bounds/dict-only, run_main passthrough/bounded/fallback paths, and
global-flag registration. No helper imports (import-cycle guard).
"""

import argparse
import ast
import contextlib
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import qscli


class FakeStdout:
    def __init__(self):
        self.buffer = io.BytesIO()


@contextlib.contextmanager
def fake_stdio(stdin=None, stdout=None):
    old_in, old_out = sys.stdin, sys.stdout
    if stdin is not None:
        sys.stdin = stdin
    if stdout is not None:
        sys.stdout = stdout
    try:
        yield
    finally:
        sys.stdin, sys.stdout = old_in, old_out


class StdlibOnlyCase(unittest.TestCase):
    def test_imports_are_stdlib_only(self):
        tree = ast.parse((ROOT / "scripts" / "qscli.py").read_text())
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    roots.add(node.module.split(".")[0])
        self.assertLessEqual(roots, {"__future__", "argparse", "json",
                                   "sys"})

    def test_input_limit_is_1mib(self):
        self.assertEqual(qscli.INPUT_LIMIT, 1024 * 1024)


class SafeParserCase(unittest.TestCase):
    def _parser(self):
        parser = qscli.SafeParser(description="test")
        parser.add_argument("--limit", default=None)
        sub = parser.add_subparsers(dest="command", required=True,
                                    parser_class=qscli.SafeParser)
        sub.add_parser("list")
        return parser

    def test_unknown_flag_never_echoed(self):
        secret = "SECRET-MARKER-9f8eqscli"
        parser = self._parser()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(ValueError) as ctx:
                parser.parse_args(["--unknown", secret])
        self.assertEqual(str(ctx.exception), "invalid arguments")
        self.assertNotIn(secret, err.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_unknown_command_never_echoed(self):
        secret = "SECRET-MARKER-4b2eqscli"
        parser = self._parser()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(ValueError) as ctx:
                parser.parse_args([secret])
        self.assertEqual(str(ctx.exception), "invalid arguments")
        self.assertNotIn(secret, err.getvalue())

    def test_missing_required_never_echoed(self):
        parser = self._parser()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(ValueError) as ctx:
                parser.parse_args([])
        self.assertEqual(str(ctx.exception), "invalid arguments")
        self.assertEqual(err.getvalue(), "")

    def test_help_still_exits_zero(self):
        parser = self._parser()
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                parser.parse_args(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_valid_argv_parses(self):
        args = self._parser().parse_args(["list"])
        self.assertEqual(args.command, "list")


class EmitCase(unittest.TestCase):
    def _emit_to_bytes(self, value):
        out = FakeStdout()
        with fake_stdio(stdout=out):
            qscli.emit(value)
        return out.buffer.getvalue()

    def test_sorted_compact_single_line(self):
        raw = self._emit_to_bytes({"b": 1, "a": [1, 2]})
        self.assertEqual(raw, b'{"a":[1,2],"b":1}\n')

    def test_non_ascii_not_escaped(self):
        raw = self._emit_to_bytes({"q": "ä"})
        self.assertEqual(raw, '{"q":"ä"}\n'.encode("utf-8"))

    def test_unserializable_capped(self):
        with self.assertRaises(ValueError) as ctx:
            self._emit_to_bytes({"bad": object()})
        self.assertEqual(str(ctx.exception), "JSON output is too large")

    def test_oversize_capped(self):
        big = {"k": "x" * (qscli.INPUT_LIMIT + 1)}
        with self.assertRaises(ValueError) as ctx:
            self._emit_to_bytes(big)
        self.assertEqual(str(ctx.exception), "JSON output is too large")

    def test_circular_capped(self):
        loop = []
        loop.append(loop)
        with self.assertRaises(ValueError) as ctx:
            self._emit_to_bytes({"loop": loop})
        self.assertEqual(str(ctx.exception), "JSON output is too large")


class ReadInputCase(unittest.TestCase):
    def _read(self, data):
        with fake_stdio(stdin=io.BytesIO(data)):
            return qscli.read_input()

    def test_dict_roundtrip(self):
        self.assertEqual(self._read(b'{"a":1}'), {"a": 1})

    def test_invalid_json_never_echoed(self):
        secret = "SECRET-MARKER-7c1eqscli"
        with self.assertRaises(ValueError) as ctx:
            self._read(("{not json " + secret).encode())
        self.assertEqual(str(ctx.exception), "JSON input is invalid")

    def test_non_dict_rejected(self):
        for raw in (b"[1,2]", b'"str"', b"42", b""):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as ctx:
                    self._read(raw)
                self.assertEqual(str(ctx.exception), "JSON input is invalid")

    def test_oversize_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._read(b"x" * (qscli.INPUT_LIMIT + 1))
        self.assertEqual(str(ctx.exception), "JSON input is too large")

    def test_oserror_rejected(self):
        class BadStream:
            def read(self, n):
                raise OSError("disk gone")

        class BadStdin:
            buffer = BadStream()

        with fake_stdio(stdin=BadStdin()):
            with self.assertRaises(ValueError) as ctx:
                qscli.read_input()
        self.assertEqual(str(ctx.exception), "cannot read JSON input")

    def test_text_stdin_without_buffer(self):
        # No .buffer attr: falls back to the stream itself (str path).
        with fake_stdio(stdin=_NoBufferString('{"a":1}')):
            self.assertEqual(qscli.read_input(), {"a": 1})


class _NoBufferString:
    """str-backed stdin without a .buffer attribute (str encode path)."""

    def __init__(self, text):
        self._text = text

    def read(self, n):
        chunk, self._text = self._text[:n], self._text[n:]
        return chunk


class GlobalFlagsCase(unittest.TestCase):
    def test_opt_in_flags(self):
        parser = qscli.SafeParser()
        self.assertIs(qscli.add_global_flags(parser, db=True,
                                             desktop_bin=True), parser)
        args = parser.parse_args(["--db", "a.db", "--desktop-bin", "b"])
        self.assertEqual(args.db, "a.db")
        self.assertEqual(args.desktop_bin, "b")

    def test_defaults_none(self):
        parser = qscli.SafeParser()
        qscli.add_global_flags(parser, db=True, graph=True,
                               desktop_bin=True, projects_file=True)
        args = parser.parse_args([])
        self.assertIsNone(args.db)
        self.assertIsNone(args.graph)
        self.assertIsNone(args.desktop_bin)
        self.assertIsNone(args.projects_file)

    def test_unrequested_flag_rejected_without_echo(self):
        parser = qscli.SafeParser()
        qscli.add_global_flags(parser, db=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(ValueError) as ctx:
                parser.parse_args(["--graph", "SECRET-GRAPH-qq"])
        self.assertEqual(str(ctx.exception), "invalid arguments")
        self.assertNotIn("SECRET-GRAPH-qq", err.getvalue())


class RunMainCase(unittest.TestCase):
    def _run(self, parse_fn, dispatch_fn, tool="testtool",
             bounded=(ValueError,), argv=None):
        out, err = FakeStdout(), io.StringIO()
        with fake_stdio(stdout=out):
            with contextlib.redirect_stderr(err):
                code = qscli.run_main(parse_fn, dispatch_fn, tool,
                                      bounded, argv=argv)
        return code, out.buffer.getvalue(), err.getvalue()

    def test_success_emits_and_returns_zero(self):
        code, stdout, stderr = self._run(
            lambda argv: SimpleNamespace(),
            lambda args: {"ok": True})
        self.assertEqual(code, 0)
        self.assertEqual(stdout, b'{"ok":true}\n')
        self.assertEqual(stderr, "")

    def test_tuple_form_returns_code(self):
        code, stdout, _ = self._run(
            lambda argv: SimpleNamespace(),
            lambda args: (3, {"ok": True}))
        self.assertEqual(code, 3)
        self.assertEqual(stdout, b'{"ok":true}\n')

    def test_int_form_passes_through(self):
        code, stdout, _ = self._run(
            lambda argv: SimpleNamespace(),
            lambda args: 0)
        self.assertEqual(code, 0)
        self.assertEqual(stdout, b"")

    def test_systemexit_passthrough(self):
        for exit_code, expected in ((0, 0), (2, 1), ("boom", 1),
                                    (None, 0)):
            with self.subTest(exit_code=exit_code):
                def parse(argv, _code=exit_code):
                    raise SystemExit(_code)

                code, stdout, stderr = self._run(
                    parse, lambda args: {"ok": True})
                self.assertEqual(code, expected)
                self.assertEqual(stdout, b"")
                self.assertEqual(stderr, "")

    def test_bounded_reports_single_line(self):
        class CustomError(ValueError):
            pass

        def dispatch(args):
            raise CustomError("bad\nthing\rSECRET-X1")

        code, stdout, stderr = self._run(
            lambda argv: SimpleNamespace(), dispatch,
            bounded=(CustomError,))
        self.assertEqual(code, 1)
        self.assertEqual(stdout, b"")
        self.assertIn("error:", stderr)
        self.assertNotIn("Traceback", stderr)
        lines = [line for line in stderr.strip().splitlines() if line]
        self.assertEqual(len(lines), 1)
        # CR/LF folded to spaces (single-line guarantee); the bounded
        # detail itself is preserved verbatim otherwise.
        self.assertEqual(stderr, "error: bad thing SECRET-X1\n")

    def test_bounded_from_parse(self):
        def parse(argv):
            raise ValueError("invalid arguments")

        code, _, stderr = self._run(parse, lambda args: {"ok": True})
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "error: invalid arguments\n")

    def test_fallback_never_echoes_or_traces(self):
        secret = "SECRET-MARKER-2d9eqscli"

        def dispatch(args):
            raise KeyError(secret)

        code, stdout, stderr = self._run(
            lambda argv: SimpleNamespace(), dispatch)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, b"")
        self.assertEqual(stderr, "error: testtool failed\n")
        self.assertNotIn(secret, stderr)
        self.assertNotIn("Traceback", stderr)

    def test_emit_failure_lands_in_bounded(self):
        big = {"k": "x" * (qscli.INPUT_LIMIT + 1)}
        code, stdout, stderr = self._run(
            lambda argv: SimpleNamespace(), lambda args: big)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, b"")
        self.assertEqual(stderr, "error: JSON output is too large\n")

    def test_real_parser_end_to_end(self):
        def parse(argv):
            parser = qscli.SafeParser()
            qscli.add_global_flags(parser, db=True)
            return parser.parse_args([] if argv is None else argv)

        code, stdout, stderr = self._run(
            parse, lambda args: {"db": args.db}, argv=[])
        self.assertEqual(code, 0)
        self.assertEqual(stdout, b'{"db":null}\n')
        self.assertEqual(stderr, "")


if __name__ == "__main__":
    unittest.main()
