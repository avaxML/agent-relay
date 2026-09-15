"""Bounded subprocess execution without a shell or shared worker workspace."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from providers import RelayCancelled, RelayError


def stop_process(process: subprocess.Popen[bytes]) -> None:
    """Stop the CLI and descendants in its own process group."""
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1)
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


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
) -> int:
    def check_control() -> None:
        if cancel_file is not None and cancel_file.exists():
            raise RelayCancelled("Cancellation requested; worker process group stopped.")
        if heartbeat is not None:
            heartbeat()

    stdout = output / "stdout.log"
    stderr = output / "stderr.log"
    check_control()
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
