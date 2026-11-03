"""Bounded subprocess execution without a shell or shared worker workspace."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from providers import RelayCancelled, RelayError, RelayStopped

# Protection levels: ordinary events may be clipped and stop at three quarters of the cap; partial-answer events
# are never clipped and may use half the reserve; events a decoder depends on for the final answer or a failure
# are never clipped and may use the whole cap.
ORDINARY, PARTIAL, TERMINAL = 0, 1, 2


def _cursor_level(event: dict[str, Any]) -> int:
    return {"result": TERMINAL, "assistant": PARTIAL}.get(event.get("type"), ORDINARY)  # type: ignore[arg-type]


PROTECTED: dict[str, Callable[[dict[str, Any]], int]] = {
    "cursor-jsonl": _cursor_level,
    "agy-jsonl": lambda event: TERMINAL if event.get("event") == "result" else ORDINARY,
    "opencode-jsonl": lambda event: (
        TERMINAL if event.get("type") in {"text", "error"} or "error" in event else ORDINARY
    ),
}
DRAIN_SECONDS = 2.0


def stop_process(process: subprocess.Popen[bytes]) -> None:
    """Stop the CLI and descendants in its own process group."""
    # macOS reports EPERM, not ESRCH, for a group whose remaining members are zombies.
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1)
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def _utf8_prefix(data: bytes, limit: int) -> bytes:
    """Cut at most limit bytes without splitting a UTF-8 character."""
    end = min(limit, len(data))
    while 0 < end < len(data) and end > limit - 4 and data[end] & 0xC0 == 0x80:
        end -= 1
    return data[:end]


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


def shrink_event(event: dict[str, Any], original: int, limit: int) -> bytes:
    """Re-serialize an event under limit bytes by clipping its longest strings; keys and types survive."""

    def render(candidate: dict[str, Any]) -> bytes:
        marked = {**candidate, "relay_truncated_bytes": original}
        data = json.dumps(marked, ensure_ascii=False, separators=(",", ":")).encode()
        marked["relay_truncated_bytes"] = original - len(data) - 1
        return json.dumps(marked, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"

    best: bytes | None = None
    # No string longer than the limit can fit, so the search starts there instead of at a multi-megabyte string.
    low, high = 0, min(_longest_string(event), limit)
    while low <= high:
        cap = (low + high) // 2
        data = render(_clip(event, cap))
        if len(data) <= limit:
            best, low = data, cap + 1
        else:
            high = cap - 1
    if best is None:
        # Too many fields to fit: keep only short top-level scalars such as type, subtype and IDs.
        stub = {
            key: value
            for key, value in event.items()
            if isinstance(value, (int, float, bool)) or (isinstance(value, str) and len(value) <= 256)
        }
        best = render(stub)
    return best


class BoundedLog:
    """Persist a provider stream under max_bytes without stopping the provider for verbosity.

    JSONL streams keep events byte for byte unless an event exceeds max_event_bytes, in which case its longest
    strings are clipped. Once the log nears its cap, ordinary events are dropped and counted; a quarter of the
    cap stays reserved for answer-carrying events (see PROTECTED). Text streams keep the head and the tail.
    """

    def __init__(self, path: Path, output: str, max_bytes: int, max_event_bytes: int) -> None:
        self.stream: IO[bytes] = path.open("wb")
        self.protected = PROTECTED.get(output)
        self.max_bytes = max_bytes
        self.max_event_bytes = max_event_bytes
        self.pending = bytearray()
        self.overflow = False
        self.written = 0
        if self.protected is None:
            self.head_limit = max_bytes // 2
            self.tail_limit = max_bytes - self.head_limit - 128
            self.line_limit = max_event_bytes
        else:
            self.reserve = max_bytes // 4
            self.line_limit = max_bytes
        self.tail: deque[bytes] = deque()
        self.tail_bytes = 0
        self.stats = {
            "max_bytes": max_bytes,
            "received_bytes": 0,
            "written_bytes": 0,
            "truncated_events": 0,
            "truncated_bytes": 0,
            "dropped_events": 0,
            "dropped_bytes": 0,
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
        if self.tail:
            if self.stats["dropped_events"]:
                dropped = self.stats["dropped_events"], self.stats["dropped_bytes"]
                self._write(f"[agent-relay dropped {dropped[0]} lines, {dropped[1]} bytes]\n".encode())
            for line in self.tail:
                self._write(line)
        self.stats["written_bytes"] = self.written
        self.stream.close()

    def _write(self, data: bytes) -> None:
        self.stream.write(data)
        self.written += len(data)

    def _append(self, piece: bytes) -> None:
        if not piece:
            return
        if self.overflow:
            self.stats["dropped_bytes" if self.protected else "truncated_bytes"] += len(piece)
            return
        self.pending += piece
        if len(self.pending) > self.line_limit:
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
        elif overflowed:
            self.stats["dropped_events"] += 1
        else:
            self._event_line(line)

    def _event_line(self, line: bytes) -> None:
        assert self.protected is not None
        oversized = len(line) > self.max_event_bytes
        level = ORDINARY
        if oversized or self.written + len(line) > self.max_bytes - self.reserve:
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                event = None
            if isinstance(event, dict):
                level = self.protected(event)
                if oversized and level == ORDINARY:
                    shrunk = shrink_event(event, len(line), self.max_event_bytes)
                    self.stats["truncated_events"] += 1
                    self.stats["truncated_bytes"] += len(line) - len(shrunk)
                    line = shrunk
        headroom = {ORDINARY: self.reserve, PARTIAL: self.reserve // 2, TERMINAL: 0}[level]
        if self.written + len(line) <= self.max_bytes - headroom:
            self._write(line)
        else:
            self.stats["dropped_events"] += 1
            self.stats["dropped_bytes"] += len(line)

    def _text_line(self, line: bytes, overflowed: bool) -> None:
        if overflowed or len(line) > self.max_event_bytes:
            body = line.removesuffix(b"\n")
            kept = _utf8_prefix(body, self.max_event_bytes - 1)
            self.stats["truncated_events"] += 1
            self.stats["truncated_bytes"] += len(body) - len(kept)
            kept += b"\n"
            line = kept
        if not self.tail and self.written + len(line) <= self.head_limit:
            self._write(line)
            return
        self.tail.append(line)
        self.tail_bytes += len(line)
        while self.tail_bytes > self.tail_limit:
            dropped = self.tail.popleft()
            self.tail_bytes -= len(dropped)
            self.stats["dropped_events"] += 1
            self.stats["dropped_bytes"] += len(dropped)


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
        self.logs = {
            "stdout": BoundedLog(
                output / "stdout.log", self.output_format, self.max_log_bytes - stderr_bytes, self.max_event_bytes
            ),
            "stderr": BoundedLog(output / "stderr.log", "text", stderr_bytes, self.max_event_bytes),
        }
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
    logs = budget.open(output)
    selector = selectors.DefaultSelector()
    process: subprocess.Popen[bytes] | None = None
    try:
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
        if process is not None:
            # Also clean up descendants left behind by a completed private CLI, then drain what they wrote.
            stop_process(process)
        deadline = time.monotonic() + DRAIN_SECONDS
        while selector.get_map() and time.monotonic() < deadline:
            _pump(selector, 0.05)
        for key in list(selector.get_map().values()):
            selector.unregister(key.fileobj)
            key.fileobj.close()  # type: ignore[union-attr]
        selector.close()
        for log in logs.values():
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
