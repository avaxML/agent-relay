from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from providers import DEFAULT_TIMEOUT, RelayError, load_adapter, resolve_effort, resolve_timeout, validate_effort
from task_runner import run


class EffortTests(unittest.TestCase):
    def test_omission_preserves_model_and_arguments(self) -> None:
        self.assertEqual(
            resolve_effort({"name": "legacy"}, "model#custom", None),
            ("model#custom", []),
        )

    def test_antigravity_selects_matching_model_and_effort(self) -> None:
        adapter = load_adapter("antigravity")
        self.assertEqual(
            resolve_effort(adapter, "gemini-3.8-flash-low", "high"),
            ("gemini-3.8-flash-high", ["--effort", "high"]),
        )
        self.assertEqual(
            resolve_effort(adapter, "gemini-3.8-flash-high", "low"),
            ("gemini-3.8-flash-low", ["--effort", "low"]),
        )

    def test_opencode_selects_variant(self) -> None:
        adapter = load_adapter("opencode")
        self.assertEqual(
            resolve_effort(adapter, "opencode-go/deepseek-v4.1-flash", "max"),
            ("opencode-go/deepseek-v4.1-flash#max", []),
        )

    def test_cursor_selects_exact_grok_model_without_extra_arguments(self) -> None:
        adapter = load_adapter("cursor")
        self.assertEqual(adapter["probe_args"], ["--help"])
        self.assertEqual(adapter["models_args"], ["models"])
        for effort in ("low", "medium", "high", "xhigh"):
            with self.subTest(effort=effort):
                self.assertEqual(
                    resolve_effort(adapter, "cursor-grok-4.6-high", effort),
                    (f"cursor-grok-4.6-{effort}", []),
                )

    def test_unsupported_models_levels_and_legacy_adapters_fail(self) -> None:
        cases = [
            (load_adapter("antigravity"), "gemini-3.8-flash-low", "max"),
            (load_adapter("opencode"), "unverified/model", "high"),
            (load_adapter("opencode"), "opencode-go/deepseek-v4.1-flash#low", "high"),
            ({"name": "legacy"}, "model", "high"),
        ]
        for adapter, model, effort in cases:
            with (
                self.subTest(model=model, effort=effort),
                self.assertRaises(RelayError),
            ):
                resolve_effort(adapter, model, effort)

    def test_rejects_empty_or_malformed_effort_mappings(self) -> None:
        for settings in (
            {},
            {"args": []},
            {"model": "{unknown}"},
            {"args": "--effort high"},
            {"timeout": 600},
            {"model": "{model}#max", "timeout": 0},
            {"model": "{model}#max", "timeout": "600"},
            {"model": "{model}#max", "timeout": True},
        ):
            with self.subTest(settings=settings), self.assertRaises(RelayError):
                validate_effort({"models": ["test"], "levels": {"high": settings}})

    def test_slow_bundled_levels_declare_longer_timeouts(self) -> None:
        cursor = load_adapter("cursor")
        opencode = load_adapter("opencode")
        deepseek = "opencode-go/deepseek-v4.1-flash"
        self.assertEqual(resolve_timeout(cursor, "xhigh", "cursor-grok-4.6-xhigh", None), 600)
        self.assertEqual(resolve_timeout(opencode, "max", f"{deepseek}#max", None), 600)
        self.assertEqual(resolve_timeout(cursor, "high", "cursor-grok-4.6-high", None), DEFAULT_TIMEOUT)
        self.assertEqual(resolve_timeout(cursor, None, "cursor-grok-4.6-high", None), DEFAULT_TIMEOUT)
        self.assertEqual(resolve_timeout(opencode, None, deepseek, None), DEFAULT_TIMEOUT)
        self.assertEqual(resolve_timeout(opencode, "max", f"{deepseek}#max", 90), 90)

    def test_directly_selected_slow_variant_implies_its_timeout(self) -> None:
        self.assertEqual(resolve_timeout(load_adapter("cursor"), None, "cursor-grok-4.6-xhigh", None), 600)
        self.assertEqual(
            resolve_timeout(load_adapter("opencode"), None, "opencode-go/deepseek-v4.1-flash#max", None), 600
        )
        self.assertEqual(resolve_timeout({"name": "legacy"}, None, "model", None), DEFAULT_TIMEOUT)

    def test_custom_effort_reaches_cli_and_result_records_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / "worker.py"
            script.write_text("import sys, json; print(json.dumps(sys.argv[1:]))\n")
            adapter_file = root / "adapter.json"
            adapter_file.write_text(
                json.dumps(
                    {
                        "name": "fake",
                        "executable": sys.executable,
                        "default_model": "base",
                        "args": [str(script), "--model", "{model}", "--timeout", "{timeout}"],
                        "input": "stdin",
                        "output": "text",
                        "env": {},
                        "models_args": [],
                        "probe_args": [],
                        "effort": {
                            "models": ["base"],
                            "levels": {
                                "high": {
                                    "model": "{model}-thinking",
                                    "args": ["--budget", "8192"],
                                    "timeout": 42,
                                }
                            },
                        },
                    }
                )
            )
            task = root / "task.txt"
            task.write_text("Return your invocation arguments.")
            args = argparse.Namespace(
                provider="fake",
                adapter_file=adapter_file,
                model=None,
                effort="high",
                root=root,
                files=[],
                task_file=task,
                kind="read",
                max_input_bytes=10000,
                output=root / "result",
                timeout=None,
                max_answer_chars=1000,
            )
            result = run(args)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(
                json.loads(result["answer"]),
                ["--model", "base-thinking", "--timeout", "42", "--budget", "8192"],
            )
            self.assertEqual(result["timeout"], 42)
            self.assertEqual(result["requested_model"], "base")
            self.assertEqual(result["model"], "base-thinking")
            self.assertEqual(result["effort"], "high")
            args.timeout = 7
            args.output = root / "explicit"
            self.assertEqual(json.loads(run(args)["answer"])[3], "7")
            args.timeout = None
            args.effort = None
            args.model = "base-thinking"
            args.output = root / "direct"
            direct = run(args)
            self.assertEqual(json.loads(direct["answer"]), ["--model", "base-thinking", "--timeout", "42"])
            self.assertEqual(direct["timeout"], 42)
            args.model = None
            args.effort = "unsupported"
            args.output = root / "must-not-exist"
            with self.assertRaises(RelayError):
                run(args)
            self.assertFalse(args.output.exists())


if __name__ == "__main__":
    unittest.main()
