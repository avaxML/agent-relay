"""Bounded subprocess execution without a shell or shared worker workspace."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from providers import (
    ORDINARY,
    PARTIAL,
    TERMINAL,
    RelayCancelled,
    RelayError,
    RelayStopped,
    event_classifier,
)

DRAIN_SECONDS = 2.0
TEXT_DROP_MARKER = "[agent-relay dropped {} lines, {} bytes]"
# Below this, a clipped protected event would carry too little of its answer to be worth writing.
MIN_PROTECTED_BYTES = 1024


def _signal_group(process: subprocess.Popen[bytes], number: int) -> None:
    try:
        os.killpg(process.pid, number)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS reports EPERM, not ESRCH, for a group whose remaining members are zombies. Signal the child
        # directly so a live child can never leave the final wait unbounded.
        process.send_signal(number)


def stop_process(process: subprocess.Popen[bytes]) -> None:
    """Stop the CLI and descendants in its own process group."""
    _signal_group(process, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1)
    _signal_group(process, signal.SIGKILL)
    process.wait()


def _utf8_prefix(data: bytes, limit: int) -> bytes:
    """Cut at most limit bytes without splitting a UTF-8 character."""
    end = min(limit, len(data))
    while 0 < end < len(data) and end > limit - 4 and data[end] & 0xC0 == 0x80:
        end -= 1
    return data[:end]


# Fields decoders and readers key on; short values survive clipping verbatim.
DISCRIMINATORS = ("type", "subtype", "event")


def _clip_event(event: dict[str, Any], cap: int) -> dict[str, Any]:
    return {
        key: value if key in DISCRIMINATORS and isinstance(value, str) and len(value) <= 256 else _clip(value, cap)
        for key, value in event.items()
    }


def _clip(value: Any, cap: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= cap else value[:cap] + "…"
    if isinstance(value, dict):
        return {key: _clip(item, cap) for key, item in value.items()}
    if isinstance(value, list):
        return [_clip(item, cap) for item in value]
    return value


def _longest_string(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return max((_longest_string(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return max((_longest_string(item) for item in value), default=0)
    return 0


def shrink_event(event: dict[str, Any], original: int, limit: int, allow_stub: bool = True) -> bytes | None:
    """Re-serialize an event under limit bytes by clipping its longest strings; keys and types survive.

    When no clipped form fits, an ordinary event becomes a stub of its short top-level fields. With allow_stub=False
    (answer events, whose nested payload the decoder needs) it returns None instead.
    """

    def render(candidate: dict[str, Any]) -> bytes:
        marked = {**candidate, "relay_truncated_bytes": original}
        # ASCII escapes keep a lone surrogate from a provider from failing the encode.
        data = json.dumps(marked, separators=(",", ":")).encode()
        marked["relay_truncated_bytes"] = original - len(data) - 1
        return json.dumps(marked, separators=(",", ":")).encode() + b"\n"

    best: bytes | None = None
    try:
        # No string longer than the limit can fit, so the search starts there instead of at a multi-megabyte one.
        low, high = 0, min(_longest_string(event), limit)
        while low <= high:
            cap = (low + high) // 2
            data = render(_clip_event(event, cap))
            if len(data) <= limit:
                best, low = data, cap + 1
            else:
                high = cap - 1
    except RecursionError:
        # json.loads accepts nesting deeper than these recursive helpers can walk; fall back to the stub.
        best = None
    if best is None and not allow_stub:
        return None
    if best is None:
        # Too many fields to fit: keep only short top-level scalars such as type, subtype and IDs, then only the
        # fields every decoder keys on.
        stub = {
            key: value
            for key, value in event.items()
            if isinstance(value, (int, float, bool)) or (isinstance(value, str) and len(value) <= 256)
        }
        best = render(stub)
        if len(best) > limit:
            # Only string discriminators: a nested value here could recurse past the limit or never fit.
            best = render({key: event[key][:64] for key in DISCRIMINATORS if isinstance(event.get(key), str)})
        if len(best) > limit:
            # Escaping can still inflate short values (a NUL becomes six bytes); the marker alone always fits.
            best = render({})
    return best


class BoundedLog:
    """Persist a provider stream under max_bytes without stopping the provider for verbosity.

    JSONL streams keep events byte for byte unless an event exceeds max_event_bytes, in which case its longest
    strings are clipped. Once the log nears its cap, ordinary events are dropped and counted; a quarter of the
    cap stays reserved for answer-carrying events (see PROTECTED). Text streams keep the head and the tail.
    """

    def __init__(self, path: Path, output: str, max_bytes: int, max_event_bytes: int) -> None:
        self.stream: IO[bytes] = path.open("wb")
        self.protected = event_classifier(output)
        self.max_bytes = max_bytes
        self.max_event_bytes = max_event_bytes
        self.pending = bytearray()
        self.overflow = False
        self.overflow_blank = False
        self.written = 0
        if self.protected is None:
            self.head_limit = max_bytes // 2
            self.tail_limit = max_bytes - self.head_limit - 128
            # A text answer can be one long line, so cut a line only when it could not fit in the tail at all.
            self.line_limit = self.tail_limit
        else:
            self.reserve = max_bytes // 4
            self.line_limit = max_bytes
        # Whole lines in one buffer, trimmed in batches: one object per line would let millions of tiny lines
        # cost far more memory than the bytes they hold.
        self.tail = bytearray()
        self.stats = {
            "max_bytes": max_bytes,
            "received_bytes": 0,
            "written_bytes": 0,
            "truncated_events": 0,
            "truncated_bytes": 0,
            "dropped_events": 0,
            "dropped_bytes": 0,
            "dropped_answer_events": 0,
            "truncated_answer_events": 0,
            "dropped_malformed_events": 0,
            "write_errors": 0,
        }

    def feed(self, data: bytes) -> None:
        self.stats["received_bytes"] += len(data)
        start = 0
        while (index := data.find(b"\n", start)) >= 0:
            self._append(data[start : index + 1])
            self._end_line()
            start = index + 1
        self._append(data[start:])

    def close(self) -> None:
        if self.pending or self.overflow:
            self._end_line()
        self._trim_tail()
        if self.protected is None and self.stats["dropped_events"]:
            dropped = self.stats["dropped_events"], self.stats["dropped_bytes"]
            # The offset, not the marker text, tells readers where the tail starts: a provider can print the text.
            self.stats["tail_offset"] = self.written + len(TEXT_DROP_MARKER.format(*dropped)) + 1
            self._write(TEXT_DROP_MARKER.format(*dropped).encode() + b"\n")
        self._write(bytes(self.tail))
        self.stats["written_bytes"] = self.written
        try:
            self.stream.close()
        except OSError:
            self.stats["write_errors"] += 1

    def _write(self, data: bytes) -> None:
        # A failed write (disk full) must not end the run silently or as a crash: it is counted, and a provider
        # whose stdout log failed is never decoded as a success.
        if self.stats["write_errors"]:
            return
        try:
            self.stream.write(data)
        except OSError:
            self.stats["write_errors"] += 1
            return
        self.written += len(data)

    def _append(self, piece: bytes) -> None:
        if not piece:
            return
        if self.overflow:
            self.stats["dropped_bytes" if self.protected else "truncated_bytes"] += len(piece)
            self.overflow_blank = self.overflow_blank and not piece.strip()
            return
        self.pending += piece
        if len(self.pending) > self.line_limit:
            self.overflow_blank = not self.pending.strip()
            # Bound memory for a line with no newline yet: text keeps a prefix, JSONL cannot be cut.
            excess = len(self.pending) - (self.line_limit if self.protected is None else 0)
            del self.pending[len(self.pending) - excess :]
            self.stats["dropped_bytes" if self.protected else "truncated_bytes"] += excess
            self.overflow = True

    def _end_line(self) -> None:
        line, overflowed = bytes(self.pending), self.overflow
        self.pending.clear()
        self.overflow = False
        if self.protected is None:
            self._text_line(line, overflowed)
        elif overflowed and not self.overflow_blank:
            # Too long to parse in bounded memory, so it may have been the answer: fail closed.
            self.stats["dropped_events"] += 1
            self.stats["dropped_answer_events"] += 1
        elif overflowed:
            # Only whitespace, which every decoder skips.
            self.stats["dropped_events"] += 1
        else:
            self._event_line(line)

    def _event_line(self, line: bytes) -> None:
        assert self.protected is not None
        original = len(line)
        oversized = original > self.max_event_bytes
        level = ORDINARY
        event: dict[str, Any] | None = None
        malformed = False
        if oversized or self.written + len(line) > self.max_bytes - self.reserve:
            try:
                parsed = json.loads(line)
            except (ValueError, RecursionError):
                # Malformed, non-UTF-8, too deeply nested, or an integer past Python's digit limit.
                parsed = None
            # Blank lines are skipped by every decoder, so dropping one changes nothing.
            malformed = not isinstance(parsed, dict) and bool(line.strip())
            if isinstance(parsed, dict):
                event = parsed
                level = self.protected(event)
                if oversized and level == ORDINARY:
                    line = shrink_event(event, len(line), self.max_event_bytes) or line
        headroom = {ORDINARY: self.reserve, PARTIAL: self.reserve // 2, TERMINAL: 0}[level]
        available = self.max_bytes - headroom - self.written
        if level != ORDINARY and len(line) > available >= MIN_PROTECTED_BYTES and event is not None:
            # Keep what fits so a stopped or failed run still has the text; a clipped terminal answer is never
            # published as complete (truncated_answer_events fails the run).
            line = shrink_event(event, original, available, allow_stub=False) or line
        if len(line) <= available:
            if len(line) < original:
                self.stats["truncated_events"] += 1
                self.stats["truncated_bytes"] += original - len(line)
                if level == TERMINAL:
                    self.stats["truncated_answer_events"] += 1
            self._write(line)
        else:
            self.stats["dropped_events"] += 1
            self.stats["dropped_bytes"] += original
            if level == TERMINAL:
                self.stats["dropped_answer_events"] += 1
            elif malformed:
                # The decoder would have rejected this line; dropping it must not turn a protocol error into success.
                self.stats["dropped_malformed_events"] += 1

    def _text_line(self, line: bytes, overflowed: bool) -> None:
        if overflowed or len(line) > self.line_limit:
            body = line.removesuffix(b"\n")
            kept = _utf8_prefix(body, self.line_limit - 1)
            # The end of the line is lost, and a text answer often ends there.
            self.stats["truncated_answer_events"] += 1
            self.stats["truncated_events"] += 1
            self.stats["truncated_bytes"] += len(body) - len(kept)
            kept += b"\n"
            line = kept
        if not self.tail and self.written + len(line) <= self.head_limit:
            self._write(line)
            return
        self.tail += line
        # Without framing, text cannot show which lines were its answer, so any line dropped from stdout makes
        # the answer unprovable; invoke_provider fails the run and keeps the tail as the partial answer.
        if len(self.tail) > 2 * self.tail_limit:
            self._trim_tail()

    def _trim_tail(self) -> None:
        """Drop whole lines from the front until the tail fits; every line fits on its own."""
        if not self.tail:
            return
        excess = len(self.tail) - self.tail_limit
        if excess <= 0:
            return
        cut = self.tail.find(b"\n", excess - 1) + 1
        lines = self.tail.count(b"\n", 0, cut)
        self.stats["dropped_events"] += lines
        self.stats["dropped_answer_events"] += lines
        self.stats["dropped_bytes"] += cut
        del self.tail[:cut]


@dataclass
class OutputBudget:
    """Limits for one provider run; stderr gets an eighth of max_log_bytes and stdout the rest."""

    output_format: str
    max_log_bytes: int
    max_event_bytes: int
    hard_ceiling_bytes: int
    logs: dict[str, BoundedLog] = field(default_factory=dict)

    def open(self, output: Path) -> dict[str, BoundedLog]:
        stderr_bytes = self.max_log_bytes // 8
        # Built one at a time so the caller can close whatever opened if the second open fails.
        self.logs = {}
        self.logs["stdout"] = BoundedLog(
            output / "stdout.log", self.output_format, self.max_log_bytes - stderr_bytes, self.max_event_bytes
        )
        self.logs["stderr"] = BoundedLog(output / "stderr.log", "text", stderr_bytes, self.max_event_bytes)
        return self.logs

    def received(self) -> int:
        return sum(log.stats["received_bytes"] for log in self.logs.values())

    def stats(self) -> dict[str, dict[str, int]]:
        return {name: dict(log.stats) for name, log in self.logs.items()}


def _pump(selector: selectors.BaseSelector, wait: float) -> None:
    for key, _events in selector.select(wait):
        try:
            data = os.read(key.fd, 65536)
        except BlockingIOError:
            continue
        if data:
            key.data.feed(data)
        else:
            selector.unregister(key.fileobj)
            key.fileobj.close()  # type: ignore[union-attr]


def _execute_bounded(
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    stdin: Path,
    output: Path,
    timeout: float,
    budget: OutputBudget,
    check_control: Callable[[], None],
) -> int:
    logs: dict[str, BoundedLog] = {}
    selector = selectors.DefaultSelector()
    process: subprocess.Popen[bytes] | None = None
    try:
        logs = budget.open(output)
        with stdin.open("rb") as source:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            assert pipe is not None
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, logs[name])
        start = time.monotonic()
        while process.poll() is None:
            check_control()
            if time.monotonic() - start > timeout:
                raise RelayStopped(f"Worker exceeded {timeout:g}s; process group stopped.", "timeout")
            _pump(selector, 0.05)
            if budget.received() > budget.hard_ceiling_bytes:
                raise RelayStopped(
                    f"Worker output passed the {budget.hard_ceiling_bytes}-byte runaway ceiling; "
                    "process group stopped.",
                    "runaway_output",
                )
        check_control()
        return process.returncode
    finally:
        # Each step runs even if an earlier one is interrupted (a second signal, a drain error), so the group is
        # always reaped and the logs are always flushed before salvage reads them. Cleanup errors never replace
        # the exception that stopped the worker.
        try:
            if process is not None:
                # Also clean up descendants left behind by a completed private CLI. Keep reading during the
                # SIGTERM grace period so a provider flushing on shutdown is not blocked on a full pipe and killed.
                try:
                    _signal_group(process, signal.SIGTERM)
                    grace = time.monotonic() + 1
                    with suppress(OSError):
                        while process.poll() is None and time.monotonic() < grace:
                            _pump(selector, 0.05)
                finally:
                    _signal_group(process, signal.SIGKILL)
                    process.wait()
            deadline = time.monotonic() + DRAIN_SECONDS
            with suppress(OSError):
                while selector.get_map() and time.monotonic() < deadline:
                    _pump(selector, 0.05)
        finally:
            for key in list(selector.get_map().values()):
                selector.unregister(key.fileobj)
                with suppress(OSError):
                    key.fileobj.close()  # type: ignore[union-attr]
            selector.close()
            for log in budget.logs.values():
                log.close()


def execute(
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    stdin: Path,
    output: Path,
    timeout: float,
    max_log_bytes: int = 4_000_000,
    cancel_file: Path | None = None,
    heartbeat: Callable[[], None] | None = None,
    budget: OutputBudget | None = None,
) -> int:
    """Run argv in its own process group.

    Without a budget, output goes straight to the log files and passing max_log_bytes stops the worker: git
    and Relay checks depend on output that is either complete or refused. With a budget, provider output is
    streamed through bounded logs, and only timeouts, cancellation and the runaway ceiling stop the worker.
    """

    def check_control() -> None:
        if cancel_file is not None and cancel_file.exists():
            raise RelayCancelled("Cancellation requested; worker process group stopped.")
        if heartbeat is not None:
            heartbeat()

    check_control()
    if budget is not None:
        return _execute_bounded(argv, cwd, env, stdin, output, timeout, budget, check_control)
    stdout = output / "stdout.log"
    stderr = output / "stderr.log"
    with stdin.open("rb") as source, stdout.open("wb") as out, stderr.open("wb") as err:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=source, stdout=out, stderr=err, start_new_session=True)
        start = time.monotonic()
        try:
            while process.poll() is None:
                check_control()
                if time.monotonic() - start > timeout:
                    raise RelayError(f"Worker exceeded {timeout:g}s; process group stopped.")
                if stdout.stat().st_size + stderr.stat().st_size > max_log_bytes:
                    raise RelayError("Worker exceeded the log size limit; process group stopped.")
                time.sleep(0.05)
            if stdout.stat().st_size + stderr.stat().st_size > max_log_bytes:
                raise RelayError("Worker exceeded the log size limit.")
            check_control()
            return process.returncode
        finally:
            # Also clean up descendants left behind by a completed private CLI.
            stop_process(process)
