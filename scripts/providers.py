"""Declarative CLI adapters and strict response decoders."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from string import Formatter
from typing import Any

DEFAULT_TIMEOUT = 180
PLACEHOLDERS = frozenset({"model", "request_file", "timeout", "workdir", "provider_workspace"})
TOOLS_PLACEHOLDERS = PLACEHOLDERS | {"clone"}
BLANKET_FLAGS = frozenset(
    {"--force", "--yolo", "--trust", "--auto", "--auto-review", "--dangerously-skip-permissions", "--approve-mcps"}
)
LIMIT_FIELDS = frozenset({"max_log_bytes", "max_event_bytes"})
# Sixteen times the default probe/execute budget, and the most an adapter, flag or default may set; the decoders
# read a whole log at once, so this also bounds their memory.
MAX_LOG_BYTES = 256 << 20
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class RelayError(Exception):
    """A delegation cannot produce a usable result."""


class RelayStopped(RelayError):
    """Relay stopped a running provider; the worker's partial work may still be salvaged."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class RelayCancelled(RelayStopped):
    """The owner requested that the worker stop."""

    def __init__(self, message: str) -> None:
        super().__init__(message, "cancelled")


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
    optional = {"effort", "tools", "limits"}
    if not isinstance(adapter, dict) or not required <= set(adapter) or set(adapter) - required - optional:
        raise RelayError(f"Adapter requires {', '.join(sorted(required))}; only effort, tools and limits are optional.")
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
        _check_placeholders(value, PLACEHOLDERS)
        _reject_blanket_flags(value, field)
    if not isinstance(adapter["env"], dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in adapter["env"].items()
    ):
        raise RelayError("Adapter env must map strings to strings.")
    if adapter["input"] == "file" and not any("{request_file}" in a for a in adapter["args"]):
        raise RelayError("File transport requires {request_file} in args.")
    if "effort" in adapter:
        validate_effort(adapter["effort"])
    if "tools" in adapter:
        validate_tools(adapter["tools"], adapter["input"])
    if "limits" in adapter:
        validate_limits(adapter["limits"])
    return adapter


def validate_limits(config: Any) -> None:
    if not isinstance(config, dict) or set(config) - LIMIT_FIELDS:
        raise RelayError("Adapter limits may set only max_log_bytes and max_event_bytes.")
    for field, value in config.items():
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise RelayError(f"Adapter limits.{field} must be a positive integer number of bytes.")
    if config.get("max_log_bytes", 0) > MAX_LOG_BYTES or config.get("max_event_bytes", 0) > MAX_LOG_BYTES // 4:
        raise RelayError(
            f"Adapter limits must keep max_log_bytes at most {MAX_LOG_BYTES} (256 MiB) and max_event_bytes at most a quarter of that."
        )
    if config.get("max_log_bytes", 4096) < 4096:
        raise RelayError("Adapter limits.max_log_bytes must be at least 4096 bytes (four 1024-byte events).")
    if config.get("max_event_bytes", 1024) < 1024:
        raise RelayError("Adapter limits.max_event_bytes must be at least 1024 bytes.")
    if set(config) >= LIMIT_FIELDS and config["max_event_bytes"] * 4 > config["max_log_bytes"]:
        raise RelayError("Adapter limits.max_event_bytes must be at most a quarter of limits.max_log_bytes.")


def _check_placeholders(values: list[str], allowed: frozenset[str]) -> None:
    for arg in values:
        for _, key, spec, conversion in Formatter().parse(arg):
            if key is not None and (key not in allowed or spec or conversion):
                raise RelayError(f"Unsupported argument placeholder: {key}")


def _reject_blanket_flags(values: list[str], field: str) -> None:
    for arg in values:
        name = arg.split("=", 1)[0]
        if name in BLANKET_FLAGS:
            raise RelayError(
                f"Adapter {field} must not contain auto-approval flag {name}; declare it only in tools.args."
            )


def validate_tools(config: Any, input_kind: str) -> None:
    if not isinstance(config, dict) or "args" not in config or set(config) - {"args", "env", "pass_env"}:
        raise RelayError("Adapter tools requires args; only env and pass_env are optional.")
    args = config["args"]
    if not isinstance(args, list) or not args or not all(isinstance(arg, str) for arg in args):
        raise RelayError("Adapter tools.args must be a nonempty string array.")
    _check_placeholders(args, TOOLS_PLACEHOLDERS)
    env = config.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise RelayError("Adapter tools.env must map strings to strings.")
    pass_env = config.get("pass_env", [])
    if not isinstance(pass_env, list) or not all(
        isinstance(name, str) and ENV_NAME.fullmatch(name) for name in pass_env
    ):
        raise RelayError("Adapter tools.pass_env must list environment variable names.")
    for name in (*pass_env, *env):
        if name.startswith("GIT_") or name == "SSH_AUTH_SOCK":
            raise RelayError(f"Adapter tools must not pass or set {name}.")
    if input_kind == "file" and not any("{request_file}" in arg for arg in args):
        raise RelayError("Adapter tools.args require {request_file} for file transport.")


def resolve_invocation(adapter: dict[str, Any], tools: bool) -> tuple[list[str], dict[str, str]]:
    if not tools:
        return adapter["args"], adapter["env"]
    if "tools" not in adapter:
        raise RelayError(f"provider {adapter['name']} has no verified tools mode")
    return adapter["tools"]["args"], adapter["tools"].get("env", {})


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
        if not isinstance(settings, dict) or not settings or set(settings) - {"args", "model", "timeout"}:
            raise RelayError("Effort settings require args or model, plus an optional timeout.")
        timeout = settings.get("timeout", 1)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise RelayError("Effort timeout must be a positive integer number of seconds.")
        args = settings.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise RelayError("Effort args must be a string array.")
        _reject_blanket_flags(args, f"effort.levels.{level}.args")
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


def resolve_timeout(adapter: dict[str, Any], effort: str | None, model: str, timeout: int | None) -> int:
    """An explicit timeout wins; otherwise slower effort levels may declare a longer default.

    Without --effort, a model ID that a level selects (such as an xhigh variant) implies that level.
    """
    if timeout is not None:
        return timeout
    config = adapter.get("effort")
    if not config:
        return DEFAULT_TIMEOUT
    if effort is not None:
        return int(config["levels"][effort].get("timeout", DEFAULT_TIMEOUT))
    for settings in config["levels"].values():
        variant = settings.get("model", "{model}")
        if variant != "{model}" and any(variant.format(model=base) == model for base in config["models"]):
            return int(settings.get("timeout", DEFAULT_TIMEOUT))
    return DEFAULT_TIMEOUT


AGY_LISTENER_BLOCKED = "listen tcp 127.0.0.1:0: bind: operation not permitted"
# Exit diagnostics decode_exit_failure looks for, watched while stderr streams: a bounded stderr.log may drop them.
EXIT_SIGNATURES = {"antigravity": (AGY_LISTENER_BLOCKED,)}


def exit_signatures(adapter: dict[str, Any]) -> tuple[str, ...]:
    return EXIT_SIGNATURES.get(adapter["name"], ())


def decode_exit_failure(
    adapter: dict[str, Any], code: int, stderr_path: Path, seen: Collection[str] = ()
) -> RelayError:
    if adapter["name"] == "antigravity" and (
        AGY_LISTENER_BLOCKED in seen
        or AGY_LISTENER_BLOCKED in stderr_path.read_text(encoding="utf-8", errors="replace")
    ):
        return RelayError(
            "Antigravity could not start: the host sandbox blocked its localhost listener. "
            "Run or submit with host-approved execution scope for localhost binding and CLI runtime access; "
            "inspect stderr.log. The failed task was not retried."
        )
    return RelayError(f"Worker exited with code {code}; inspect stdout.log and stderr.log.")


def _lines(raw: str) -> Iterator[str]:
    """LF-separated lines, as the bounded writer frames events, without materialising a list of all of them."""
    start = 0
    while start <= len(raw):
        end = raw.find("\n", start)
        if end < 0:
            end = len(raw)
        yield raw[start:end]
        start = end + 1


def decode_response(raw: str, output: str) -> tuple[str, dict[str, Any]]:
    if output == "text":
        if not raw.strip():
            raise RelayError("Worker returned no text.")
        return raw.strip(), {}
    # One pass that keeps only what the decision needs, so millions of small events cannot exhaust memory.
    # Only the first terminal result and a count: more than one is already a failure.
    first_result: Any = None
    result_count = 0
    parts: list[Any] = []
    session_ids: set[str] = set()
    failed = False
    for line in _lines(raw):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError) as exc:
            raise RelayError("Worker returned malformed JSON output; inspect stdout.log.") from exc
        if not isinstance(event, dict):
            raise RelayError("Worker event must be an object.")
        if output == "agy-jsonl":
            if event.get("event") == "result":
                result_count += 1
                first_result = event.get("result") if result_count == 1 else first_result
        elif output == "cursor-jsonl":
            if event.get("type") == "result":
                result_count += 1
                first_result = event if result_count == 1 else first_result
        else:
            failed = failed or event.get("type") == "error" or bool(event.get("error"))
            if event.get("type") == "text" and isinstance(event.get("part"), dict):
                parts.append(event["part"].get("text"))
            if isinstance(event.get("sessionID"), str):
                session_ids.add(event["sessionID"])
    if output == "agy-jsonl":
        if result_count != 1 or not isinstance(first_result, dict):
            raise RelayError("Antigravity did not return exactly one terminal result.")
        agy_result = first_result
        if agy_result.get("status") != "SUCCESS":
            raise RelayError("Antigravity reported failure; inspect stdout.log.")
        answer = agy_result.get("response")
        metadata = {
            key: agy_result[key] for key in ("conversation_id", "usage", "duration_seconds") if key in agy_result
        }
    elif output == "cursor-jsonl":
        if result_count != 1:
            raise RelayError("Cursor did not return exactly one terminal result.")
        cursor_result = first_result
        if cursor_result.get("is_error") or cursor_result.get("subtype") != "success":
            raise RelayError("Cursor reported failure; inspect stdout.log.")
        answer = cursor_result.get("result")
        metadata = {
            key: cursor_result[key]
            for key in ("session_id", "request_id", "usage", "duration_ms")
            if key in cursor_result
        }
    else:
        if failed:
            raise RelayError("OpenCode reported failure; inspect stdout.log.")
        if not all(isinstance(p, str) for p in parts):
            raise RelayError("OpenCode returned an invalid text event.")
        answer = "\n".join(parts)
        metadata = {"session_ids": sorted(session_ids)}
    if not isinstance(answer, str) or not answer.strip():
        raise RelayError("Worker returned no answer.")
    return answer.strip(), metadata


# Protection levels for bounded provider logs. Ordinary events are clipped past max_event_bytes and stop at three
# quarters of the cap. Partial-answer events may use half the reserve and terminal events (the final answer or a
# failure) the whole cap; neither is clipped for size, only to fit the space left, and a clipped or dropped terminal
# event fails the run.
ORDINARY, PARTIAL, TERMINAL = 0, 1, 2


# Comparisons, not hashing: a hostile event may carry a list or object where a type string belongs.
def _cursor_level(event: dict[str, Any]) -> int:
    kind = event.get("type")
    return TERMINAL if kind == "result" else PARTIAL if kind == "assistant" else ORDINARY


def _agy_level(event: dict[str, Any]) -> int:
    return TERMINAL if event.get("event") == "result" else ORDINARY


def _opencode_level(event: dict[str, Any]) -> int:
    # Exactly the decoder's failure test: a truthy error, not merely an error key.
    return TERMINAL if event.get("type") in ("text", "error") or event.get("error") else ORDINARY


EVENT_LEVELS: dict[str, Callable[[dict[str, Any]], int]] = {
    "cursor-jsonl": _cursor_level,
    "agy-jsonl": _agy_level,
    "opencode-jsonl": _opencode_level,
}


def event_classifier(output: str) -> Callable[[dict[str, Any]], int] | None:
    """Return how decode_response depends on each event of a JSONL output, or None for unstructured text."""
    return EVENT_LEVELS.get(output)


def _assistant_texts(event: dict[str, Any]) -> list[Any]:
    message = event.get("message")
    if (
        event.get("type") != "assistant"
        or not isinstance(message, dict)
        or not isinstance(message.get("content"), list)
    ):
        return []
    return [item.get("text") for item in message["content"] if isinstance(item, dict)]


def _answer_texts(event: dict[str, Any], output: str) -> list[Any]:
    """The answer text one event carries, if any."""
    if output == "cursor-jsonl":
        if event.get("type") == "assistant":
            return _assistant_texts(event)
        # A failed, malformed or clipped result still carries the provider's last words.
        return [event.get("result")] if event.get("type") == "result" else []
    if output == "agy-jsonl":
        result = event.get("result")
        return [result.get("response")] if event.get("event") == "result" and isinstance(result, dict) else []
    part = event.get("part")
    return [part.get("text")] if event.get("type") == "text" and isinstance(part, dict) else []


def decode_partial(raw: str, output: str) -> str | None:
    """Best-effort answer from a stopped worker's log; malformed or missing events are skipped.

    One pass that keeps only answer strings, never event objects, so memory stays near the size of the text.
    """
    if output == "text":
        return raw.strip() or None
    level = EVENT_LEVELS[output]
    parts: list[str] = []
    clipped = False
    for line in _lines(raw):
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict):
            continue
        parts += [text for text in _answer_texts(event, output) if isinstance(text, str)]
        # A terminal event Relay clipped to fit would decode as if it were whole. Then keep every answer text,
        # assistant and result alike: the clip may have shortened only other fields.
        clipped = clipped or (level(event) == TERMINAL and "relay_truncated_bytes" in event)
    if not clipped:
        # ValueError and RecursionError: one hostile event (huge integer, deep nesting) must not cost the rest.
        with suppress(RelayError, ValueError, RecursionError):
            return decode_response(raw, output)[0]
    answer = "\n".join(parts).strip()
    return answer or None
