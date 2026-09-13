#!/usr/bin/env python3
"""Capture a Hyprland region and return it in the image-tool JSON format."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager

TIMEOUT = 30
MAX_BYTES = 8 * 1024 * 1024
PROCESS_TERMINATE_TIMEOUT = 2
GEOMETRY_MAX_BYTES = 4096
GEOMETRY_PATTERN = re.compile(r"([+-]?[0-9]+),([+-]?[0-9]+) ([0-9]+)x([0-9]+)")


class CaptureCancelled(Exception):
    """Raised when the capture is cancelled by the parent or the user."""


def _terminate_process_group(process):
    """Terminate and reap an interactive capture process and its children.

    Both capture stages are put in private process groups, so stopping the
    Python helper cannot leave a selector or capture process behind. Check
    ``poll`` before each signal: once Popen has reaped the child, its old
    process-group id must never be reused for cleanup.
    """
    if process.poll() is not None:
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        # The child may have exited between poll() and killpg().  wait() below
        # still reaps it when possible, and no other process is targeted.
        pass

    try:
        process.wait(timeout=PROCESS_TERMINATE_TIMEOUT)
        return
    except subprocess.TimeoutExpired:
        pass

    # Do not send SIGKILL after the Popen child has exited.  In particular,
    # never assume that the original process-group id still belongs to us.
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            pass
        try:
            process.wait(timeout=PROCESS_TERMINATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            # SIGKILL cannot be ignored by a real child.  A stubborn mocked or
            # broken wait implementation must not make cancellation unbounded.
            pass


def _run_capture_process(command, timeout=TIMEOUT):
    """Run one capture command with cancellation handlers and group cleanup."""
    process = None
    cancel_requested = False

    def cancel(_signum, _frame):
        nonlocal cancel_requested
        # Popen can briefly be between fork and returning its object.  Defer
        # raising until the object is available so that the child is still
        # reachable by the cleanup in finally.
        if process is None:
            cancel_requested = True
        else:
            raise CaptureCancelled("screen capture cancelled")

    previous_term = None
    previous_int = None
    term_installed = False
    int_installed = False
    try:
        previous_term = signal.signal(signal.SIGTERM, cancel)
        term_installed = True
        previous_int = signal.signal(signal.SIGINT, cancel)
        int_installed = True
        process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        if cancel_requested:
            raise CaptureCancelled("screen capture cancelled")
        try:
            returncode_output = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("screen capture timed out") from exc
    finally:
        # This also runs after cancel() raises, allowing normal termination of
        # this helper to clean up the capture process and its descendants.
        if process is not None:
            _terminate_process_group(process)
        if int_installed:
            assert previous_int is not None
            signal.signal(signal.SIGINT, previous_int)
        if term_installed:
            assert previous_term is not None
            signal.signal(signal.SIGTERM, previous_term)

    assert process is not None
    return process.returncode, returncode_output[0], returncode_output[1]


@contextmanager
def _capture_cancellation_scope():
    """Keep cancellation effective while transitioning between capture stages."""
    def cancel(_signum, _frame):
        raise CaptureCancelled("screen capture cancelled")

    previous_term = signal.signal(signal.SIGTERM, cancel)
    previous_int = signal.signal(signal.SIGINT, cancel)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)


def _stderr_detail(stderr):
    return stderr.decode("utf-8", "replace").strip()


def parse_geometry(value):
    """Validate and canonicalize a grim/slurp geometry string.

    This is intentionally shared by the command line parser and ``capture``
    so callers cannot bypass the CLI validation when using the helper as a
    Python module.
    """
    if not isinstance(value, str):
        raise ValueError("geometry must be a string in the form x,y widthxheight")
    match = GEOMETRY_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError("geometry must be in the form x,y widthxheight")
    x, y, width, height = (int(item) for item in match.groups())
    if width <= 0 or height <= 0:
        raise ValueError("geometry width and height must be positive")
    return f"{x},{y} {width}x{height}"


def _geometry_argument(value):
    try:
        return parse_geometry(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _geometry(stdout, stderr, returncode):
    """Return slurp's geometry in the canonical format."""
    if len(stdout) > GEOMETRY_MAX_BYTES or len(stderr) > GEOMETRY_MAX_BYTES:
        raise RuntimeError("slurp returned too much output")

    detail = _stderr_detail(stderr)
    if returncode != 0 or not stdout.strip():
        message = detail or "screen selection cancelled"
        if returncode != 0 and not detail:
            message = f"screen selection cancelled (slurp exited with status {returncode})"
        raise CaptureCancelled(message)

    try:
        text = stdout.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError("slurp returned invalid geometry") from exc

    try:
        return parse_geometry(text)
    except ValueError as exc:
        raise RuntimeError("slurp returned invalid geometry") from exc


def _valid_png(data):
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    position = 8
    saw_header = False
    while position + 12 <= len(data):
        length = int.from_bytes(data[position:position + 4], "big")
        kind = data[position + 4:position + 8]
        end = position + 12 + length
        if end > len(data):
            return False
        if not saw_header:
            if kind != b"IHDR" or length != 13:
                return False
            saw_header = True
        if kind == b"IEND":
            return saw_header and length == 0 and end == len(data)
        position = end
    return False


def capture(geometry=None):
    """Capture ``geometry`` directly, or use slurp when it is omitted."""
    if geometry is not None:
        try:
            geometry = parse_geometry(geometry)
        except ValueError as exc:
            raise RuntimeError(f"invalid capture geometry: {exc}") from exc

    grim = shutil.which("grim")
    if grim is None:
        raise RuntimeError("grim is required for screen capture")
    slurp = None
    if geometry is None:
        slurp = shutil.which("slurp")
        if slurp is None:
            raise RuntimeError("slurp is required when no capture geometry is supplied")
    image_tool = shutil.which("magick") or shutil.which("convert")
    with tempfile.TemporaryDirectory(prefix="quickshell-capture-") as directory:
        source = Path(directory) / "capture.png"
        with _capture_cancellation_scope():
            if geometry is None:
                returncode, stdout, stderr = _run_capture_process(
                    [slurp, "-d"], timeout=TIMEOUT)
                geometry = _geometry(stdout, stderr, returncode)
            returncode, stdout, stderr = _run_capture_process(
                [grim, "-g", geometry, "-t", "png", "-"], timeout=TIMEOUT)
            if returncode != 0:
                detail = _stderr_detail(stderr)
                raise RuntimeError(detail or "grim screen capture failed")
            if not stdout or not _valid_png(stdout):
                raise RuntimeError("screen capture did not return a valid PNG")
        needs_resize = len(stdout) * 4 > MAX_BYTES * 3
        if needs_resize and image_tool is None:
            raise RuntimeError("ImageMagick (magick or convert) is required for large captures")
        data = stdout
        if needs_resize:
            output = Path(directory) / "resized.png"
            source.write_bytes(stdout)
            # The geometry suffix makes ImageMagick leave images below the bound alone.
            try:
                resized = subprocess.run([image_tool or "", str(source), "-resize", "2000x2000>", str(output)],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         timeout=TIMEOUT, check=False)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("image resize timed out") from exc
            if resized.returncode != 0 or not output.is_file():
                detail = resized.stderr.decode("utf-8", "replace").strip()
                raise RuntimeError(detail or "image resize failed")
            data = output.read_bytes()
            if not _valid_png(data):
                raise RuntimeError("resized image is not a valid PNG")
    encoded = base64.b64encode(data).decode("ascii")
    if len(encoded) > MAX_BYTES:
        raise RuntimeError("encoded image is too large")
    return {"images": [{"type": "image", "data": encoded,
                         "mimeType": "image/png"}]}


def build_parser():
    """Build the CLI parser using the same geometry validator as capture()."""
    parser = argparse.ArgumentParser(description="Capture a selected screen region")
    parser.add_argument(
        "--geometry", type=_geometry_argument,
        help="capture directly using grim geometry (for example: 0,0 640x480)",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        print(json.dumps(capture(args.geometry)))
        return 0
    except (CaptureCancelled, OSError, RuntimeError, KeyboardInterrupt) as exc:
        print(f"error: {exc or 'cancelled'}", file=sys.stderr)
        return 130 if isinstance(exc, (CaptureCancelled, KeyboardInterrupt)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
