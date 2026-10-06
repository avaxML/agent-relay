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

    def test_provider_timeout_is_an_error_and_keeps_logs(self) -> None:
        result = self.run_kind("probe", "sleep", timeout=1)
        self.assertEqual(result["status"], "error")
        self.assertEqual(list(self.clones.iterdir()), [])
        self.assertTrue((Path(result["output_dir"]) / "stderr.log").is_file())

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
