"""Persistent, detached jobs with cooperative cancellation and one state writer."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import FrameType
from typing import Any

from providers import RelayError
from task_runner import prepare_task, run_prepared, write_json

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
OUTCOMES = {"ok": "completed", "error": "failed", "cancelled": "cancelled"}
STARTUP_GRACE = 15


def job_root(given: Path | None) -> Path:
    return (
        (given or Path(os.environ.get("AGENT_RELAY_JOBS_DIR", str(Path.home() / ".local/state/agent-relay/jobs"))))
        .expanduser()
        .resolve()
    )


def find_job(job_id: str, root: Path) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise RelayError("Invalid job ID; use the ID returned by submit.")
    directory = root / job_id
    if not directory.is_dir() or directory.is_symlink():
        raise RelayError(f"Job not found: {job_id} in {root}")
    return directory


def read_state(directory: Path) -> dict[str, Any]:
    state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("job_id") != directory.name:
        raise RelayError("Invalid job state record.")
    return state


def status(job_id: str, root: Path) -> dict[str, Any]:
    directory = find_job(job_id, root)
    with (directory / "worker.lock").open("r+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            active = False
        except BlockingIOError:
            active = True
        # Hold an acquired lock while reading, so startup/completion cannot race
        # with an interrupted classification. A busy worker publishes atomically.
        state = read_state(directory)
        result_path = Path(state["output_dir"]) / "result.json"
        if result_path.is_file():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if result.get("status") in OUTCOMES:
                return {**state, "status": OUTCOMES[result["status"]], "result_path": str(result_path)}
        if state["status"] in TERMINAL:
            return state
        if not active and (state["status"] != "queued" or time.time() - state["created_at"] > STARTUP_GRACE):
            return {
                **state,
                "status": "interrupted",
                "error": "Job runner is no longer active. No automatic retry; external CLI cleanup cannot be confirmed.",
            }
        if (directory / "cancel.request").exists():
            return {**state, "status": "cancelling"}
        return state


def submit(args: argparse.Namespace) -> dict[str, Any]:
    task = prepare_task(args)
    root = job_root(args.jobs_dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = root / uuid.uuid4().hex
    directory.mkdir(mode=0o700)
    output = args.output.expanduser().resolve() if args.output else directory / "artifacts"
    try:
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
    except OSError:
        directory.rmdir()
        raise
    state = {
        "schema_version": 1,
        "job_id": directory.name,
        "status": "queued",
        "jobs_dir": str(root),
        "job_dir": str(directory),
        "output_dir": str(output),
        "created_at": time.time(),
        "provider": task["result"]["provider"],
        "model": task["result"]["model"],
        "effort": task["result"]["effort"],
        "kind": task["result"]["kind"],
    }
    (directory / "worker.lock").touch(mode=0o600)
    write_json(directory / "state.json", state)
    try:
        write_json(directory / "task.json", task)
        runtime = directory / "runtime"
        runtime.mkdir(mode=0o700)
        for name in ("jobs.py", "task_runner.py", "execution.py", "providers.py"):
            shutil.copy2(Path(__file__).resolve().parent / name, runtime / name)
        with (directory / "supervisor.log").open("wb") as log:
            subprocess.Popen(
                [sys.executable, str(runtime / "jobs.py"), str(directory)],
                cwd=directory,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
            )
    except OSError as exc:
        state.update(status="failed", error=f"Could not launch job: {exc}", finished_at=time.time())
        write_json(directory / "state.json", state)
        return state
    return status(directory.name, root)


def cancel(job_id: str, root: Path) -> dict[str, Any]:
    state = status(job_id, root)
    if state["status"] in TERMINAL:
        return state
    marker = find_job(job_id, root) / "cancel.request"
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    return status(job_id, root)


def wait(job_id: str, root: Path, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        state = status(job_id, root)
        if state["status"] in TERMINAL:
            return {**state, "wait_timed_out": False}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {**state, "wait_timed_out": True}
        time.sleep(min(0.1, remaining))


def result(job_id: str, root: Path) -> dict[str, Any]:
    state = status(job_id, root)
    if state["status"] not in TERMINAL:
        return {**state, "result_ready": False}
    path = Path(state["output_dir"]) / "result.json"
    if path.is_file():
        return {**state, "result_ready": True, "result": json.loads(path.read_text(encoding="utf-8"))}
    if state["status"] == "completed":
        raise RelayError("The completed job result artifact is missing.")
    return {**state, "result_ready": False}


def signal_cancel(_number: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def worker(directory: Path) -> int:
    with (directory / "worker.lock").open("r+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state(directory)
        if state["status"] != "queued":
            return 1
        if time.time() - state["created_at"] > STARTUP_GRACE:
            state.update(
                status="interrupted",
                error="Worker missed the startup deadline; task was not executed.",
                finished_at=time.time(),
            )
            write_json(directory / "state.json", state)
            return 1
        state.update(status="running", started_at=time.time(), updated_at=time.time(), runner_pid=os.getpid())
        write_json(directory / "state.json", state)
        signal.signal(signal.SIGTERM, signal_cancel)
        signal.signal(signal.SIGINT, signal_cancel)
        last_heartbeat = 0.0

        def heartbeat() -> None:
            nonlocal last_heartbeat
            now = time.monotonic()
            if now - last_heartbeat >= 1:
                state["updated_at"] = time.time()
                write_json(directory / "state.json", state)
                last_heartbeat = now

        try:
            task = json.loads((directory / "task.json").read_text(encoding="utf-8"))
            outcome = run_prepared(task, Path(state["output_dir"]), directory / "cancel.request", heartbeat)
            state.update(status=OUTCOMES[outcome["status"]])
        except KeyboardInterrupt:
            state.update(status="cancelled", error="Job runner interrupted.")
        except Exception as exc:
            logging.getLogger(__name__).exception("Job runner failed")
            state.update(status="failed", error=f"Job runner failed: {exc}")
        state.update(finished_at=time.time(), updated_at=time.time())
        write_json(directory / "state.json", state)
        return 0 if state["status"] == "completed" else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Internal worker entrypoint requires one job directory.")
    raise SystemExit(worker(Path(sys.argv[1]).resolve()))
