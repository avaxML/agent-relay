"""Declarative CLI adapters and strict response decoders."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from string import Formatter
from typing import Any


class RelayError(Exception):
    """A delegation cannot produce a usable result."""


class RelayCancelled(RelayError):
    """The owner requested that the worker stop."""


def bundled_adapter_directory() -> Path:
    return Path(__file__).resolve().parent.parent / "adapters"


def registry_directory(given: Path | None = None) -> Path:
    if given is not None:
        path = given.expanduser()
    elif configured := os.environ.get("AGENT_RELAY_ADAPTERS_DIR"):
        path = Path(configured).expanduser()
    elif configured := os.environ.get("XDG_CONFIG_HOME"):
        path = Path(configured).expanduser() / "agent-relay" / "adapters"
    else:
        path = Path.home() / ".config" / "agent-relay" / "adapters"
    if path.is_symlink():
        raise RelayError(f"Adapter registry must not be a symlink: {path}")
    if path.exists() and not path.is_dir():
        raise RelayError(f"Adapter registry must be a directory: {path}")
    return path.resolve()


def validate_provider_name(name: str) -> None:
    if not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name):
        raise RelayError("Provider names use lowercase letters, digits and hyphens.")


def validate_adapter(adapter: Any, name: str) -> dict[str, Any]:
    validate_provider_name(name)
    required = {
        "name",
        "executable",
        "default_model",
        "args",
        "input",
        "output",
        "env",
        "models_args",
        "probe_args",
    }
    if not isinstance(adapter, dict) or not required <= set(adapter) or set(adapter) - required - {"effort"}:
        raise RelayError(f"Adapter requires {', '.join(sorted(required))}; only effort is optional.")
    if adapter["name"] != name:
        raise RelayError("Adapter name does not match --provider.")
    for field in ("name", "executable", "default_model"):
        if not isinstance(adapter[field], str) or not adapter[field].strip():
            raise RelayError(f"Adapter {field} must be a nonempty string.")
    if adapter["input"] not in ("file", "stdin", "agy-stream"):
        raise RelayError("Unknown adapter input format.")
    if adapter["output"] not in ("opencode-jsonl", "agy-jsonl", "cursor-jsonl", "text"):
        raise RelayError("Unknown adapter output format.")
    for field in ("args", "models_args", "probe_args"):
        value = adapter[field]
        if not isinstance(value, list) or not all(isinstance(arg, str) for arg in value):
            raise RelayError(f"Adapter {field} must be a string array.")
        for arg in value:
            for _, key, spec, conversion in Formatter().parse(arg):
                if key is not None and (
                    key not in {"model", "request_file", "timeout", "workdir", "provider_workspace"}
                    or spec
                    or conversion
                ):
                    raise RelayError(f"Unsupported argument placeholder: {key}")
    if not isinstance(adapter["env"], dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in adapter["env"].items()
    ):
        raise RelayError("Adapter env must map strings to strings.")
    if adapter["input"] == "file" and not any("{request_file}" in a for a in adapter["args"]):
        raise RelayError("File transport requires {request_file} in args.")
    if "effort" in adapter:
        validate_effort(adapter["effort"])
    return adapter


def _read_adapter_data(path: Path, expected_name: str | None = None) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink():
        raise RelayError(f"Adapter file must not be a symlink: {path}")
    if not path.is_file():
        raise RelayError(f"Adapter file not found: {path}")
    try:
        data = path.read_bytes()
        adapter = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RelayError(f"Could not read adapter JSON {path}: {exc}") from exc
    name = expected_name if expected_name is not None else adapter.get("name") if isinstance(adapter, dict) else None
    if not isinstance(name, str):
        raise RelayError("Adapter name must be a nonempty string.")
    return validate_adapter(adapter, name), data


def read_adapter(path: Path, expected_name: str | None = None) -> dict[str, Any]:
    adapter, _data = _read_adapter_data(path, expected_name)
    return adapter


def bundled_adapter_names() -> set[str]:
    return {path.stem for path in bundled_adapter_directory().glob("*.json")}


def resolve_adapter_path(name: str, custom: Path | None = None, registry_dir: Path | None = None) -> tuple[Path, str]:
    validate_provider_name(name)
    if custom is not None:
        return custom.expanduser(), "explicit"
    registered = registry_directory(registry_dir) / f"{name}.json"
    if registered.is_symlink():
        raise RelayError(f"Registered adapter must not be a symlink: {registered}")
    if registered.exists():
        if name in bundled_adapter_names():
            raise RelayError(f"Registered adapter shadows bundled provider: {name}")
        if not registered.is_file():
            raise RelayError(f"Registered adapter must be a regular file: {registered}")
        return registered, "registered"
    bundled = bundled_adapter_directory() / f"{name}.json"
    if bundled.is_file() and not bundled.is_symlink():
        return bundled, "bundled"
    raise RelayError(f"Adapter not found: {name}")


def load_adapter(name: str, custom: Path | None = None, registry_dir: Path | None = None) -> dict[str, Any]:
    path, _source = resolve_adapter_path(name, custom, registry_dir)
    return read_adapter(path, name)


def list_adapters(registry_dir: Path | None = None) -> list[dict[str, str]]:
    paths: dict[str, tuple[Path, str]] = {
        name: (bundled_adapter_directory() / f"{name}.json", "bundled") for name in bundled_adapter_names()
    }
    registry = registry_directory(registry_dir)
    if registry.exists():
        for path in registry.glob("*.json"):
            if path.is_symlink():
                raise RelayError(f"Registered adapter must not be a symlink: {path}")
            if not path.is_file():
                raise RelayError(f"Registered adapter must be a regular file: {path}")
            validate_provider_name(path.stem)
            if path.stem in paths:
                raise RelayError(f"Registered adapter shadows bundled provider: {path.stem}")
            paths[path.stem] = (path, "registered")
    records = []
    for name, (path, source) in sorted(paths.items()):
        adapter = read_adapter(path, name)
        records.append(
            {
                "name": name,
                "source": source,
                "path": str(path),
                "executable": adapter["executable"],
                "default_model": adapter["default_model"],
            }
        )
    return records


def _ensure_registry_directory(given: Path | None) -> Path:
    registry = registry_directory(given)
    registry.mkdir(mode=0o700, parents=True, exist_ok=True)
    if registry.is_symlink() or not registry.is_dir():
        raise RelayError(f"Adapter registry must be a regular directory: {registry}")
    registry.chmod(0o700)
    return registry


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _provider_lock(registry: Path, name: str) -> Iterator[None]:
    lock_path = registry / f".{name}.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        if lock_path.is_symlink():
            raise RelayError(f"Adapter lock must not be a symlink: {lock_path}") from exc
        raise RelayError(f"Could not open adapter lock {lock_path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RelayError(f"Adapter lock must be a regular file: {lock_path}")
        os.fchmod(descriptor, 0o600)
        fcntl.lockf(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def install_adapter(source: Path, registry_dir: Path | None = None, replace: bool = False) -> dict[str, str]:
    if source.is_symlink():
        raise RelayError(f"Adapter source must not be a symlink: {source}")
    adapter, data = _read_adapter_data(source)
    name = adapter["name"]
    if name in bundled_adapter_names():
        raise RelayError(f"Cannot register bundled provider: {name}")
    registry = _ensure_registry_directory(registry_dir)
    destination = registry / f"{name}.json"
    with _provider_lock(registry, name):
        if destination.is_symlink():
            raise RelayError(f"Registered adapter must not be a symlink: {destination}")
        if destination.exists():
            if not destination.is_file():
                raise RelayError(f"Registered adapter must be a regular file: {destination}")
            try:
                existing = read_adapter(destination, name)
            except RelayError:
                if not replace:
                    raise RelayError(f"Registered adapter is invalid: {name}; use --replace.") from None
            else:
                if existing == adapter:
                    destination.chmod(0o600)
                    return {"status": "unchanged", "provider": name, "path": str(destination)}
            if not replace:
                raise RelayError(f"Adapter already registered with different content: {name}; use --replace.")
        _atomic_write(destination, data)
    return {"status": "installed", "provider": name, "path": str(destination)}


def remove_adapter(name: str, registry_dir: Path | None = None) -> dict[str, str]:
    validate_provider_name(name)
    if name in bundled_adapter_names():
        raise RelayError(f"Cannot remove bundled provider: {name}")
    registry = registry_directory(registry_dir)
    destination = registry / f"{name}.json"
    if destination.is_symlink():
        raise RelayError(f"Registered adapter must not be a symlink: {destination}")
    if not destination.exists():
        return {"status": "absent", "provider": name, "path": str(destination)}
    if not destination.is_file():
        raise RelayError(f"Registered adapter must be a regular file: {destination}")
    destination.unlink()
    return {"status": "removed", "provider": name, "path": str(destination)}


def validate_effort(config: Any) -> None:
    if not isinstance(config, dict) or set(config) != {"models", "levels"}:
        raise RelayError("effort must contain models and levels.")
    if (
        not isinstance(config["models"], list)
        or not config["models"]
        or not all(isinstance(model, str) and model for model in config["models"])
    ):
        raise RelayError("effort.models must list supported model IDs.")
    levels = config["levels"]
    if not isinstance(levels, dict) or not levels:
        raise RelayError("effort.levels must map effort names to CLI settings.")
    for level, settings in levels.items():
        if not level or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in level):
            raise RelayError("Effort names use lowercase letters, digits and hyphens.")
        if not isinstance(settings, dict) or not settings or set(settings) - {"args", "model"}:
            raise RelayError("Effort settings require args or model, with no other fields.")
        args = settings.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise RelayError("Effort args must be a string array.")
        model = settings.get("model", "{model}")
        if not isinstance(model, str) or not model:
            raise RelayError("Effort model must be a nonempty string.")
        if not args and model == "{model}":
            raise RelayError("Effort settings must change the model or supply CLI arguments.")
        for argument in [*args, model]:
            for _, key, spec, conversion in Formatter().parse(argument):
                if key is not None and (key != "model" or spec or conversion):
                    raise RelayError("Effort settings support only the {model} placeholder.")


def resolve_effort(adapter: dict[str, Any], model: str, effort: str | None) -> tuple[str, list[str]]:
    """Map a requested level explicitly; omission preserves the original invocation."""
    if effort is None:
        return model, []
    config = adapter.get("effort")
    if not config or model not in config["models"]:
        raise RelayError(
            f"No effort mapping for {adapter['name']} model {model}; omit --effort or add a verified mapping."
        )
    if effort not in config["levels"]:
        raise RelayError(f"Unsupported effort {effort}; supported: {', '.join(config['levels'])}.")
    settings = config["levels"][effort]
    return settings.get("model", "{model}").format(model=model), [
        arg.format(model=model) for arg in settings.get("args", [])
    ]


def decode_exit_failure(adapter: dict[str, Any], code: int, stderr_path: Path) -> RelayError:
    if adapter["name"] == "antigravity" and (
        "listen tcp 127.0.0.1:0: bind: operation not permitted"
        in stderr_path.read_text(encoding="utf-8", errors="replace")
    ):
        return RelayError(
            "Antigravity could not start: the host sandbox blocked its localhost listener. "
            "Run or submit with host-approved execution scope for localhost binding and CLI runtime access; "
            "inspect stderr.log. The failed task was not retried."
        )
    return RelayError(f"Worker exited with code {code}; inspect stdout.log and stderr.log.")


def decode_response(raw: str, output: str) -> tuple[str, dict[str, Any]]:
    if output == "text":
        if not raw.strip():
            raise RelayError("Worker returned no text.")
        return raw.strip(), {}
    events = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RelayError("Worker returned malformed JSON output; inspect stdout.log.") from exc
        if not isinstance(event, dict):
            raise RelayError("Worker event must be an object.")
        events.append(event)
    if output == "agy-jsonl":
        results = [e.get("result") for e in events if e.get("event") == "result"]
        if len(results) != 1 or not isinstance(results[0], dict):
            raise RelayError("Antigravity did not return exactly one terminal result.")
        agy_result = results[0]
        if agy_result.get("status") != "SUCCESS":
            raise RelayError("Antigravity reported failure; inspect stdout.log.")
        answer = agy_result.get("response")
        metadata = {
            key: agy_result[key] for key in ("conversation_id", "usage", "duration_seconds") if key in agy_result
        }
    elif output == "cursor-jsonl":
        cursor_results = [event for event in events if event.get("type") == "result"]
        if len(cursor_results) != 1:
            raise RelayError("Cursor did not return exactly one terminal result.")
        cursor_result = cursor_results[0]
        if cursor_result.get("is_error") or cursor_result.get("subtype") != "success":
            raise RelayError("Cursor reported failure; inspect stdout.log.")
        answer = cursor_result.get("result")
        metadata = {
            key: cursor_result[key]
            for key in ("session_id", "request_id", "usage", "duration_ms")
            if key in cursor_result
        }
    else:
        if any(e.get("type") == "error" or e.get("error") for e in events):
            raise RelayError("OpenCode reported failure; inspect stdout.log.")
        parts = [
            e.get("part", {}).get("text") for e in events if e.get("type") == "text" and isinstance(e.get("part"), dict)
        ]
        if not all(isinstance(p, str) for p in parts):
            raise RelayError("OpenCode returned an invalid text event.")
        answer = "\n".join(parts)
        metadata = {"session_ids": sorted({e["sessionID"] for e in events if isinstance(e.get("sessionID"), str)})}
    if not isinstance(answer, str) or not answer.strip():
        raise RelayError("Worker returned no answer.")
    return answer.strip(), metadata
