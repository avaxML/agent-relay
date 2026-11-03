from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import isolation
import task_runner
from providers import RelayError
from task_runner import prepare_task, run, run_prepared

FAKE_CLI = """
import json
import os
import pathlib
import subprocess
import sys
import time

request_file = pathlib.Path(sys.argv[1])
clone = pathlib.Path(sys.argv[2])
mode = os.environ.get("FAKE_MODE", "probe")
if os.environ.get("GIT_DIR"):
    print("GIT_DIR leaked into the worker environment", file=sys.stderr)
    raise SystemExit(3)
if os.path.realpath(os.environ.get("PWD", ".")) != os.path.realpath(os.getcwd()):
    print("PWD does not name the clone; a PWD-resolving CLI would work in the caller's directory", file=sys.stderr)
    raise SystemExit(6)
if os.environ.get("FAKE_TOOLS") != "1":
    print("tools environment missing", file=sys.stderr)
    raise SystemExit(4)
json.loads(request_file.read_text(encoding="utf-8"))
if mode == "probe":
    subprocess.run([sys.executable, "-c", "print(7319)"], cwd=clone, check=True)
    pathlib.Path("probe.txt").write_text("7319\\n", encoding="utf-8")
    print("ran python3 -c print(7319)")
elif mode == "probe-setup":
    if not pathlib.Path("setup.txt").is_file():
        print("setup.txt is missing", file=sys.stderr)
        raise SystemExit(5)
    pathlib.Path("probe.txt").write_text("7319\\n", encoding="utf-8")
    print("ran setup-aware probe")
elif mode == "edit":
    pathlib.Path("app.py").write_text("def answer():\\n    return 2\\n", encoding="utf-8")
    print("edited app.py")
elif mode == "other":
    pathlib.Path("other.py").write_text("value = 1\\n", encoding="utf-8")
    print("wrote other.py")
elif mode == "delete":
    pathlib.Path("test_app.py").unlink()
    print("deleted test_app.py")
elif mode == "hook":
    pathlib.Path(".git/hooks/post-commit").write_text("#!/bin/sh\\n", encoding="utf-8")
    print("wrote a git hook")
elif mode == "claim":
    print("all checks passed")
elif mode == "empty":
    pass
elif mode == "sleep":
    time.sleep(30)
elif mode == "cancel":
    pathlib.Path(os.environ["FAKE_CANCEL"]).write_text("cancel", encoding="utf-8")
    time.sleep(30)
elif mode == "edit-sleep":
    pathlib.Path("app.py").write_text("def answer():\\n    return 2\\n", encoding="utf-8")
    print("edited app.py, still working", flush=True)
    time.sleep(30)
elif mode == "edit-cancel":
    pathlib.Path("app.py").write_text("def answer():\\n    return 2\\n", encoding="utf-8")
    print("edited app.py", flush=True)
    pathlib.Path(os.environ["FAKE_CANCEL"]).write_text("cancel", encoding="utf-8")
    time.sleep(30)
elif mode == "edit-fail":
    pathlib.Path("app.py").write_text("def answer():\\n    return 2\\n", encoding="utf-8")
    raise SystemExit(3)
elif mode == "probe-sleep":
    pathlib.Path("probe.txt").write_text("7319\\n", encoding="utf-8")
    print("wrote probe.txt", flush=True)
    time.sleep(30)
elif mode == "edit-flood":
    pathlib.Path("app.py").write_text("def answer():\\n    return 2\\n", encoding="utf-8")
    while True:
        sys.stdout.write("noise " * 10000 + "\\n")
else:
    raise SystemExit(f"unknown FAKE_MODE {mode}")
"""


class ToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agent-relay-tools-test-")
        self.directory = Path(self.temp.name)
        self.clones = self.directory / "clones"
        self.clones.mkdir()
        self.repository = self.directory / "repository"
        self.repository.mkdir()
        self._git("init", "-q")
        self.app = self.repository / "app.py"
        self.app.write_text("def answer():\n    return 1\n", encoding="utf-8")
        (self.repository / "test_app.py").write_text(
            "from app import answer\n\n\ndef test_answer():\n    assert answer() == 1\n",
            encoding="utf-8",
        )
        self._git("-c", "user.name=Test", "-c", "user.email=test@example.com", "add", ".")
        self._git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "initial")
        self.mtime = self.app.stat().st_mtime_ns
        self.task = self.directory / "task.txt"
        self.task.write_text("Investigate the answer.\n", encoding="utf-8")
        self.script = self.directory / "fake_cli.py"
        self.script.write_text(textwrap.dedent(FAKE_CLI), encoding="utf-8")
        self.adapter = self.directory / "adapter.json"
        self.adapter.write_text(
            json.dumps(
                {
                    "name": "fake",
                    "executable": sys.executable,
                    "default_model": "m",
                    "args": [str(self.script), "{request_file}", "."],
                    "input": "file",
                    "output": "text",
                    "env": {},
                    "models_args": [],
                    "probe_args": [],
                    "tools": {
                        "args": [str(self.script), "{request_file}", "{clone}"],
                        "env": {"FAKE_TOOLS": "1"},
                    },
                }
            ),
            encoding="utf-8",
        )
        self.runs = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _git(self, *arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
        )

    def _status(self) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repository), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout

    def make_args(self, kind: str, **options: Any) -> argparse.Namespace:
        self.runs += 1
        values: dict[str, Any] = {
            "provider": "fake",
            "adapter_file": self.adapter,
            "model": None,
            "effort": None,
            "root": self.repository,
            "files": ["app.py"],
            "task_file": self.task,
            "kind": kind,
            "max_input_bytes": 400000,
            "output": self.directory / f"output-{self.runs:02d}",
            "timeout": 30,
            "max_answer_chars": 12000,
            "ref": "HEAD",
            "owns": [],
            "checks": [],
            "setup": [],
            "check_timeout": 180,
        }
        values.update(options)
        return argparse.Namespace(**values)

    def run_kind(self, kind: str, mode: str, **options: Any) -> dict[str, Any]:
        args = self.make_args(kind, **options)
        with mock.patch.dict(os.environ, {"FAKE_MODE": mode, "AGENT_RELAY_CLONES_DIR": str(self.clones)}):
            return run(args)

    def test_probe_ok_reports_untracked_files_and_passes_relay_checks(self) -> None:
        check = 'python3 -c \'import pathlib; assert pathlib.Path("probe.txt").read_text().strip() == "7319"\''
        result = self.run_kind("probe", "probe", checks=[check])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["untracked_files"], ["probe.txt"])
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["checks"][0]["exit_code"], 0)
        self.assertIn("7319", result["provider_claims"])
        self.assertIsNone(result["clone"])
        self.assertEqual(list(self.clones.iterdir()), [])
        self.assertEqual(self._status(), "")
        self.assertEqual(self.app.stat().st_mtime_ns, self.mtime)

    def test_probe_tracked_change_is_a_violation(self) -> None:
        result = self.run_kind("probe", "edit")
        self.assertEqual(result["status"], "violation")
        self.assertEqual(result["violations"], ["app.py"])

    def test_execute_owned_change_keeps_clone_until_removed(self) -> None:
        result = self.run_kind("execute", "edit", owns=["app.py"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["violations"], [])
        diff_path = Path(result["diff_path"])
        self.assertTrue(diff_path.is_file())
        self.assertIn("app.py", diff_path.read_text(encoding="utf-8"))
        self.assertEqual(result["files_changed"], ["app.py"])
        self.assertTrue(Path(result["clone"]).is_dir())
        [directory] = list(self.clones.iterdir())
        with mock.patch.dict(os.environ, {"AGENT_RELAY_CLONES_DIR": str(self.clones)}):
            isolation.remove_clone(directory)
        self.assertFalse(directory.exists())

    def test_execute_unowned_change_is_a_violation(self) -> None:
        result = self.run_kind("execute", "other", owns=["app.py"])
        self.assertEqual(result["status"], "violation")
        self.assertEqual(result["violations"], ["other.py"])

    def test_execute_tracked_deletion_is_a_violation(self) -> None:
        result = self.run_kind("execute", "delete", owns=["app.py"])
        self.assertEqual(result["status"], "violation")
        self.assertIn("test_app.py", result["violations"])

    def test_execute_git_metadata_change_is_a_violation(self) -> None:
        result = self.run_kind("execute", "hook", owns=["app.py"])
        self.assertEqual(result["status"], "violation")
        self.assertIn(".git/hooks/post-commit", result["violations"])

    def test_invalid_owns_globs_fail_before_the_clone_exists(self) -> None:
        for owns in (["../x"], ["/abs"]):
            with self.subTest(owns=owns), self.assertRaises(RelayError):
                self.run_kind("execute", "edit", owns=owns)
        self.assertEqual(list(self.clones.iterdir()), [])

    def test_probe_rejects_owns(self) -> None:
        with self.assertRaisesRegex(RelayError, "probe must not declare --owns"):
            self.run_kind("probe", "probe", owns=["app.py"])

    def test_read_rejects_tool_options(self) -> None:
        with self.assertRaisesRegex(RelayError, "apply only to probe and execute"):
            self.run_kind("read", "probe", checks=["python3 -c 'pass'"])

    def test_relay_reruns_declared_checks_instead_of_trusting_claims(self) -> None:
        result = self.run_kind("probe", "claim", checks=["python3 -c 'raise SystemExit(3)'"])
        self.assertEqual(result["status"], "check_failed")
        self.assertEqual(result["checks"][0]["exit_code"], 3)
        self.assertIn("all checks passed", result["provider_claims"])

    def test_checks_run_without_a_shell(self) -> None:
        check = "python3 -c 'import sys; print(sys.argv[1:])' a;b && c"
        result = self.run_kind("probe", "probe", checks=[check])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["checks"][0]["exit_code"], 0)
        self.assertIn("['a;b', '&&', 'c']", result["checks"][0]["output_tail"])

    def test_empty_provider_output_is_an_error_and_removes_the_clone(self) -> None:
        result = self.run_kind("probe", "empty")
        self.assertEqual(result["status"], "error")
        self.assertEqual(list(self.clones.iterdir()), [])

    def test_provider_timeout_is_a_stop_and_keeps_logs(self) -> None:
        result = self.run_kind("probe", "sleep", timeout=1)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["stop_reason"], "timeout")
        self.assertEqual(list(self.clones.iterdir()), [])
        self.assertTrue((Path(result["output_dir"]) / "stderr.log").is_file())

    def assert_edit_salvaged(self, result: dict[str, Any]) -> None:
        diff_path = Path(result["diff_path"])
        self.assertEqual(diff_path, Path(result["output_dir"]) / "changes.diff")
        self.assertIn("+    return 2", diff_path.read_text(encoding="utf-8"))
        self.assertEqual(result["files_changed"], ["app.py"])
        self.assertEqual(result["violations"], [])
        self.assertIsNone(result["clone"])
        self.assertEqual(list(self.clones.iterdir()), [])
        recorded = json.loads((Path(result["output_dir"]) / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(recorded["stop_reason"], result["stop_reason"])
        self.assertEqual(self._status(), "")

    def test_timeout_keeps_the_workers_diff_and_partial_answer(self) -> None:
        result = self.run_kind("execute", "edit-sleep", owns=["app.py"], timeout=2)
        self.assertEqual((result["status"], result["stop_reason"]), ("stopped", "timeout"))
        self.assert_edit_salvaged(result)
        self.assertEqual(result["checks"], [])
        self.assertIn("still working", result["partial_answer"])
        self.assertIn("still working", Path(result["partial_answer_path"]).read_text(encoding="utf-8"))

    def test_cancellation_keeps_the_workers_diff(self) -> None:
        cancel_file = self.directory / "cancel.request"
        args = self.make_args("execute", owns=["app.py"])
        task = prepare_task(args)
        output = args.output.resolve()
        output.mkdir(mode=0o700, parents=True)
        environment = {
            "FAKE_MODE": "edit-cancel",
            "FAKE_CANCEL": str(cancel_file),
            "AGENT_RELAY_CLONES_DIR": str(self.clones),
        }
        with mock.patch.dict(os.environ, environment):
            result = run_prepared(task, output, cancel_file)
        self.assertEqual((result["status"], result["stop_reason"]), ("cancelled", "cancelled"))
        self.assert_edit_salvaged(result)

    def test_runaway_output_keeps_the_workers_diff(self) -> None:
        result = self.run_kind("execute", "edit-flood", owns=["app.py"], max_log_bytes=200_000, max_event_bytes=10_000)
        self.assertEqual((result["status"], result["stop_reason"]), ("stopped", "runaway_output"))
        self.assert_edit_salvaged(result)
        self.assertLessEqual((Path(result["output_dir"]) / "stdout.log").stat().st_size, 200_000)
        self.assertEqual(result["log_budget"]["max_log_bytes"], 200_000)
        self.assertGreater(result["log_stats"]["stdout"]["dropped_bytes"], 0)

    def test_provider_failure_after_edits_keeps_the_workers_diff(self) -> None:
        result = self.run_kind("execute", "edit-fail", owns=["app.py"])
        self.assertEqual(result["status"], "error")
        self.assertIn("code 3", result["error"])
        self.assertNotIn("stop_reason", result)
        self.assertIn("+    return 2", Path(result["diff_path"]).read_text(encoding="utf-8"))
        self.assertEqual(list(self.clones.iterdir()), [])

    def test_an_internal_error_still_publishes_a_terminal_result_and_the_diff(self) -> None:
        args = self.make_args("execute", owns=["app.py"])
        task = prepare_task(args)
        output = args.output.resolve()
        output.mkdir(mode=0o700, parents=True)
        real = task_runner.decode_response

        def broken(*arguments: Any) -> Any:
            real(*arguments)
            raise RuntimeError("decoder bug")

        environment = {"FAKE_MODE": "edit", "AGENT_RELAY_CLONES_DIR": str(self.clones)}
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(task_runner, "decode_response", broken),
            self.assertRaisesRegex(RuntimeError, "decoder bug"),
        ):
            run_prepared(task, output)
        recorded = json.loads((output / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(recorded["status"], "error")
        self.assertIn("decoder bug", recorded["error"])
        self.assertIn("+    return 2", (output / "changes.diff").read_text(encoding="utf-8"))
        self.assertEqual(list(self.clones.iterdir()), [])

    def test_failed_capture_keeps_the_clone_for_manual_recovery(self) -> None:
        with mock.patch.object(task_runner, "capture", side_effect=RelayError("git timed out")):
            result = self.run_kind("execute", "edit-sleep", owns=["app.py"], timeout=2)
        self.assertEqual((result["status"], result["stop_reason"]), ("stopped", "timeout"))
        self.assertEqual(result["capture_error"], "git timed out")
        clone = Path(result["clone"])
        self.assertIn("return 2", (clone / "app.py").read_text(encoding="utf-8"))
        with mock.patch.dict(os.environ, {"AGENT_RELAY_CLONES_DIR": str(self.clones)}):
            isolation.remove_clone(clone.parent)

    def test_a_failed_diff_write_keeps_the_clone(self) -> None:
        with mock.patch.object(task_runner, "_record_capture", side_effect=OSError(28, "No space left on device")):
            result = self.run_kind("execute", "edit-sleep", owns=["app.py"], timeout=2)
        self.assertEqual((result["status"], result["stop_reason"]), ("stopped", "timeout"))
        self.assertIn("No space left", result["salvage_error"])
        clone = Path(result["clone"])
        self.assertIn("return 2", (clone / "app.py").read_text(encoding="utf-8"))
        with mock.patch.dict(os.environ, {"AGENT_RELAY_CLONES_DIR": str(self.clones)}):
            isolation.remove_clone(clone.parent)

    def test_a_clipped_answer_fails_the_run_but_keeps_what_fit(self) -> None:
        script = self.directory / "clipped.py"
        script.write_text(
            "import json, pathlib\n"
            "pathlib.Path('app.py').write_text('def answer():\\n    return 2\\n')\n"
            "for i in range(60):\n"
            "    print(json.dumps({'type': 'tool_call', 'subtype': 'completed', 'r': 'x' * 4000}))\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'A' * 300000}))\n",
            encoding="utf-8",
        )
        adapter = json.loads(self.adapter.read_text(encoding="utf-8"))
        adapter["output"] = "cursor-jsonl"
        adapter["tools"]["args"] = [str(script), "{request_file}"]
        self.adapter.write_text(json.dumps(adapter), encoding="utf-8")
        result = self.run_kind("execute", "edit", owns=["app.py"], max_log_bytes=400_000, max_event_bytes=4_000)
        self.assertEqual(result["status"], "error")
        self.assertIn("did not fit", result["error"])
        self.assertTrue(result["partial_answer"].startswith("AAAA"))
        self.assertIn("+    return 2", Path(result["diff_path"]).read_text(encoding="utf-8"))
        self.assertEqual(list(self.clones.iterdir()), [])

    def test_salvage_never_recaptures_over_a_published_diff(self) -> None:
        output = self.directory / "salvage-out"
        output.mkdir()
        (output / "changes.diff").write_text("original diff\n", encoding="utf-8")
        result: dict[str, Any] = {"kind": "execute", "diff_path": str(output / "changes.diff")}
        task: dict[str, Any] = {"tools": {"check_timeout": 5, "owns": []}, "max_answer_chars": 100}
        task["adapter"] = {"output": "text"}
        with mock.patch.object(task_runner, "capture", side_effect=AssertionError("recaptured")):
            task_runner.salvage(task, result, output, mock.Mock(), {}, "answer")
        self.assertEqual((output / "changes.diff").read_text(encoding="utf-8"), "original diff\n")
        self.assertNotIn("capture_error", result)

    def test_stopped_probe_keeps_its_untracked_files(self) -> None:
        result = self.run_kind("probe", "probe-sleep", timeout=2)
        self.assertEqual((result["status"], result["stop_reason"]), ("stopped", "timeout"))
        self.assertEqual(result["untracked_files"], ["probe.txt"])
        self.assertIn("+7319", Path(result["diff_path"]).read_text(encoding="utf-8"))
        self.assertEqual(result["violations"], [])
        self.assertIn("wrote probe.txt", result["partial_answer"])
        self.assertEqual(list(self.clones.iterdir()), [])

    def test_tool_results_record_the_effective_log_budget(self) -> None:
        result = self.run_kind("execute", "edit", owns=["app.py"])
        self.assertEqual(result["log_budget"]["sources"], {"max_log_bytes": "kind", "max_event_bytes": "kind"})
        self.assertGreater(result["log_budget"]["hard_ceiling_bytes"], result["log_budget"]["max_log_bytes"])
        with mock.patch.dict(os.environ, {"AGENT_RELAY_CLONES_DIR": str(self.clones)}):
            isolation.remove_clone(Path(result["clone"]).parent)

    def test_cancellation_removes_the_clone(self) -> None:
        cancel_file = self.directory / "cancel.request"
        args = self.make_args("probe")
        task = prepare_task(args)
        output = args.output.resolve()
        output.mkdir(mode=0o700, parents=True)
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_MODE": "cancel",
                "FAKE_CANCEL": str(cancel_file),
                "AGENT_RELAY_CLONES_DIR": str(self.clones),
            },
        ):
            result = run_prepared(task, output, cancel_file)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(list(self.clones.iterdir()), [])

    def test_cancellation_during_a_relay_check_removes_the_execute_clone(self) -> None:
        cancel_file = self.directory / "cancel.request"
        check = (
            f"python3 -c 'import pathlib, time; pathlib.Path({json.dumps(str(cancel_file))}).touch(); time.sleep(30)'"
        )
        args = self.make_args("execute", owns=["app.py"], checks=[check])
        task = prepare_task(args)
        output = args.output.resolve()
        output.mkdir(mode=0o700, parents=True)
        with mock.patch.dict(os.environ, {"FAKE_MODE": "edit", "AGENT_RELAY_CLONES_DIR": str(self.clones)}):
            result = run_prepared(task, output, cancel_file)
        self.assertEqual(result["status"], "cancelled")
        self.assertLess(result["duration_seconds"], 15)
        self.assertEqual(result["stop_reason"], "cancelled")
        self.assertEqual(result["checks"], [])
        self.assertIn("app.py", Path(result["diff_path"]).read_text(encoding="utf-8"))
        self.assertIn("edited app.py", result["provider_claims"])
        self.assertIsNone(result["clone"])
        self.assertEqual(list(self.clones.iterdir()), [])

    def test_changes_made_by_relay_checks_are_captured(self) -> None:
        check = 'python3 -c \'open("stray.txt", "w").write("x")\''
        result = self.run_kind("execute", "edit", owns=["app.py"], checks=[check])
        self.assertEqual(result["checks"][0]["exit_code"], 0)
        self.assertEqual(result["status"], "violation")
        self.assertEqual(result["violations"], ["stray.txt"])
        self.assertEqual(result["check_side_effects"], ["stray.txt"])
        self.assertIn("stray.txt", Path(result["diff_path"]).read_text(encoding="utf-8"))
        with mock.patch.dict(os.environ, {"AGENT_RELAY_CLONES_DIR": str(self.clones)}):
            isolation.remove_clone(Path(result["clone"]).parent)

    def test_tool_files_are_checked_against_the_pinned_commit(self) -> None:
        self.app.unlink()
        (self.repository / "fresh.py").write_text("x = 1\n", encoding="utf-8")
        result = self.run_kind("probe", "probe", files=["app.py"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["files"], ["app.py"])
        with self.assertRaisesRegex(RelayError, "not a file in the pinned commit"):
            self.run_kind("probe", "probe", files=["fresh.py"])
        for given in ("../x", "/etc/passwd", ".git/config", ".env"):
            with self.subTest(given=given), self.assertRaises(RelayError):
                self.run_kind("probe", "probe", files=[given])

    def test_setup_runs_inside_the_clone_before_the_provider(self) -> None:
        setup = ['python3 -c \'open("setup.txt","w").write("x")\'']
        result = self.run_kind("probe", "probe-setup", setup=setup)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["untracked_files"], ["probe.txt", "setup.txt"])

    def test_provider_without_tools_mode_is_rejected(self) -> None:
        adapter = json.loads(self.adapter.read_text(encoding="utf-8"))
        del adapter["tools"]
        self.adapter.write_text(json.dumps(adapter), encoding="utf-8")
        for kind, options in (("probe", {}), ("execute", {"owns": ["app.py"]})):
            with self.subTest(kind=kind), self.assertRaisesRegex(RelayError, "has no verified tools mode"):
                self.run_kind(kind, "probe", **options)

    def test_tool_processes_see_the_clone_as_pwd(self) -> None:
        check = "python3 -c 'import os; assert os.path.realpath(os.environ[\"PWD\"]) == os.path.realpath(os.getcwd())'"
        with mock.patch.dict(os.environ, {"PWD": str(ROOT)}):
            result = self.run_kind("probe", "probe", checks=[check])
        self.assertEqual(result["status"], "ok", result.get("error"))
        self.assertEqual(result["checks"][0]["exit_code"], 0)

    def test_provider_environment_excludes_git_variables_and_keeps_tools_env(self) -> None:
        with mock.patch.dict(os.environ, {"GIT_DIR": "/nonexistent"}):
            result = self.run_kind("probe", "probe")
        self.assertEqual(result["status"], "ok")
        self.assertIn("GIT_DIR", result["env_removed"])


if __name__ == "__main__":
    unittest.main()
