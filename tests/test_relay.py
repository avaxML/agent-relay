from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from execution import execute
from providers import RelayError, decode_response, load_adapter
from task_runner import build_request, run


class RelayTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.root = self.directory / "project"
        self.root.mkdir()
        self.task = self.directory / "task.txt"
        self.task.write_text("Find the answer.", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_adapter(
        self,
        name: str,
        executable: str,
        input_kind: str = "stdin",
        output_kind: str = "text",
        args: list[str] | None = None,
    ) -> Path:
        adapter = {
            "name": name,
            "executable": executable,
            "default_model": "test-model",
            "args": args or [],
            "input": input_kind,
            "output": output_kind,
            "env": {},
            "models_args": [],
            "probe_args": [],
        }
        path = self.directory / f"{name}.json"
        path.write_text(json.dumps(adapter), encoding="utf-8")
        return path


class BuildRequestTests(RelayTestCase):
    def test_build_request_deduplicates_files_and_preserves_hash_and_line_references(self) -> None:
        source = self.root / "src.txt"
        source.write_text("alpha\nbeta\n", encoding="utf-8")
        request, records = build_request(self.root, ["src.txt", "src.txt"], self.task, "read", 10000)
        payload = json.loads(request)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["lines"], 2)
        self.assertEqual(records[0]["sha256"], __import__("hashlib").sha256(source.read_bytes()).hexdigest())
        self.assertEqual(payload["sources"][0]["numbered_content"], "1: alpha\n2: beta")

    def test_build_request_rejects_traversal_and_symlink_escape(self) -> None:
        outside = self.directory / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (self.root / "link.txt").symlink_to(outside)
        for name in ("../outside.txt", "link.txt"):
            with self.subTest(name=name), self.assertRaises(RelayError):
                build_request(self.root, [name], self.task, "read", 10000)

    def test_build_request_rejects_credentials_and_metadata(self) -> None:
        for name in (".env", ".env.local", ".git/config", ".ssh/key"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("secret", encoding="utf-8")
            with self.subTest(name=name), self.assertRaises(RelayError):
                build_request(self.root, [name], self.task, "read", 10000)

    def test_build_request_rejects_empty_task(self) -> None:
        self.task.write_text("  \n", encoding="utf-8")
        with self.assertRaisesRegex(RelayError, "must not be empty"):
            build_request(self.root, [], self.task, "read", 10000)

    def test_build_request_rejects_binary_and_oversize_files(self) -> None:
        binary = self.root / "binary"
        binary.write_bytes(b"abc\0def")
        with self.assertRaises(RelayError):
            build_request(self.root, ["binary"], self.task, "read", 10000)
        large = self.root / "large"
        large.write_text("x" * 20, encoding="utf-8")
        with self.assertRaisesRegex(RelayError, "File bundle exceeds"):
            build_request(self.root, ["large"], self.task, "read", 30)

    def test_chaos_request_is_bounded_and_proposal_only(self) -> None:
        request, _records = build_request(self.root, [], self.task, "chaos", 10000)
        instructions = json.loads(request)["instructions"]
        for requirement in (
            "steady-state hypothesis",
            "malformed inputs",
            "partial dependency failures",
            "timeouts",
            "retries",
            "cancellation",
            "concurrency and races",
            "stale state",
            "resource exhaustion",
            "permission-boundary abuse",
            "blast radius",
            "observability gaps",
            "abort criteria",
            "Never claim to have run",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, instructions)


class ProviderTests(unittest.TestCase):
    def test_decodes_successful_antigravity_result_and_metadata(self) -> None:
        raw = json.dumps(
            {
                "event": "result",
                "result": {"status": "SUCCESS", "response": " answer ", "conversation_id": "c1", "usage": {"in": 1}},
            }
        )
        answer, metadata = decode_response(raw, "agy-jsonl")
        self.assertEqual(answer, "answer")
        self.assertEqual(metadata["conversation_id"], "c1")

    def test_decodes_opencode_text_events_and_session_ids(self) -> None:
        raw = "\n".join(
            [
                json.dumps({"type": "text", "part": {"text": "one"}, "sessionID": "b"}),
                json.dumps({"type": "text", "part": {"text": "two"}, "sessionID": "a"}),
            ]
        )
        answer, metadata = decode_response(raw, "opencode-jsonl")
        self.assertEqual(answer, "one\ntwo")
        self.assertEqual(metadata["session_ids"], ["a", "b"])

    def test_decodes_successful_cursor_result_and_metadata(self) -> None:
        raw = "\n".join(
            [
                json.dumps({"type": "system", "subtype": "init"}),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": " answer ",
                        "session_id": "session-1",
                        "request_id": "request-1",
                        "usage": {"input_tokens": 7},
                        "duration_ms": 42,
                    }
                ),
            ]
        )
        answer, metadata = decode_response(raw, "cursor-jsonl")
        self.assertEqual(answer, "answer")
        self.assertEqual(
            metadata,
            {
                "session_id": "session-1",
                "request_id": "request-1",
                "usage": {"input_tokens": 7},
                "duration_ms": 42,
            },
        )

    def test_cursor_decoder_rejects_missing_duplicate_and_failed_results(self) -> None:
        success = {"type": "result", "subtype": "success", "is_error": False, "result": "ok"}
        cases = [
            json.dumps({"type": "system", "subtype": "init"}),
            "\n".join((json.dumps(success), json.dumps(success))),
            json.dumps({**success, "is_error": True}),
            json.dumps({**success, "subtype": "error"}),
            json.dumps({**success, "result": 7}),
        ]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(RelayError):
                decode_response(raw, "cursor-jsonl")

    def test_decoders_reject_failure_empty_and_malformed_output(self) -> None:
        cases = [
            ('{"event":"result","result":{"status":"FAILURE","response":"x"}}', "agy-jsonl"),
            ("", "text"),
            ("not json", "opencode-jsonl"),
            ('{"type":"error","error":"bad"}', "opencode-jsonl"),
        ]
        for raw, kind in cases:
            with self.subTest(kind=kind), self.assertRaises(RelayError):
                decode_response(raw, kind)

    def test_load_adapter_rejects_malformed_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adapter.json"
            path.write_text(json.dumps({"name": "x"}), encoding="utf-8")
            with self.assertRaises(RelayError):
                load_adapter("x", path)


class ExecutionTests(unittest.TestCase):
    def make_cli(self, body: str) -> tuple[Path, Path]:
        directory = Path(tempfile.mkdtemp())
        script = directory / "fake-cli"
        script.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script, directory

    def test_execute_passes_stdin_and_captures_text(self) -> None:
        script, directory = self.make_cli("import sys; print(sys.stdin.read(), end='')")
        stdin = directory / "in"
        output = directory / "out"
        output.mkdir()
        stdin.write_text("hello", encoding="utf-8")
        self.assertEqual(execute([str(script)], directory, os.environ.copy(), stdin, output, 2), 0)
        self.assertEqual((output / "stdout.log").read_text(), "hello")

    def test_execute_returns_nonzero_and_captures_stderr(self) -> None:
        script, directory = self.make_cli("import sys; print('bad', file=sys.stderr); raise SystemExit(7)")
        stdin = directory / "in"
        stdin.touch()
        output = directory / "out"
        output.mkdir()
        self.assertEqual(execute([str(script)], directory, os.environ.copy(), stdin, output, 2), 7)
        self.assertIn("bad", (output / "stderr.log").read_text())

    def test_execute_stops_timed_out_process(self) -> None:
        script, directory = self.make_cli("import time; time.sleep(5)")
        stdin = directory / "in"
        stdin.touch()
        output = directory / "out"
        output.mkdir()
        with self.assertRaisesRegex(RelayError, "exceeded"):
            execute([str(script)], directory, os.environ.copy(), stdin, output, 0.1)


class RunIntegrationTests(RelayTestCase):
    def test_bundled_cursor_adapter_uses_stdin_and_isolated_workdir(self) -> None:
        script = self.directory / "cursor-agent"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "request = json.load(sys.stdin)\n"
            "result = {\n"
            "    'argv': sys.argv[1:],\n"
            "    'task': request['task'],\n"
            "    'cwd_files': sorted(path.name for path in pathlib.Path.cwd().iterdir()),\n"
            "    'workspace_source_visible': pathlib.Path('source.txt').exists(),\n"
            "}\n"
            "print(json.dumps({\n"
            "    'type': 'result', 'subtype': 'success', 'is_error': False,\n"
            "    'result': json.dumps(result), 'session_id': 's1', 'request_id': 'r1',\n"
            "    'usage': {'input_tokens': 1}, 'duration_ms': 5,\n"
            "}))\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        (self.root / "source.txt").write_text("source", encoding="utf-8")
        output = self.directory / "cursor-artifacts"
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{script.parent}:{old_path}"
        try:
            result = run(
                type(
                    "Args",
                    (),
                    {
                        "provider": "cursor",
                        "adapter_file": None,
                        "registry_dir": None,
                        "model": None,
                        "effort": None,
                        "root": self.root,
                        "files": ["source.txt"],
                        "task_file": self.task,
                        "kind": "read",
                        "max_input_bytes": 10000,
                        "output": output,
                        "timeout": 2,
                        "max_answer_chars": 2000,
                    },
                )()
            )
        finally:
            os.environ["PATH"] = old_path

        self.assertEqual(result["status"], "ok")
        answer = json.loads(result["answer"])
        self.assertEqual(
            answer["argv"],
            [
                "-p",
                "--trust",
                "--mode",
                "ask",
                "--sandbox",
                "enabled",
                "--model",
                "cursor-grok-4.6-high",
                "--output-format",
                "stream-json",
            ],
        )
        self.assertEqual(answer["task"], "Find the answer.")
        self.assertEqual(answer["cwd_files"], ["request.json", "stdin.jsonl"])
        self.assertFalse(answer["workspace_source_visible"])
        self.assertEqual(result["metadata"]["session_id"], "s1")

    def test_run_uses_stdin_and_truncates_answer_while_writing_full_artifact(self) -> None:
        script = self.directory / "fake-cli"
        script.write_text(
            "#!/usr/bin/env python3\nimport sys; data=sys.stdin.read(); print('answer:' + data[:5])\n", encoding="utf-8"
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        adapter = self.write_adapter("fake", script.name, output_kind="text")
        output = self.directory / "artifacts"
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{script.parent}:{old_path}"
        try:
            args = type(
                "Args",
                (),
                {
                    "provider": "fake",
                    "adapter_file": adapter,
                    "model": None,
                    "effort": None,
                    "root": self.root,
                    "files": [],
                    "task_file": self.task,
                    "kind": "read",
                    "max_input_bytes": 10000,
                    "output": output,
                    "timeout": 2,
                    "max_answer_chars": 8,
                },
            )()
            result = run(args)
        finally:
            os.environ["PATH"] = old_path
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["answer_truncated"])
        full_answer = (output / "answer.txt").read_text().rstrip("\n")
        self.assertTrue(full_answer.startswith("answer:"))
        self.assertGreater(len(full_answer), len(result["answer"]))

    def test_run_refuses_to_overwrite_existing_output(self) -> None:
        script = self.directory / "fake-cli"
        script.write_text("#!/usr/bin/env python3\nprint('ok')\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        adapter = self.write_adapter("fake", script.name, output_kind="text")
        output = self.directory / "artifacts"
        output.mkdir()
        args = type(
            "Args",
            (),
            {
                "provider": "fake",
                "adapter_file": adapter,
                "model": None,
                "effort": None,
                "root": self.root,
                "files": [],
                "task_file": self.task,
                "kind": "read",
                "max_input_bytes": 10000,
                "output": output,
                "timeout": 2,
                "max_answer_chars": 8,
            },
        )()
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{script.parent}:{old_path}"
        try:
            with self.assertRaises(FileExistsError):
                run(args)
        finally:
            os.environ["PATH"] = old_path


if __name__ == "__main__":
    unittest.main()
