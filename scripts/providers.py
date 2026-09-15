"""Declarative CLI adapters and strict response decoders."""

from __future__ import annotations

import json
from pathlib import Path
from string import Formatter
from typing import Any


class RelayError(Exception):
    """A delegation cannot produce a usable result."""


class RelayCancelled(RelayError):
    """The owner requested that the worker stop."""


def load_adapter(name: str, custom: Path | None = None) -> dict[str, Any]:
    path = custom or Path(__file__).resolve().parent.parent / "adapters" / f"{name}.json"
    if not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name):
        raise RelayError("Provider names use lowercase letters, digits and hyphens.")
    adapter = json.loads(path.read_text())
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
    if adapter["output"] not in ("opencode-jsonl", "agy-jsonl", "text"):
        raise RelayError("Unknown adapter output format.")
    for field in ("args", "models_args", "probe_args"):
        value = adapter[field]
        if not isinstance(value, list) or not all(isinstance(arg, str) for arg in value):
            raise RelayError(f"Adapter {field} must be a string array.")
        for arg in value:
            for _, key, spec, conversion in Formatter().parse(arg):
                if key is not None and (
                    key not in {"model", "request_file", "timeout", "workdir"} or spec or conversion
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
        result = results[0]
        if result.get("status") != "SUCCESS":
            raise RelayError("Antigravity reported failure; inspect stdout.log.")
        answer = result.get("response")
        metadata = {k: result[k] for k in ("conversation_id", "usage", "duration_seconds") if k in result}
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
