from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import dispatch
import execution
import task_runner
from execution import BoundedLog, OutputBudget, execute, shrink_event
from providers import RelayError, RelayStopped, decode_partial, decode_response, validate_adapter
from task_runner import KINDS, prepare_task, provider_stdout, resolve_log_budget, run, run_check

TOOL_RESULT = "x" * 50_000


def cursor_stream(tool_events: int, answer: str = "final answer") -> list[bytes]:
    lines = [{"type": "system", "subtype": "init", "session_id": "s"}]
    for index in range(tool_events):
        lines.append({"type": "thinking", "subtype": "delta", "text": "hmm"})
        lines.append(
            {
                "type": "tool_call",
                "subtype": "completed",
                "call_id": str(index),
                "tool_call": {"shellToolCall": {"result": {"success": {"stdout": TOOL_RESULT}}}},
            }
        )
    lines.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]}})
    lines.append({"type": "result", "subtype": "success", "is_error": False, "result": answer, "session_id": "s"})
    return [json.dumps(line).encode() + b"\n" for line in lines]


def agy_stream(tool_events: int, answer: str = "final answer") -> list[bytes]:
    lines: list[dict[str, Any]] = [{"event": "tool", "output": TOOL_RESULT} for _ in range(tool_events)]
    lines.append({"event": "result", "result": {"status": "SUCCESS", "response": answer}})
    return [json.dumps(line).encode() + b"\n" for line in lines]


def opencode_stream(tool_events: int, answer: str = "final answer") -> list[bytes]:
    lines: list[dict[str, Any]] = [{"type": "text", "part": {"text": "first"}, "sessionID": "a"}]
    lines += [{"type": "tool_use", "part": {"state": {"output": TOOL_RESULT}}} for _ in range(tool_events)]
    lines.append({"type": "text", "part": {"text": answer}, "sessionID": "a"})
    return [json.dumps(line).encode() + b"\n" for line in lines]


def text_stream(lines: int, answer: str = "final answer") -> list[bytes]:
    return [f"progress line {index} {'.' * 200}\n".encode() for index in range(lines)] + [answer.encode() + b"\n"]


class BoundedLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "stdout.log"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, output: str, chunks: list[bytes], max_bytes: int, max_event_bytes: int) -> BoundedLog:
        log = BoundedLog(self.path, output, max_bytes, max_event_bytes)
        data = b"".join(chunks)
        # Feed in odd-sized pieces so lines arrive split across reads.
        for start in range(0, len(data), 7_919):
            log.feed(data[start : start + 7_919])
        log.close()
        return log

    def test_each_output_type_still_decodes_after_truncation(self) -> None:
        cases = {
            "cursor-jsonl": (cursor_stream(120), "final answer"),
            "agy-jsonl": (agy_stream(120), "final answer"),
            "opencode-jsonl": (opencode_stream(120), "first\nfinal answer"),
        }
        for output, (chunks, expected) in cases.items():
            with self.subTest(output=output):
                raw_size = sum(map(len, chunks))
                self.assertGreater(raw_size, 4_000_000)
                log = self.write(output, chunks, 1_000_000, 8_000)
                persisted = self.path.read_bytes()
                self.assertLessEqual(len(persisted), 1_000_000)
                self.assertGreater(log.stats["truncated_events"], 0)
                self.assertEqual(log.stats["received_bytes"], raw_size)
                answer, _metadata = decode_response(persisted.decode("utf-8"), output)
                self.assertEqual(answer, expected)

    def test_truncated_events_keep_their_type_and_carry_a_marker(self) -> None:
        self.write("cursor-jsonl", cursor_stream(3), 1_000_000, 8_000)
        lines = self.path.read_bytes().splitlines(keepends=True)
        tools = [(line, json.loads(line)) for line in lines if json.loads(line)["type"] == "tool_call"]
        self.assertEqual(len(tools), 3)
        for line, event in tools:
            self.assertEqual(event["subtype"], "completed")
            self.assertGreater(event["relay_truncated_bytes"], 40_000)
            self.assertLessEqual(len(line), 8_000)

    def test_small_events_pass_through_byte_for_byte(self) -> None:
        chunks = [b'{"type":"system",  "subtype":"init"}\n', b'{"type":"result","subtype":"success","result":"ok"}\n']
        log = self.write("cursor-jsonl", chunks, 1_000_000, 8_000)
        self.assertEqual(self.path.read_bytes(), b"".join(chunks))
        self.assertEqual(log.stats["truncated_events"], 0)

    def test_events_past_the_cap_are_dropped_and_counted_but_the_result_is_kept(self) -> None:
        chunks = cursor_stream(400)
        log = self.write("cursor-jsonl", chunks, 400_000, 4_000)
        persisted = self.path.read_bytes()
        self.assertLessEqual(len(persisted), 400_000)
        self.assertGreater(log.stats["dropped_events"], 0)
        self.assertGreater(log.stats["dropped_bytes"], 0)
        self.assertEqual(decode_response(persisted.decode("utf-8"), "cursor-jsonl")[0], "final answer")

    def test_cursor_assistant_messages_survive_the_cap_for_partial_answers(self) -> None:
        chunks = cursor_stream(400, "latest progress")[:-1]
        log = self.write("cursor-jsonl", chunks, 400_000, 4_000)
        self.assertGreater(log.stats["dropped_events"], 0)
        self.assertEqual(decode_partial(self.path.read_text(encoding="utf-8"), "cursor-jsonl"), "latest progress")

    def test_answer_events_are_never_truncated(self) -> None:
        answer = "A" * 20_000
        for output, chunks in (
            ("cursor-jsonl", cursor_stream(5, answer)),
            ("agy-jsonl", agy_stream(5, answer)),
            ("opencode-jsonl", opencode_stream(5, answer)),
        ):
            with self.subTest(output=output):
                self.write(output, chunks, 1_000_000, 8_000)
                decoded = decode_response(self.path.read_text(encoding="utf-8"), output)[0]
                self.assertTrue(decoded.endswith(answer))

    def test_opencode_error_events_survive_so_failure_is_still_reported(self) -> None:
        error = {"type": "error", "error": {"message": "e" * 50_000}}
        chunks = [json.dumps(error).encode() + b"\n", *opencode_stream(2)]
        self.write("opencode-jsonl", chunks, 1_000_000, 8_000)
        with self.assertRaisesRegex(RelayError, "OpenCode reported failure"):
            decode_response(self.path.read_text(encoding="utf-8"), "opencode-jsonl")

    def test_text_output_keeps_head_and_tail_so_the_final_answer_survives(self) -> None:
        chunks = text_stream(30_000)
        log = self.write("text", chunks, 200_000, 8_000)
        persisted = self.path.read_text(encoding="utf-8")
        self.assertLessEqual(len(persisted.encode()), 200_000)
        self.assertTrue(persisted.startswith("progress line 0 "))
        self.assertTrue(persisted.rstrip().endswith("final answer"))
        self.assertIn("agent-relay dropped", persisted)
        self.assertGreater(log.stats["dropped_bytes"], 0)
        self.assertTrue(decode_response(persisted, "text")[0].endswith("final answer"))

    def test_text_lines_are_cut_on_utf8_boundaries(self) -> None:
        log = self.write("text", ["é".encode() * 10_000 + b"\n", b"done\n"], 20_000, 1_024)
        self.assertEqual(log.stats["truncated_events"], 1)
        persisted = self.path.read_bytes()
        persisted.decode("utf-8")
        self.assertTrue(persisted.endswith(b"done\n"))

    def test_a_long_single_line_text_answer_is_not_cut_at_the_event_limit(self) -> None:
        answer = "word " * 20_000
        self.write("text", [answer.encode() + b"\n"], 1_000_000, 8_000)
        self.assertEqual(decode_response(self.path.read_text(encoding="utf-8"), "text")[0], answer.strip())

    def test_text_drop_marker_is_written_whenever_lines_are_dropped(self) -> None:
        self.write("text", text_stream(5_000), 20_000, 1_024)
        self.assertIn("[agent-relay dropped", self.path.read_text(encoding="utf-8"))

    def test_dropped_events_are_not_also_counted_as_truncated(self) -> None:
        log = self.write("cursor-jsonl", cursor_stream(400), 400_000, 4_000)
        events = [json.loads(line) for line in self.path.read_bytes().splitlines()]
        kept = sum(1 for event in events if "relay_truncated_bytes" in event)
        self.assertEqual(log.stats["truncated_events"], kept)

    def test_fallback_stub_respects_the_event_limit(self) -> None:
        event = {"type": "tool_call", "subtype": "completed", **{f"k{index}": index for index in range(5_000)}}
        line = json.dumps(event).encode()
        self.assertLessEqual(len(shrink_event(event, len(line), 2_000)), 2_000)

    def test_hostile_json_is_kept_as_unparseable_instead_of_crashing(self) -> None:
        nested = b"[" * 5_000 + b"]" * 5_000
        huge_int = b'{"type":"tool_call","n":' + b"9" * 6_000 + b"}"
        surrogate = json.dumps({"type": "tool_call", "out": "\ud800" + "x" * 20_000}).encode()
        log = self.write("cursor-jsonl", [nested + b"\n", huge_int + b"\n", surrogate + b"\n"], 1_000_000, 8_000)
        lines = self.path.read_bytes().splitlines()
        self.assertEqual(lines[:2], [nested, huge_int])
        self.assertLessEqual(len(lines[2]), 8_000)
        self.assertEqual(log.stats["truncated_events"], 1)

    def test_a_terminal_event_that_does_not_fit_is_clipped_not_dropped(self) -> None:
        answer = "A" * 300_000
        chunks = [*cursor_stream(60)[:-2], *cursor_stream(0, answer)[-1:]]
        log = self.write("cursor-jsonl", chunks, 400_000, 4_000)
        decoded = decode_response(self.path.read_text(encoding="utf-8"), "cursor-jsonl")[0]
        self.assertTrue(answer.startswith(decoded.rstrip("…")))
        self.assertGreater(len(decoded), 10_000)
        self.assertLessEqual(self.path.stat().st_size, 400_000)
        self.assertEqual(log.stats["truncated_answer_events"], 1)

    def test_answer_events_are_never_stubbed(self) -> None:
        event = {"type": "text", "part": {"text": "answer"}, **{f"m{index}": "v" * 20 for index in range(1_200)}}
        line = json.dumps(event).encode()
        self.assertIsNone(shrink_event(event, len(line), 2_000, allow_stub=False))
        log = self.write("opencode-jsonl", [*opencode_stream(30)[:-1], line + b"\n"], 100_000, 4_000)
        # Either way the run fails closed: the event is clipped (keeping part) or dropped, never stubbed silently.
        self.assertEqual(log.stats["dropped_answer_events"] + log.stats["truncated_answer_events"], 1)

    def test_an_unparseable_oversized_line_counts_as_a_lost_answer(self) -> None:
        log = self.write("cursor-jsonl", [b'{"type":"result","result":"' + b"r" * 300_000 + b'"}\n'], 200_000, 4_000)
        self.assertEqual(log.stats["dropped_answer_events"], 1)

    def test_deeply_nested_events_are_shrunk_without_recursing_out(self) -> None:
        nested: Any = "x" * 20_000
        for _ in range(900):
            nested = [nested]
        line = json.dumps({"type": "tool_call", "out": nested}).encode()
        log = self.write("cursor-jsonl", [line + b"\n"], 1_000_000, 8_000)
        self.assertEqual(log.stats["truncated_events"], 1)

    def test_dropped_bytes_count_the_original_event(self) -> None:
        log = self.write("cursor-jsonl", cursor_stream(60), 100_000, 2_000)
        # Counting the shrunk form would cap the average at the 2,000-byte event limit.
        self.assertGreater(log.stats["dropped_bytes"], log.stats["dropped_events"] * 2_000)

    def test_answer_events_that_cannot_fit_are_counted(self) -> None:
        chunks = [json.dumps({"type": "text", "part": {"text": "t" * 50_000}}).encode() + b"\n" for _ in range(20)]
        log = self.write("opencode-jsonl", chunks, 400_000, 4_000)
        self.assertGreater(log.stats["dropped_answer_events"], 0)
        self.assertLessEqual(self.path.stat().st_size, 400_000)

    def test_text_answer_is_what_followed_the_drop(self) -> None:
        log = self.write("text", text_stream(30_000, "the real answer"), 200_000, 8_000)
        answer = decode_response(provider_stdout(self.path.parent, {"stdout": log.stats}), "text")[0]
        self.assertEqual(answer.splitlines()[-1], "the real answer")
        self.assertNotIn("progress line 0 ", answer)
        self.assertNotIn("agent-relay dropped", answer)

    def test_provider_text_cannot_impersonate_the_drop_marker(self) -> None:
        chunks = [b"first half of the answer\n", b"[agent-relay dropped 1 lines, 12 bytes]\n", b"second half\n"]
        log = self.write("text", chunks, 200_000, 8_000)
        answer = decode_response(provider_stdout(self.path.parent, {"stdout": log.stats}), "text")[0]
        self.assertTrue(answer.startswith("first half of the answer"))

    def test_unhashable_discriminators_do_not_crash_classification(self) -> None:
        for output in ("cursor-jsonl", "opencode-jsonl", "agy-jsonl"):
            with self.subTest(output=output):
                event = {"type": [], "event": {}, "pad": "x" * 20_000}
                log = self.write(output, [json.dumps(event).encode() + b"\n"], 1_000_000, 8_000)
                self.assertEqual(log.stats["truncated_events"], 1)

    def test_discriminators_survive_clipping_of_many_short_fields(self) -> None:
        event = {"type": "tool_call", "subtype": "completed", **{f"f{index}": "y" * 20 for index in range(400)}}
        line = json.dumps(event).encode()
        shrunk = json.loads(shrink_event(event, len(line), 4_000))
        self.assertEqual((shrunk["type"], shrunk["subtype"]), ("tool_call", "completed"))

    def test_a_dropped_malformed_line_is_counted(self) -> None:
        chunks = [*cursor_stream(40)[:-1], b"{not json " + b"z" * 5_000 + b"\n", cursor_stream(0)[-1]]
        log = self.write("cursor-jsonl", chunks, 200_000, 4_000)
        self.assertEqual(log.stats["dropped_malformed_events"], 1)

    def test_millions_of_tiny_text_lines_keep_a_bounded_tail(self) -> None:
        log = self.write("text", [b"\n" * 3_000_000, b"done\n"], 100_000, 8_000)
        persisted = self.path.read_text(encoding="utf-8")
        self.assertLessEqual(len(persisted.encode()), 100_000)
        self.assertTrue(persisted.endswith("done\n"))
        self.assertEqual(log.stats["dropped_events"] + persisted.count("\n") - 1, 3_000_001)

    def test_blank_lines_past_the_cap_are_not_malformed(self) -> None:
        log = self.write("cursor-jsonl", [*cursor_stream(40)[:-1], b"\n" * 50, cursor_stream(0)[-1]], 200_000, 4_000)
        self.assertEqual(log.stats["dropped_malformed_events"], 0)

    def test_hostile_json_in_a_kept_line_is_a_decode_error_not_a_crash(self) -> None:
        # Deep enough to exhaust recursion on any supported Python; huge integers hit the digit limit.
        for line in ("[" * 200_000 + "]" * 200_000, '{"n":' + "9" * 6_000 + "}"):
            with self.subTest(line=line[:10]), self.assertRaisesRegex(RelayError, "malformed JSON"):
                decode_response(line + "\n", "cursor-jsonl")

    def test_a_cut_text_line_counts_as_a_truncated_answer(self) -> None:
        log = self.write("text", [b"A" * 30_000 + b"FINAL_ANSWER\n"], 20_000, 1_024)
        self.assertEqual(log.stats["truncated_answer_events"], 1)

    def test_stub_fallback_ignores_nested_discriminators(self) -> None:
        nested: Any = "t"
        for _ in range(2_000):
            nested = [nested]
        event = {"type": nested, **{f"k{index}": index for index in range(3_000)}}
        shrunk = shrink_event(event, 100_000, 2_000)
        assert shrunk is not None
        self.assertLessEqual(len(shrunk), 2_000)

    def test_write_failures_are_counted_not_raised(self) -> None:
        log = BoundedLog(self.path, "cursor-jsonl", 1_000_000, 8_000)
        with mock.patch.object(log.stream, "write", side_effect=OSError(28, "No space left on device")):
            log.feed(b'{"type":"result","subtype":"success","result":"ok"}\n')
        log.close()
        self.assertEqual(log.stats["write_errors"], 1)

    def test_opencode_null_error_events_are_ordinary(self) -> None:
        event = {"type": "tool_use", "error": None, "part": {"state": {"output": TOOL_RESULT}}}
        log = self.write("opencode-jsonl", [json.dumps(event).encode() + b"\n", *opencode_stream(1)], 1_000_000, 8_000)
        self.assertEqual(log.stats["truncated_events"], 2)  # this event and the stream's own tool result
        self.assertEqual(log.stats["truncated_answer_events"], 0)

    def test_a_whitespace_only_overflow_line_is_not_a_lost_answer(self) -> None:
        log = self.write("cursor-jsonl", [b" " * 300_000 + b"\n", cursor_stream(0)[-1]], 200_000, 4_000)
        self.assertEqual(log.stats["dropped_answer_events"], 0)

    def test_stub_respects_the_limit_even_when_escaping_inflates_it(self) -> None:
        event = {key: "\0" * 64 for key in ("type", "subtype", "event")} | {f"k{i}": i for i in range(3_000)}
        shrunk = shrink_event(event, 100_000, 1_024)
        assert shrunk is not None
        self.assertLessEqual(len(shrunk), 1_024)

    def test_a_bom_prefixed_event_stays_malformed(self) -> None:
        event = b"\xef\xbb\xbf" + json.dumps({"type": "tool_call", "out": "x" * 20_000}).encode()
        self.write("cursor-jsonl", [event + b"\n", cursor_stream(0)[-1]], 1_000_000, 4_000)
        with self.assertRaisesRegex(RelayError, "malformed JSON"):
            decode_response(self.path.read_text(encoding="utf-8"), "cursor-jsonl")

    def test_a_whitespace_only_text_overflow_is_not_a_lost_answer(self) -> None:
        log = self.write("text", [b" " * 30_000 + b"\n", b"answer\n"], 20_000, 1_024)
        self.assertEqual(log.stats["truncated_answer_events"], 0)
        self.assertEqual(log.stats["truncated_events"], 1)

    def test_unparseable_lines_pass_through(self) -> None:
        self.write("cursor-jsonl", [b"not json\n"], 1_000_000, 8_000)
        self.assertEqual(self.path.read_bytes(), b"not json\n")


class DecodePartialTests(unittest.TestCase):
    def test_partial_answers_come_from_streams_without_a_terminal_result(self) -> None:
        cursor = b"".join(cursor_stream(1, "half done")[:-1]).decode()
        self.assertEqual(decode_partial(cursor, "cursor-jsonl"), "half done")
        opencode = b"".join(opencode_stream(1, "two")).decode() + "{truncated"
        self.assertEqual(decode_partial(opencode, "opencode-jsonl"), "first\ntwo")
        self.assertEqual(decode_partial("working...\n", "text"), "working...")
        self.assertIsNone(decode_partial(json.dumps({"event": "tool"}), "agy-jsonl"))
        self.assertIsNone(decode_partial("", "cursor-jsonl"))
        failed = json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": "gave up: quota"})
        self.assertEqual(decode_partial(failed + "\n", "cursor-jsonl"), "gave up: quota")
        huge = '{"type":"tool_call","n":' + "9" * 6_000 + "}"
        self.assertEqual(decode_partial(cursor + huge + "\n", "cursor-jsonl"), "half done")


FLOOD = """
import json, sys
event = json.dumps({"type": "tool_call", "subtype": "completed", "result": "x" * 50_000})
for _ in range(int(sys.argv[1])):
    sys.stdout.write(event + "\\n")
sys.stderr.write("e" * 3_000_000)
sys.stdout.write(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done"}) + "\\n")
"""

RUNAWAY = """
import sys
while True:
    sys.stdout.write("y" * 65536)
"""


class ExecuteBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.stdin = self.directory / "stdin"
        self.stdin.touch()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def script(self, name: str, body: str) -> Path:
        path = self.directory / name
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    def test_verbose_worker_completes_with_bounded_logs(self) -> None:
        flood = self.script("flood.py", FLOOD)
        budget = OutputBudget("cursor-jsonl", 4_000_000, 32_000, 64_000_000)
        code = execute(
            [sys.executable, str(flood), "200"],
            self.directory,
            dict(os.environ),
            self.stdin,
            self.directory,
            30,
            budget=budget,
        )
        self.assertEqual(code, 0)
        stdout = self.directory / "stdout.log"
        stderr = self.directory / "stderr.log"
        self.assertLessEqual(stdout.stat().st_size + stderr.stat().st_size, 4_000_000)
        self.assertEqual(decode_response(stdout.read_text(encoding="utf-8"), "cursor-jsonl")[0], "done")
        stats = budget.stats()
        self.assertGreater(stats["stdout"]["truncated_events"], 0)
        self.assertEqual(stats["stdout"]["truncated_events"] + stats["stdout"]["dropped_events"], 200)
        self.assertGreater(stats["stderr"]["truncated_bytes"], 2_000_000)

    def test_runaway_output_past_the_hard_ceiling_is_stopped(self) -> None:
        runaway = self.script("runaway.py", RUNAWAY)
        budget = OutputBudget("text", 100_000, 8_000, 2_000_000)
        with self.assertRaises(RelayStopped) as caught:
            execute(
                [sys.executable, str(runaway)],
                self.directory,
                dict(os.environ),
                self.stdin,
                self.directory,
                30,
                budget=budget,
            )
        self.assertEqual(caught.exception.reason, "runaway_output")
        self.assertLessEqual((self.directory / "stdout.log").stat().st_size, 100_000)

    def test_timeout_and_cancellation_are_stops_with_reasons(self) -> None:
        sleeper = self.script("sleep.py", "import time\nprint('started', flush=True)\ntime.sleep(30)\n")
        budget = OutputBudget("text", 100_000, 8_000, 2_000_000)
        with self.assertRaises(RelayStopped) as caught:
            execute(
                [sys.executable, str(sleeper)],
                self.directory,
                dict(os.environ),
                self.stdin,
                self.directory,
                1,
                budget=budget,
            )
        self.assertEqual(caught.exception.reason, "timeout")
        self.assertIn("started", (self.directory / "stdout.log").read_text(encoding="utf-8"))
        cancel = self.directory / "cancel"
        canceller = self.script(
            "cancel.py", f"import pathlib, time\npathlib.Path({str(cancel)!r}).touch()\ntime.sleep(30)\n"
        )
        with self.assertRaises(RelayStopped) as caught:
            execute(
                [sys.executable, str(canceller)],
                self.directory,
                dict(os.environ),
                self.stdin,
                self.directory,
                30,
                cancel_file=cancel,
                budget=budget,
            )
        self.assertEqual(caught.exception.reason, "cancelled")

    def test_stopping_a_group_of_zombies_is_not_an_error(self) -> None:
        # macOS answers killpg with EPERM when only zombies remain; that must not turn a stop into an error.
        sleeper = self.script("quick.py", "print('done')\n")
        budget = OutputBudget("text", 100_000, 8_000, 2_000_000)
        with mock.patch("execution.os.killpg", side_effect=PermissionError(1, "Operation not permitted")):
            code = execute(
                [sys.executable, str(sleeper)],
                self.directory,
                dict(os.environ),
                self.stdin,
                self.directory,
                30,
                budget=budget,
            )
        self.assertEqual(code, 0)

    def test_eperm_on_a_live_group_still_stops_the_child(self) -> None:
        sleeper = self.script("sleep.py", "import time\ntime.sleep(30)\n")
        budget = OutputBudget("text", 100_000, 8_000, 2_000_000)
        started = time.monotonic()
        with (
            mock.patch("execution.os.killpg", side_effect=PermissionError(1, "Operation not permitted")),
            self.assertRaises(RelayStopped),
        ):
            execute(
                [sys.executable, str(sleeper)],
                self.directory,
                dict(os.environ),
                self.stdin,
                self.directory,
                1,
                budget=budget,
            )
        self.assertLess(time.monotonic() - started, 10)

    def test_output_flushed_on_sigterm_is_kept(self) -> None:
        flusher = self.script(
            "flush.py",
            "import signal, sys, time\n"
            "def bye(*_):\n"
            "    sys.stdout.write('x' * 200_000 + '\\nFINAL\\n'); sys.stdout.flush(); sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, bye)\n"
            "print('started', flush=True)\n"
            "time.sleep(30)\n",
        )
        budget = OutputBudget("text", 4_000_000, 32_000, 64_000_000)
        with self.assertRaises(RelayStopped):
            execute(
                [sys.executable, str(flusher)],
                self.directory,
                dict(os.environ),
                self.stdin,
                self.directory,
                1,
                budget=budget,
            )
        self.assertTrue((self.directory / "stdout.log").read_text(encoding="utf-8").endswith("FINAL\n"))

    def test_logs_are_flushed_even_if_shutdown_is_interrupted(self) -> None:
        sleeper = self.script("talk.py", "import time\nprint('assistant partial', flush=True)\ntime.sleep(30)\n")
        budget = OutputBudget("text", 100_000, 8_000, 2_000_000)
        real = execution._signal_group
        calls: list[int] = []

        def interrupted(process: Any, number: int) -> None:
            calls.append(number)
            if len(calls) == 1:
                raise KeyboardInterrupt
            real(process, number)

        with mock.patch.object(execution, "_signal_group", interrupted), self.assertRaises(KeyboardInterrupt):
            execute(
                [sys.executable, str(sleeper)],
                self.directory,
                dict(os.environ),
                self.stdin,
                self.directory,
                1,
                budget=budget,
            )
        self.assertIn("assistant partial", (self.directory / "stdout.log").read_text(encoding="utf-8"))
        self.assertTrue(budget.logs["stdout"].stream.closed)

    def test_without_a_budget_the_strict_cap_still_kills(self) -> None:
        # Git and Relay checks rely on this: their stdout is never truncated, only refused.
        runaway = self.script("runaway.py", RUNAWAY)
        with self.assertRaisesRegex(RelayError, "log size limit") as caught:
            execute(
                [sys.executable, str(runaway)],
                self.directory,
                dict(os.environ),
                self.stdin,
                self.directory,
                30,
                max_log_bytes=200_000,
            )
        self.assertNotIsInstance(caught.exception, RelayStopped)

    def test_check_commands_keep_their_own_strict_limit(self) -> None:
        record = run_check(
            f"{sys.executable} -c 'import sys; sys.stdout.write(\"z\" * 1_500_000)'",
            self.directory,
            dict(os.environ),
            self.directory / "check",
            30,
        )
        self.assertIsNone(record["exit_code"])
        self.assertIn("log size limit", record["error"])


class BudgetResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.root = self.directory / "project"
        self.root.mkdir()
        self.task = self.directory / "task.txt"
        self.task.write_text("Find the answer.", encoding="utf-8")
        self.script = self.directory / "fake.py"
        self.script.write_text("print('answer')\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def adapter(self, **extra: Any) -> dict[str, Any]:
        return {
            "name": "fake",
            "executable": sys.executable,
            "default_model": "m",
            "args": [str(self.script)],
            "input": "stdin",
            "output": "text",
            "env": {},
            "models_args": [],
            "probe_args": [],
            **extra,
        }

    def test_tool_kinds_get_larger_defaults_than_read_kinds(self) -> None:
        adapter = self.adapter()
        read = resolve_log_budget(adapter, "read", None, None)
        review = resolve_log_budget(adapter, "review", None, None)
        for kind in ("execute", "probe"):
            tool = resolve_log_budget(adapter, kind, None, None)
            self.assertGreater(tool["max_log_bytes"], read["max_log_bytes"])
            self.assertGreater(tool["max_event_bytes"], read["max_event_bytes"])
            self.assertGreater(tool["max_log_bytes"], review["max_log_bytes"])
            self.assertGreater(tool["hard_ceiling_bytes"], tool["max_log_bytes"])
        self.assertEqual(read["sources"], {"max_log_bytes": "kind", "max_event_bytes": "kind"})
        self.assertEqual(
            {kind.max_log_bytes for name, kind in KINDS.items() if not kind.tools}, {read["max_log_bytes"]}
        )

    def test_cli_beats_adapter_beats_kind(self) -> None:
        adapter = self.adapter(limits={"max_log_bytes": 8_000_000, "max_event_bytes": 100_000})
        from_adapter = resolve_log_budget(adapter, "read", None, None)
        self.assertEqual((from_adapter["max_log_bytes"], from_adapter["max_event_bytes"]), (8_000_000, 100_000))
        self.assertEqual(from_adapter["sources"], {"max_log_bytes": "adapter", "max_event_bytes": "adapter"})
        from_cli = resolve_log_budget(adapter, "read", 2_000_000, None)
        self.assertEqual((from_cli["max_log_bytes"], from_cli["max_event_bytes"]), (2_000_000, 100_000))
        self.assertEqual(from_cli["sources"], {"max_log_bytes": "cli", "max_event_bytes": "adapter"})

    def test_inconsistent_budgets_are_rejected_naming_their_sources(self) -> None:
        with self.assertRaisesRegex(RelayError, "from --max-event-bytes and 100000 from --max-log-bytes"):
            resolve_log_budget(self.adapter(), "read", 100_000, 50_000)
        adapter = self.adapter(limits={"max_log_bytes": 100_000})
        with self.assertRaisesRegex(RelayError, "from the read default and 100000 from adapter limits.max_log_bytes"):
            resolve_log_budget(adapter, "read", None, None)
        with self.assertRaisesRegex(RelayError, "at most a quarter"):
            validate_adapter(self.adapter(limits={"max_log_bytes": 100_000, "max_event_bytes": 50_000}), "fake")

    def test_adapter_limits_are_validated(self) -> None:
        validate_adapter(self.adapter(limits={"max_log_bytes": 8_000_000}), "fake")
        for limits in (
            {"max_event_bytes": 1000},
            {"max_log_bytes": 0},
            {"max_log_bytes": True},
            {"other": 1},
            {"max_event_bytes": "1"},
            [],
        ):
            with self.subTest(limits=limits), self.assertRaises(RelayError):
                validate_adapter(self.adapter(limits=limits), "fake")

    def args(self, adapter_path: Path, **options: Any) -> argparse.Namespace:
        values: dict[str, Any] = {
            "provider": "fake",
            "adapter_file": adapter_path,
            "model": None,
            "effort": None,
            "root": self.root,
            "files": [],
            "task_file": self.task,
            "kind": "read",
            "max_input_bytes": 400000,
            "output": self.directory / "out",
            "timeout": 30,
            "max_answer_chars": 12000,
        }
        values.update(options)
        return argparse.Namespace(**values)

    def test_cli_flags_and_adapter_override_reach_execute_and_result(self) -> None:
        path = self.directory / "fake.json"
        path.write_text(json.dumps(self.adapter(limits={"max_event_bytes": 20_000})), encoding="utf-8")
        seen: list[OutputBudget] = []
        real = task_runner.execute

        def spy(*args: Any, **kwargs: Any) -> int:
            seen.append(kwargs["budget"])
            return real(*args, **kwargs)

        with mock.patch.object(task_runner, "execute", spy):
            result = run(self.args(path, max_log_bytes=3_000_000))
        self.assertEqual(result["status"], "ok", result.get("error"))
        [budget] = seen
        self.assertEqual((budget.max_log_bytes, budget.max_event_bytes), (3_000_000, 20_000))
        self.assertEqual(budget.output_format, "text")
        recorded = json.loads((self.directory / "out" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(recorded["log_budget"]["max_log_bytes"], 3_000_000)
        self.assertEqual(recorded["log_budget"]["max_event_bytes"], 20_000)
        self.assertEqual(recorded["log_budget"]["sources"], {"max_log_bytes": "cli", "max_event_bytes": "adapter"})
        self.assertIn("received_bytes", recorded["log_stats"]["stdout"])

    def test_relay_cli_flags_are_recorded_in_the_result(self) -> None:
        path = self.directory / "fake.json"
        path.write_text(json.dumps(self.adapter()), encoding="utf-8")
        output = self.directory / "cli-out"
        (self.root / "notes.txt").write_text("notes\n", encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "relay.py"),
                "run",
                "--provider",
                "fake",
                "--adapter-file",
                str(path),
                "--task-file",
                str(self.task),
                "--root",
                str(self.root),
                "--files",
                "notes.txt",
                "--output",
                str(output),
                "--max-log-bytes",
                "5000000",
                "--max-event-bytes",
                "40000",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        budget = json.loads(completed.stdout)["log_budget"]
        self.assertEqual((budget["max_log_bytes"], budget["max_event_bytes"]), (5_000_000, 40_000))

    def test_dispatch_settings_reach_task_arguments(self) -> None:
        task = {
            "provider": "fake",
            "model": None,
            "effort": None,
            "task_file": str(self.task),
            "files": [],
            "output_dir": str(self.directory / "d"),
            "kind": "read",
            "owns": [],
            "checks": [],
            "setup": [],
        }
        ledger = {"root": str(self.root), "settings": {"max_log_bytes": 6_000_000, "max_event_bytes": 50_000}}
        args = dispatch.task_arguments(task, ledger)
        self.assertEqual((args.max_log_bytes, args.max_event_bytes), (6_000_000, 50_000))
        defaults = dispatch.task_arguments(task, {"root": str(self.root), "settings": {}})
        self.assertEqual((defaults.max_log_bytes, defaults.max_event_bytes), (None, None))

    def test_each_flag_and_adapter_override_reaches_execute_independently(self) -> None:
        path = self.directory / "fake.json"
        seen: list[OutputBudget] = []
        real = task_runner.execute

        def spy(*args: Any, **kwargs: Any) -> int:
            seen.append(kwargs["budget"])
            return real(*args, **kwargs)

        cases = [
            ({"max_log_bytes": 9_000_000}, {}, (9_000_000, KINDS["read"].max_event_bytes)),
            ({}, {"max_event_bytes": 12_000}, (KINDS["read"].max_log_bytes, 12_000)),
            ({"max_log_bytes": 9_000_000}, {"max_log_bytes": 5_000_000}, (5_000_000, KINDS["read"].max_event_bytes)),
            ({"max_event_bytes": 20_000}, {"max_event_bytes": 30_000}, (KINDS["read"].max_log_bytes, 30_000)),
        ]
        for index, (limits, flags, expected) in enumerate(cases):
            with self.subTest(limits=limits, flags=flags):
                path.write_text(json.dumps(self.adapter(limits=limits)), encoding="utf-8")
                seen.clear()
                with mock.patch.object(task_runner, "execute", spy):
                    result = run(self.args(path, output=self.directory / f"out-{index}", **flags))
                self.assertEqual(result["status"], "ok", result.get("error"))
                self.assertEqual((seen[0].max_log_bytes, seen[0].max_event_bytes), expected)

    def test_a_dropped_answer_event_fails_the_run_instead_of_passing_a_prefix(self) -> None:
        self.script.write_text(
            "import json\nfor _ in range(30):\n"
            "    print(json.dumps({'type': 'text', 'part': {'text': 't' * 50_000}}))\n",
            encoding="utf-8",
        )
        path = self.directory / "fake.json"
        path.write_text(json.dumps(self.adapter(output="opencode-jsonl")), encoding="utf-8")
        result = run(self.args(path, max_log_bytes=400_000, max_event_bytes=4_000))
        self.assertEqual(result["status"], "error")
        self.assertIn("did not fit", result["error"])
        self.assertGreater(result["log_stats"]["stdout"]["dropped_answer_events"], 0)

    def test_a_text_answer_cut_mid_line_fails_the_run(self) -> None:
        self.script.write_text("print('A' * 300_000 + 'FINAL_ANSWER')\n", encoding="utf-8")
        path = self.directory / "fake.json"
        path.write_text(json.dumps(self.adapter()), encoding="utf-8")
        result = run(self.args(path, max_log_bytes=200_000, max_event_bytes=8_000))
        self.assertEqual(result["status"], "error")
        self.assertIn("did not fit", result["error"])
        self.assertTrue(result["partial_answer"].startswith("AAAA"))

    def test_a_text_answer_that_lost_lines_fails_but_keeps_the_tail(self) -> None:
        self.script.write_text("for i in range(5_000):\n    print(f'answer line {i} ' + '.' * 100)\n", encoding="utf-8")
        path = self.directory / "fake.json"
        path.write_text(json.dumps(self.adapter()), encoding="utf-8")
        result = run(self.args(path, max_log_bytes=200_000, max_event_bytes=8_000))
        self.assertEqual(result["status"], "error")
        self.assertIn("did not fit", result["error"])
        self.assertIn("answer line 4999", Path(result["partial_answer_path"]).read_text(encoding="utf-8"))
        self.assertNotIn("agent-relay dropped", result["partial_answer"])

    def test_result_records_survive_lone_surrogates(self) -> None:
        target = self.directory / "record.json"
        task_runner.write_json(target, {"metadata": {"session_ids": ["\ud800"]}})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["metadata"]["session_ids"], ["\ud800"])

    def test_prepared_task_carries_the_budget_for_detached_jobs(self) -> None:
        path = self.directory / "fake.json"
        path.write_text(json.dumps(self.adapter()), encoding="utf-8")
        task = prepare_task(self.args(path, max_event_bytes=10_000))
        self.assertEqual(task["log_budget"]["max_event_bytes"], 10_000)
        self.assertEqual(json.loads(json.dumps(task))["log_budget"], task["log_budget"])


if __name__ == "__main__":
    unittest.main()
