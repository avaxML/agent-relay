"""Fan a manifest of bounded tasks out across providers through injected job functions."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from itertools import combinations
from pathlib import Path
from typing import Any

import isolation
import task_runner
from isolation import globs_overlap
from providers import RelayError
from task_runner import write_json

TASK_KEYS = frozenset({"id", "kind", "task_file", "files", "owns", "checks", "setup", "provider", "effort", "model"})
REQUIRED_KEYS = frozenset({"id", "kind", "task_file", "files"})
TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})
SUBMIT_FAILED = frozenset({"failed", "cancelled", "interrupted"})
TASK_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")

Submit = Callable[[argparse.Namespace], Mapping[str, Any]]
Lookup = Callable[[str], Mapping[str, Any]]


def dispatch_root() -> Path:
    if configured := os.environ.get("AGENT_RELAY_DISPATCH_DIR"):
        root = Path(configured).expanduser()
    elif state := os.environ.get("XDG_STATE_HOME"):
        root = Path(state).expanduser() / "agent-relay" / "dispatches"
    else:
        root = Path.home() / ".local" / "state" / "agent-relay" / "dispatches"
    if root.is_symlink():
        raise RelayError(f"Dispatch root must not be a symlink: {root}")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    return root.resolve()


def _strings(task_id: str, key: str, value: Any, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value) or (nonempty and not value):
        raise RelayError(f"Task {task_id}: {key} must be a {'nonempty ' if nonempty else ''}list of strings.")
    return list(value)


def _normalize(raw: Any, directory: Path, kinds: Mapping[str, bool]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RelayError("Each manifest task must be an object.")
    unknown = set(raw) - TASK_KEYS
    missing = REQUIRED_KEYS - set(raw)
    task_id = raw.get("id")
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        raise RelayError(f"Task id must match [A-Za-z0-9._-]{{1,64}}: {task_id!r}")
    if unknown or missing:
        raise RelayError(f"Task {task_id}: unknown keys {sorted(unknown)}, missing keys {sorted(missing)}.")
    kind = raw["kind"]
    if not isinstance(kind, str) or kind not in kinds:
        raise RelayError(f"Task {task_id}: unknown kind {kind!r}.")
    if not isinstance(raw["task_file"], str):
        raise RelayError(f"Task {task_id}: task_file must be a string.")
    try:
        task_file = (directory / raw["task_file"]).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RelayError(f"Task {task_id}: task_file not found: {raw['task_file']}") from exc
    if not task_file.is_relative_to(directory) or not task_file.is_file():
        raise RelayError(f"Task {task_id}: task_file must be a regular file inside {directory}.")
    task: dict[str, Any] = {
        "id": task_id,
        "kind": kind,
        "task_file": str(task_file),
        "files": _strings(task_id, "files", raw["files"], nonempty=True),
    }
    for key in ("owns", "checks", "setup"):
        task[key] = _strings(task_id, key, raw.get(key, []))
    for key in ("provider", "effort", "model"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            raise RelayError(f"Task {task_id}: {key} must be a string.")
        task[key] = value
    try:
        task["owns"] = task_runner.check_tool_options(kind, task["owns"], task["checks"], task["setup"])
    except RelayError as exc:
        raise RelayError(f"Task {task_id}: {exc}") from exc
    return task


def load_manifest(path: Path, kinds: Mapping[str, bool]) -> list[dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RelayError(f"Could not read manifest {path}: {exc}") from exc
    if not isinstance(document, dict) or set(document) != {"tasks"}:
        raise RelayError('Manifest must be an object with exactly one key, "tasks".')
    if not isinstance(document["tasks"], list) or not document["tasks"]:
        raise RelayError("Manifest tasks must be a nonempty list.")
    directory = path.resolve().parent
    tasks = [_normalize(raw, directory, kinds) for raw in document["tasks"]]
    seen: set[str] = set()
    for task in tasks:
        if task["id"] in seen:
            raise RelayError(f"Duplicate task id: {task['id']}")
        seen.add(task["id"])
    executes = [task for task in tasks if task["kind"] == "execute"]
    for first, second in combinations(executes, 2):
        if any(globs_overlap(a, b) for a in first["owns"] for b in second["owns"]):
            raise RelayError(f"Tasks {first['id']} and {second['id']} declare overlapping owns.")
    return tasks


def assign(
    tasks: Sequence[Mapping[str, Any]],
    order: Sequence[str],
    health: Mapping[str, Mapping[str, Any]],
    kinds: Mapping[str, bool],
) -> tuple[dict[str, str], list[dict[str, str]]]:
    def eligible(name: str, kind: str) -> bool:
        report = health.get(name, {})
        return bool(report.get("ok")) and (bool(report.get("tools")) or not kinds[kind])

    load = dict.fromkeys(order, 0)
    assignments: dict[str, str] = {}
    fallbacks: list[dict[str, str]] = []
    for task in tasks:
        requested = task.get("provider")
        if requested is not None:
            if requested not in load:
                raise RelayError(f"Task {task['id']} requests provider {requested}, which is not in --providers.")
            if eligible(requested, task["kind"]):
                choice: str | None = requested
            else:
                reason = str(health.get(requested, {}).get("reason", "unknown"))
                fallbacks.append(
                    {
                        "id": task["id"],
                        "task_file": task["task_file"],
                        "reason": f"requested provider {requested} unavailable: {reason}",
                    }
                )
                continue
        else:
            candidates = [name for name in order if eligible(name, task["kind"])]
            choice = min(candidates, key=lambda name: load[name]) if candidates else None
        if choice is None:
            reason = f"no provider passed doctor with support for kind {task['kind']}"
            fallbacks.append({"id": task["id"], "task_file": task["task_file"], "reason": reason})
            continue
        assignments[task["id"]] = choice
        load[choice] += 1
    return assignments, fallbacks


def _json_safe(settings: Mapping[str, Any]) -> dict[str, Any]:
    return {key: str(value) if isinstance(value, Path) else value for key, value in settings.items()}


def _never_consulted(job_id: str) -> Mapping[str, Any]:
    raise RelayError(f"A fresh dispatch has no submitted job to inspect: {job_id}")


def _pin_ref(root: Path, ref: str) -> str:
    with tempfile.TemporaryDirectory(prefix="agent-relay-ref-") as temporary:
        return isolation.resolve_ref(root, ref, Path(temporary), 60)


def create(
    manifest: Path,
    root: Path,
    order: Sequence[str],
    settings: Mapping[str, Any],
    kinds: Mapping[str, bool],
    doctor: Lookup,
    submit: Submit,
    max_per_provider: int = 2,
    directory: Path | None = None,
    pin_ref: Callable[[Path, str], str] = _pin_ref,
) -> Path:
    if not order or len(set(order)) != len(order):
        raise RelayError("Providers must be a nonempty list without duplicates.")
    if max_per_provider < 1:
        raise RelayError("max_per_provider must be at least 1.")
    tasks = load_manifest(manifest, kinds)
    health = {name: dict(doctor(name)) for name in order}
    assignments, fallbacks = assign(tasks, order, health, kinds)
    reasons = {item["id"]: item["reason"] for item in fallbacks}
    target = directory if directory is not None else dispatch_root() / uuid.uuid4().hex
    target.mkdir(mode=0o700, parents=True)
    records = []
    for task in tasks:
        fallback = task["id"] in reasons
        records.append(
            {
                **task,
                "provider": assignments.get(task["id"], task["provider"]),
                "state": "needs_native_fallback" if fallback else "pending",
                "job_id": None,
                "output_dir": None,
                "reason": reasons.get(task["id"]),
            }
        )
    counts = {name: {"assigned": list(assignments.values()).count(name), "submitted": 0} for name in order}
    raw_ref = settings.get("ref", "HEAD")
    requested_ref = raw_ref if isinstance(raw_ref, str) else str(raw_ref)
    stored_settings = _json_safe(settings)
    stored_settings["requested_ref"] = requested_ref
    stored_settings["ref"] = pin_ref(root, requested_ref)
    ledger = {
        "schema_version": 1,
        "dispatch_id": target.name,
        "created_at": time.time(),
        "manifest": str(manifest.resolve()),
        "root": str(root.resolve()),
        "providers": list(order),
        "max_per_provider": max_per_provider,
        "settings": stored_settings,
        "doctor": health,
        "tasks": records,
        "needs_native_fallback": fallbacks,
        "counts": counts,
    }
    path = target / "ledger.json"
    write_json(path, ledger)
    advance(path, submit, _never_consulted)
    return path


def _optional_path(value: Any) -> Path | None:
    return None if value is None else Path(value)


def task_arguments(task: Mapping[str, Any], ledger: Mapping[str, Any]) -> argparse.Namespace:
    settings = ledger["settings"]
    return argparse.Namespace(
        provider=task["provider"],
        adapter_file=None,
        registry_dir=_optional_path(settings.get("registry_dir")),
        model=task["model"],
        effort=task["effort"],
        task_file=Path(task["task_file"]),
        root=Path(ledger["root"]),
        files=task["files"],
        output=None,
        kind=task["kind"],
        timeout=settings.get("timeout"),
        max_input_bytes=settings.get("max_input_bytes", 400000),
        max_answer_chars=settings.get("max_answer_chars", 12000),
        jobs_dir=_optional_path(settings.get("jobs_dir")),
        ref=settings.get("ref", "HEAD"),
        owns=task["owns"],
        checks=task["checks"],
        setup=task["setup"],
        check_timeout=settings.get("check_timeout", 180),
    )


@contextmanager
def _locked(ledger_path: Path) -> Iterator[None]:
    with ledger_path.with_suffix(".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read(ledger_path: Path) -> dict[str, Any]:
    ledger: dict[str, Any] = json.loads(ledger_path.read_text(encoding="utf-8"))
    return ledger


def advance(ledger_path: Path, submit: Submit, status: Lookup) -> dict[str, Any]:
    with _locked(ledger_path):
        ledger = _read(ledger_path)
        for name in ledger["providers"]:
            mine = [task for task in ledger["tasks"] if task["provider"] == name]
            active = sum(
                1 for task in mine if task["state"] == "submitted" and status(task["job_id"])["status"] not in TERMINAL
            )
            for task in mine:
                if active >= ledger["max_per_provider"]:
                    break
                if task["state"] != "pending":
                    continue
                task["state"] = "submitting"
                write_json(ledger_path, ledger)
                try:
                    job = submit(task_arguments(task, ledger))
                except (RelayError, OSError, ValueError) as exc:
                    task.update(state="submit_failed", reason=str(exc))
                else:
                    task.update(job_id=job.get("job_id"), output_dir=job.get("output_dir"))
                    if job.get("status") in SUBMIT_FAILED:
                        task.update(state="submit_failed", reason=str(job.get("error") or job.get("status")))
                    else:
                        task["state"] = "submitted"
                        ledger["counts"][name]["submitted"] += 1
                        active += 1
                write_json(ledger_path, ledger)
        return ledger


def _done(ledger: Mapping[str, Any], status: Lookup) -> bool:
    return all(
        task["state"] != "pending" and (task["state"] != "submitted" or status(task["job_id"])["status"] in TERMINAL)
        for task in ledger["tasks"]
    )


def _summary(task: Mapping[str, Any], status: Lookup, result: Lookup) -> dict[str, Any]:
    job_status = status(task["job_id"])["status"] if task["state"] == "submitted" else None
    outcome: Mapping[str, Any] = {}
    if job_status in TERMINAL:
        outcome = result(task["job_id"]).get("result") or {}
    return {
        "id": task["id"],
        "kind": task["kind"],
        "provider": task["provider"],
        "state": task["state"],
        "job_id": task["job_id"],
        "job_status": job_status,
        "status": outcome.get("status"),
        "violations": outcome.get("violations"),
        "checks": outcome.get("checks"),
        "diff_path": outcome.get("diff_path"),
        "clone": outcome.get("clone"),
        "error": outcome.get("error", task["reason"]),
    }


def collect(
    ledger_path: Path,
    submit: Submit,
    status: Lookup,
    result: Lookup,
    timeout: float,
    poll: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    deadline = clock() + timeout
    while True:
        ledger = advance(ledger_path, submit, status)
        complete = _done(ledger, status)
        remaining = deadline - clock()
        if complete or remaining <= 0:
            break
        sleep(min(poll, remaining))
    tasks = [_summary(task, status, result) for task in ledger["tasks"]]
    providers = {
        name: {
            "assigned": counts["assigned"],
            "submitted": counts["submitted"],
            "completed": sum(1 for task in tasks if task["provider"] == name and task["job_status"] in TERMINAL),
            "ok": sum(1 for task in tasks if task["provider"] == name and task["status"] == "ok"),
        }
        for name, counts in ledger["counts"].items()
    }
    return {
        "dispatch_id": ledger["dispatch_id"],
        "ledger": str(ledger_path),
        "complete": complete,
        "tasks": tasks,
        "providers": providers,
        "needs_native_fallback": ledger["needs_native_fallback"],
    }


def cleanup(
    ledger_path: Path,
    result: Lookup,
    remove_clone: Callable[[Path], None] = isolation.remove_clone,
) -> dict[str, Any]:
    ledger = _read(ledger_path)
    removed: list[str] = []
    errors: list[dict[str, str]] = []
    for task in ledger["tasks"]:
        if task["state"] != "submitted":
            continue
        try:
            clone = (result(task["job_id"]).get("result") or {}).get("clone")
            if clone is None:
                continue
            directory = Path(clone).parent
            if str(directory) not in removed:
                remove_clone(directory)
                removed.append(str(directory))
        except (RelayError, OSError, ValueError) as exc:
            errors.append({"id": str(task["id"]), "error": str(exc)})
    return {"dispatch_id": ledger["dispatch_id"], "removed": removed, "errors": errors}
