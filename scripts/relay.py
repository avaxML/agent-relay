#!/usr/bin/env python3
"""Delegate bounded coding tasks to installed CLIs. Python 3.11+, POSIX."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import forum
import jobs
from execution import execute
from providers import (
    RelayError,
    install_adapter,
    list_adapters,
    load_adapter,
    read_adapter,
    remove_adapter,
    resolve_adapter_path,
)
from task_runner import KINDS, run


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Must be positive.")
    return number


def nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("Must not be negative.")
    return number


def inspect_provider(
    name: str,
    custom: Path | None,
    registry_dir: Path | None,
    probe: bool,
    models: bool = False,
) -> dict[str, Any]:
    adapter = load_adapter(name, custom, registry_dir)
    executable = shutil.which(adapter["executable"])
    result: dict[str, Any] = {
        "provider": name,
        "executable": executable,
        "default_model": adapter["default_model"],
        "installed": bool(executable),
        "authentication": "not checked",
        "effort": adapter.get("effort"),
    }
    if executable and (probe or models):
        with tempfile.TemporaryDirectory(prefix="agent-relay-doctor-") as temp:
            directory = Path(temp)
            stdin = directory / "stdin"
            stdin.touch()
            argv = [executable] + (adapter["models_args"] if models else adapter["probe_args"])
            code = execute(argv, directory, {**os.environ, **adapter["env"]}, stdin, directory, 30)
            result.update(
                exit_code=code,
                output=(directory / "stdout.log").read_text(errors="replace")[:12000],
                stderr=(directory / "stderr.log").read_text(errors="replace")[:2000],
            )
    return result


def adapter_validation_target(value: str, registry_dir: Path | None) -> tuple[str, Path | None]:
    path = Path(value).expanduser()
    if path.is_absolute() or path.parent != Path(".") or path.suffix == ".json" or path.exists() or path.is_symlink():
        adapter = read_adapter(path)
        return adapter["name"], path
    return value, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("doctor", "models", "run", "submit"):
        sub = commands.add_parser(command)
        sub.add_argument("--provider", required=command != "doctor")
        sub.add_argument("--adapter-file", type=Path)
        sub.add_argument("--registry-dir", type=Path)
        if command == "doctor":
            sub.add_argument("--probe", action="store_true", help="Run CLI help, without inference.")
        if command in ("run", "submit"):
            sub.add_argument("--model")
            sub.add_argument(
                "--effort", help="Reasoning level declared by the adapter; omitted keeps provider defaults."
            )
            sub.add_argument("--task-file", type=Path, required=True)
            sub.add_argument("--root", type=Path, required=True)
            sub.add_argument("--files", nargs="+", required=True)
            sub.add_argument(
                "--output", type=Path, required=command == "run", help="New artifact directory; never overwritten."
            )
            sub.add_argument("--kind", choices=KINDS, default="read")
            sub.add_argument("--timeout", type=positive_int, default=180)
            sub.add_argument("--max-input-bytes", type=positive_int, default=400000)
            sub.add_argument("--max-answer-chars", type=positive_int, default=12000)
        if command == "submit":
            sub.add_argument("--jobs-dir", type=Path)
    adapter = commands.add_parser("adapter")
    adapter_commands = adapter.add_subparsers(dest="adapter_command", required=True)
    install = adapter_commands.add_parser("install")
    install.add_argument("path", type=Path)
    install.add_argument("--replace", action="store_true")
    install.add_argument("--registry-dir", type=Path)
    listing = adapter_commands.add_parser("list")
    listing.add_argument("--registry-dir", type=Path)
    validate = adapter_commands.add_parser("validate")
    validate.add_argument("name_or_path")
    validate.add_argument("--probe", action="store_true")
    validate.add_argument("--registry-dir", type=Path)
    remove = adapter_commands.add_parser("remove")
    remove.add_argument("name")
    remove.add_argument("--registry-dir", type=Path)
    for command in ("status", "wait", "result", "cancel"):
        sub = commands.add_parser(command)
        sub.add_argument("job_id")
        sub.add_argument("--jobs-dir", type=Path)
        if command == "wait":
            sub.add_argument(
                "--timeout", type=nonnegative_int, default=30, help="Wait deadline; does not cancel the job."
            )
    forum.register_cli(commands)
    args = parser.parse_args()
    result: dict[str, Any]
    try:
        if args.command == "adapter":
            if args.adapter_command == "install":
                result = install_adapter(args.path, args.registry_dir, args.replace)
                code = 0
            elif args.adapter_command == "list":
                result = {"adapters": list_adapters(args.registry_dir)}
                code = 0
            elif args.adapter_command == "remove":
                result = remove_adapter(args.name, args.registry_dir)
                code = 0
            else:
                name, custom = adapter_validation_target(args.name_or_path, args.registry_dir)
                result = inspect_provider(name, custom, args.registry_dir, args.probe)
                result["status"] = "valid"
                path, source = resolve_adapter_path(name, custom, args.registry_dir)
                result.update(path=str(path), source=source)
                code = 0 if not args.probe or result["installed"] and result.get("exit_code") == 0 else 1
        elif args.command == "run":
            result = run(args)
            code = 0 if result["status"] == "ok" else 1
        elif args.command == "submit":
            result = jobs.submit(args)
            code = 1 if result["status"] in {"failed", "cancelled", "interrupted"} else 0
        elif args.command in ("status", "wait", "result", "cancel"):
            root = jobs.job_root(args.jobs_dir)
            if args.command == "wait":
                result = jobs.wait(args.job_id, root, args.timeout)
            else:
                result = getattr(jobs, args.command)(args.job_id, root)
            if args.command in ("status", "cancel") or result["status"] == "completed":
                code = 0
            elif result["status"] in jobs.TERMINAL:
                code = 1
            else:
                code = 2
        elif args.command == "forum":
            result, code = forum.dispatch(args)
        else:
            if args.adapter_file and not args.provider:
                raise RelayError("--adapter-file requires --provider.")
            names = (
                [args.provider] if args.provider else [record["name"] for record in list_adapters(args.registry_dir)]
            )
            result = {
                "providers": [
                    inspect_provider(
                        n,
                        args.adapter_file,
                        args.registry_dir,
                        getattr(args, "probe", False),
                        args.command == "models",
                    )
                    for n in names
                ]
            }
            code = 0 if all(p["installed"] and p.get("exit_code", 0) == 0 for p in result["providers"]) else 1
        print(json.dumps(result, ensure_ascii=False))
        return code
    except (RelayError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
