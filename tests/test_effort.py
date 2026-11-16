from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from providers import DEFAULT_TIMEOUT, RelayError, load_adapter, resolve_effort, resolve_timeout, validate_effort
from relay import inspect_provider
from task_runner import run


class EffortTests(unittest.TestCase):
    def test_omission_preserves_model_and_arguments(self) -> None:
        self.assertEqual(
            resolve_effort({"name": "legacy"}, "model#custom", None),
            ("model#custom", []),
        )

    def test_antigravity_selects_matching_model_and_effort(self) -> None:
        adapter = load_adapter("antigravity")
        for model, effort, selected in (
            ("gemini-4-argon-low", "high", "gemini-4-argon-high"),
            ("gemini-4-hydrogen-low", "xhigh", "gemini-4-hydrogen-xhigh"),
            ("gemini-4-hydrogen-high", "low", "gemini-4-hydrogen-low"),
            ("gemini-3.8-flash-low", "high", "gemini-3.8-flash-high"),
            ("gemini-3.8-flash-high", "low", "gemini-3.8-flash-low"),
        ):
            with self.subTest(model=model, effort=effort):
                self.assertEqual(resolve_effort(adapter, model, effort), (selected, ["--effort", effort]))

    def test_antigravity_rejects_levels_outside_requested_family(self) -> None:
        with self.assertRaises(RelayError) as raised:
            resolve_effort(load_adapter("antigravity"), "gemini-4-argon-low", "xhigh")
        self.assertEqual(str(raised.exception), "Unsupported effort xhigh; supported: low, medium, high.")

    def test_antigravity_unknown_model_preserves_error_text(self) -> None:
        with self.assertRaises(RelayError) as raised:
            resolve_effort(load_adapter("antigravity"), "unknown-model", "high")
        self.assertEqual(
            str(raised.exception),
            "No effort mapping for antigravity model unknown-model; omit --effort or add a verified mapping.",
        )

    def test_antigravity_default_family_reaches_fake_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / "worker.py"
            script.write_text("import json, sys; print(json.dumps(sys.argv[1:]))\n")
            adapter = load_adapter("antigravity")
            adapter.update(
                executable=sys.executable,
                args=[str(script), "--model", "{model}"],
                input="stdin",
                output="text",
            )
            adapter_file = root / "adapter.json"
            adapter_file.write_text(json.dumps(adapter))
            task = root / "task.txt"
            task.write_text("Return your invocation arguments.")
            result = run(
                argparse.Namespace(
                    provider="antigravity",
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
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["requested_model"], "gemini-4-hydrogen-low")
            self.assertEqual(result["model"], "gemini-4-hydrogen-high")
            self.assertEqual(json.loads(result["answer"]), ["--model", "gemini-4-hydrogen-high", "--effort", "high"])

    def test_grok_passes_reasoning_effort_without_changing_the_model(self) -> None:
        adapter = load_adapter("grok")
        for model in ("grok-4.7", "grok-4.7-build-fast", "grok-4.6"):
            for level in ("low", "medium", "high", "xhigh"):
                with self.subTest(model=model, level=level):
                    self.assertEqual(resolve_effort(adapter, model, level), (model, ["--reasoning-effort", level]))
        with self.assertRaises(RelayError):
            resolve_effort(adapter, "grok-4.7", "max")
        with self.assertRaises(RelayError):
            resolve_effort(adapter, "grok-4.7-high", "high")
        # grok-4.5 rejects xhigh live, so it has no effort mapping; it still runs with --model and no --effort.
        with self.assertRaises(RelayError):
            resolve_effort(adapter, "grok-4.5", "low")
        self.assertEqual(resolve_effort(adapter, "grok-4.5", None), ("grok-4.5", []))
        self.assertEqual(resolve_effort(adapter, "grok-4.7", None), ("grok-4.7", []))
        # Measured on a 172 KB review: medium 580 s, high 838 s, xhigh 987 s.
        for level, seconds in (("low", DEFAULT_TIMEOUT), ("medium", 900), ("high", 1200), ("xhigh", 1500)):
            with self.subTest(level=level):
                self.assertEqual(resolve_timeout(adapter, level, "grok-4.7", None), seconds)
        self.assertEqual(resolve_timeout(adapter, None, "grok-4.7", None), DEFAULT_TIMEOUT)
        self.assertEqual(resolve_timeout(adapter, "xhigh", "grok-4.7", 60), 60)

    def test_opencode_selects_variant(self) -> None:
        adapter = load_adapter("opencode")
        for level in ("low", "medium", "high", "max"):
            with self.subTest(level=level):
                self.assertEqual(
                    resolve_effort(adapter, "opencode-go/deepseek-v4.1-flash", level),
                    (f"opencode-go/deepseek-v4.1-flash#{level}", []),
                )

    def test_single_object_doctor_effort_is_byte_identical(self) -> None:
        expected = {
            "cursor": {
                "models": ["grok-4.7-low", "grok-4.7-medium", "grok-4.7-high", "grok-4.7-xhigh"],
                "levels": {
                    "low": {"model": "grok-4.7-low"},
                    "medium": {"model": "grok-4.7-medium"},
                    "high": {"model": "grok-4.7-high"},
                    "xhigh": {"model": "grok-4.7-xhigh", "timeout": 600},
                },
            },
            "opencode": {
                "models": ["opencode-go/deepseek-v4.1-flash"],
                "levels": {
                    "low": {"model": "{model}#low"},
                    "medium": {"model": "{model}#medium"},
                    "high": {"model": "{model}#high"},
                    "max": {"model": "{model}#max", "timeout": 600},
                },
            },
        }
        for provider, effort in expected.items():
            with self.subTest(provider=provider):
                report = inspect_provider(provider, None, None, probe=False)
                self.assertEqual(json.dumps(report["effort"]), json.dumps(effort))

    def test_antigravity_doctor_reports_all_effort_families(self) -> None:
        report = inspect_provider("antigravity", None, None, probe=False)
        expected: list[dict[str, Any]] = []
        for prefix, levels in (
            ("gemini-4-hydrogen", ("low", "medium", "high", "xhigh")),
            ("gemini-4-argon", ("low", "medium", "high")),
            ("gemini-3.8-flash", ("low", "medium", "high")),
        ):
            expected.append(
                {
                    "models": [f"{prefix}-{level}" for level in levels],
                    "levels": {level: {"args": ["--effort", level], "model": f"{prefix}-{level}"} for level in levels},
                }
            )
        expected[0]["levels"]["xhigh"]["timeout"] = 600
        self.assertEqual(report["effort"], expected)
        self.assertEqual(report["default_model"], "gemini-4-hydrogen-low")

    def test_cursor_selects_exact_grok_model_without_extra_arguments(self) -> None:
        adapter = load_adapter("cursor")
        self.assertEqual(adapter["probe_args"], ["--help"])
        self.assertEqual(adapter["models_args"], ["models"])
        for effort in ("low", "medium", "high", "xhigh"):
            with self.subTest(effort=effort):
                self.assertEqual(
                    resolve_effort(adapter, "grok-4.7-high", effort),
                    (f"grok-4.7-{effort}", []),
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

    def test_validates_single_object_and_multiple_effort_families(self) -> None:
        first = {"models": ["first"], "levels": {"high": {"model": "first-high"}}}
        second = {"models": ["second"], "levels": {"low": {"args": ["--effort", "low"]}}}
        validate_effort(first)
        validate_effort([first, second])

    def test_rejects_model_id_in_multiple_effort_families(self) -> None:
        with self.assertRaisesRegex(RelayError, "shared-model"):
            validate_effort(
                [
                    {"models": ["shared-model"], "levels": {"high": {"model": "first-high"}}},
                    {"models": ["shared-model"], "levels": {"low": {"model": "second-low"}}},
                ]
            )

    def test_rejects_concrete_level_model_in_multiple_effort_families(self) -> None:
        with self.assertRaisesRegex(RelayError, "shared-variant"):
            validate_effort(
                [
                    {"models": ["first"], "levels": {"high": {"model": "shared-variant"}}},
                    {"models": ["second"], "levels": {"low": {"model": "shared-variant"}}},
                ]
            )

    def test_rejects_level_model_listed_in_another_family(self) -> None:
        with self.assertRaisesRegex(RelayError, "first"):
            validate_effort(
                [
                    {"models": ["first"], "levels": {"high": {"model": "first-high"}}},
                    {"models": ["second"], "levels": {"low": {"model": "first"}}},
                ]
            )

    def test_rejects_expanded_variant_in_another_family(self) -> None:
        for first in (
            {"models": ["base#max"], "levels": {"high": {"model": "other"}}},
            {"models": ["first"], "levels": {"high": {"model": "base#max"}}},
            {"models": ["base"], "levels": {"high": {"model": "{model}#max"}}},
        ):
            with self.subTest(first=first), self.assertRaisesRegex(RelayError, "base#max"):
                validate_effort([first, {"models": ["second", "base#"], "levels": {"max": {"model": "{model}max"}}}])

    def test_rejects_empty_list_and_malformed_effort_families(self) -> None:
        valid = {"models": ["base"], "levels": {"high": {"model": "base-high"}}}
        for config, message in (
            ([], "effort must contain models and levels."),
            ([valid, None], "effort must contain models and levels."),
            ([valid, {}], "effort must contain models and levels."),
            ([{"models": [], "levels": {"high": {"model": "high"}}}], "effort.models must list supported model IDs."),
            ([{"models": ["base"], "levels": {}}], "effort.levels must map effort names to CLI settings."),
            (
                [{"models": ["base"], "levels": {"high": {"timeout": 0}}}],
                "Effort timeout must be a positive integer number of seconds.",
            ),
        ):
            with self.subTest(config=config), self.assertRaises(RelayError) as raised:
                validate_effort(config)
            self.assertEqual(str(raised.exception), message)

    def test_slow_bundled_levels_declare_longer_timeouts(self) -> None:
        cursor = load_adapter("cursor")
        opencode = load_adapter("opencode")
        antigravity = load_adapter("antigravity")
        self.assertEqual(resolve_timeout(antigravity, "xhigh", "gemini-4-hydrogen-low", None), 600)
        self.assertEqual(resolve_timeout(antigravity, "high", "gemini-4-hydrogen-high", None), 180)
        self.assertEqual(resolve_timeout(antigravity, "xhigh", "gemini-4-hydrogen-low", 90), 90)
        deepseek = "opencode-go/deepseek-v4.1-flash"
        self.assertEqual(resolve_timeout(cursor, "xhigh", "grok-4.7-xhigh", None), 600)
        self.assertEqual(resolve_timeout(opencode, "max", f"{deepseek}#max", None), 600)
        self.assertEqual(resolve_timeout(cursor, "high", "grok-4.7-high", None), DEFAULT_TIMEOUT)
        self.assertEqual(resolve_timeout(cursor, None, "grok-4.7-high", None), DEFAULT_TIMEOUT)
        self.assertEqual(resolve_timeout(opencode, None, deepseek, None), DEFAULT_TIMEOUT)
        self.assertEqual(resolve_timeout(opencode, "max", f"{deepseek}#max", 90), 90)

    def test_timeout_uses_only_the_requested_family(self) -> None:
        adapter = load_adapter("antigravity")
        for model, effort in (
            ("gemini-4-argon-low", "xhigh"),
            ("gemini-3.8-flash-low", "xhigh"),
            ("gemini-4-hydrogen-low", "missing"),
            ("unknown-model", "xhigh"),
        ):
            with self.subTest(model=model, effort=effort):
                self.assertEqual(resolve_timeout(adapter, effort, model, None), DEFAULT_TIMEOUT)
                self.assertEqual(resolve_timeout(adapter, effort, model, 7), 7)
        self.assertEqual(resolve_timeout(load_adapter("cursor"), "xhigh", "unknown-model", None), DEFAULT_TIMEOUT)
        self.assertEqual(
            resolve_timeout(
                {
                    "effort": [
                        {"models": ["first"], "levels": {"high": {"args": ["--high"], "timeout": 600}}},
                        {"models": ["second"], "levels": {"high": {"args": ["--high"], "timeout": 42}}},
                    ]
                },
                "high",
                "second",
                None,
            ),
            42,
        )

    def test_list_timeout_matches_concrete_variant_when_effort_is_given(self) -> None:
        adapter = {"effort": [{"models": ["base"], "levels": {"high": {"model": "base-high", "timeout": 600}}}]}
        self.assertEqual(resolve_timeout(adapter, "high", "base-high", None), 600)
        self.assertEqual(resolve_timeout(adapter, None, "base-high", None), 600)

    def test_list_timeout_matches_second_family_template_variant(self) -> None:
        adapter = {
            "effort": [
                {"models": ["first"], "levels": {"high": {"model": "first-high"}}},
                {"models": ["base"], "levels": {"max": {"model": "{model}#max", "timeout": 600}}},
            ]
        }
        validate_effort(adapter["effort"])
        self.assertEqual(resolve_timeout(adapter, "max", "base#max", None), 600)
        self.assertEqual(resolve_timeout(adapter, "max", "base#max", 7), 7)
        self.assertEqual(resolve_timeout(adapter, None, "base#max", None), 600)
        self.assertEqual(resolve_timeout(adapter, "missing", "base#max", None), DEFAULT_TIMEOUT)

    def test_directly_selected_slow_variant_implies_its_timeout(self) -> None:
        self.assertEqual(resolve_timeout(load_adapter("antigravity"), None, "gemini-4-hydrogen-xhigh", None), 600)
        self.assertEqual(resolve_timeout(load_adapter("antigravity"), None, "gemini-4-hydrogen-high", None), 180)
        self.assertEqual(resolve_timeout(load_adapter("cursor"), None, "grok-4.7-xhigh", None), 600)
        self.assertEqual(
            resolve_timeout(load_adapter("opencode"), None, "opencode-go/deepseek-v4.1-flash#max", None), 600
        )
        self.assertEqual(resolve_timeout({"name": "legacy"}, None, "model", None), DEFAULT_TIMEOUT)

    def test_cursor_rejects_previous_generation_and_keeps_composer_without_effort(self) -> None:
        cursor = load_adapter("cursor")
        for model in ("cursor-grok-4.6-high", "composer-2.5"):
            with self.subTest(model=model), self.assertRaises(RelayError):
                resolve_effort(cursor, model, "high")
        self.assertEqual(resolve_effort(cursor, "composer-2.5", None), ("composer-2.5", []))
        self.assertEqual(resolve_timeout(cursor, None, "composer-2.5", None), DEFAULT_TIMEOUT)
        self.assertEqual(load_adapter("cursor")["default_model"], "grok-4.7-high")

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
