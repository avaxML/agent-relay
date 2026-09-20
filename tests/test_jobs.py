from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELAY = ROOT / "scripts" / "relay.py"
sys.path.insert(0, str(ROOT / "scripts"))
import jobs
from task_runner import prepare_task, write_json


class JobCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agent-relay-jobs-test-")
        self.directory = Path(self.temp.name)
        self.project = self.directory / "project"
        self.project.mkdir()
        self.jobs = self.directory / "jobs"
        self.task = self.directory / "task.txt"
        self.task.write_text("Find the answer.", encoding="utf-8")
        self.source = self.project / "source.txt"
        self.source.write_text("original source\n", encoding="utf-8")
        self.adapter, self.worker = self._make_worker()
        self.plugin_copy = self.directory / "plugin-copy"
        shutil.copytree(ROOT, self.plugin_copy)
        self.relay = self.plugin_copy / "scripts" / "relay.py"
        self.active: list[str] = []

    def tearDown(self) -> None:
        for job_id in self.active:
            self._cli("cancel", job_id, "--jobs-dir", str(self.jobs), relay=RELAY)
            self._cli("wait", job_id, "--jobs-dir", str(self.jobs), "--timeout", "3", relay=RELAY)
        self.temp.cleanup()

    def _make_worker(self) -> tuple[Path, Path]:
        worker = self.directory / "fake_worker.py"
        worker.write_text(
            textwrap.dedent(
                """
                import json
                import os
                import pathlib
                import sys
                import time

                request = json.load(sys.stdin)
                mode = os.environ.get("FAKE_MODE", "success")
                started = pathlib.Path(os.environ.get("FAKE_STARTED", "/dev/null"))
                with started.open("a", encoding="utf-8") as stream:
                    stream.write("started\\n")
                if mode == "sleep":
                    time.sleep(float(os.environ.get("FAKE_DELAY", "0.5")))
                if mode == "fail":
                    print("worker failed", file=sys.stderr)
                    raise SystemExit(9)
                answer = "answer:" + request["task"]
                if os.environ.get("FAKE_INCLUDE_SOURCE"):
                    answer += "|source:" + request["sources"][0]["numbered_content"]
                print(answer)
                """
            ),
            encoding="utf-8",
        )
        adapter = self.directory / "adapter.json"
        adapter.write_text(
            json.dumps(
                {
                    "name": "fake",
                    "executable": sys.executable,
                    "default_model": "fake-model",
                    "args": [str(worker)],
                    "input": "stdin",
                    "output": "text",
                    "env": {"FAKE_STARTED": str(self.directory / "started")},
                    "models_args": [],
                    "probe_args": [],
                    "effort": {
                        "models": ["fake-model"],
                        "levels": {
                            "high": {"args": ["--fake-high"]},
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        return adapter, worker

    def _cli(self, *args: str, timeout: float = 8, relay: Path | None = None) -> tuple[int, dict]:
        command = [
            sys.executable,
            str(relay or self.relay),
            *args,
        ]
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, env=environment, check=False
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"CLI returned non-JSON output: {completed.stdout!r}; stderr={completed.stderr!r}: {exc}")
        return completed.returncode, payload

    def _submit(self, *extra: str, timeout: int = 3) -> tuple[str, dict]:
        code, payload = self._cli(
            "submit",
            "--provider",
            "fake",
            "--adapter-file",
            str(self.adapter),
            "--task-file",
            str(self.task),
            "--root",
            str(self.project),
            "--files",
            "source.txt",
            "--jobs-dir",
            str(self.jobs),
            "--timeout",
            str(timeout),
            *extra,
        )
        self.assertEqual(code, 0, payload)
        job_id = payload["job_id"]
        self.active.append(job_id)
        return job_id, payload

    def test_submit_returns_promptly_and_detached_job_is_retrievable_from_new_process(self) -> None:
        started = time.monotonic()
        job_id, submitted = self._submit()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(len(job_id), 32)
        code, result = self._cli("wait", job_id, "--jobs-dir", str(self.jobs), "--timeout", "3")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "completed")
        code, fetched = self._cli("result", job_id, "--jobs-dir", str(self.jobs))
        self.assertEqual(code, 0, fetched)
        self.assertTrue(fetched["result_ready"])
        self.assertEqual(fetched["result"]["status"], "ok")
        self.assertEqual(fetched["result"]["answer"], "answer:Find the answer.")
        self.assertEqual(fetched["result"]["effort"], None)
        self.assertEqual(submitted["provider"], "fake")

    def test_wait_zero_reports_pending_without_cancelling_job(self) -> None:
        adapter = json.loads(self.adapter.read_text())
        adapter["env"]["FAKE_MODE"] = "sleep"
        adapter["env"]["FAKE_DELAY"] = "1"
        self.adapter.write_text(json.dumps(adapter), encoding="utf-8")
        job_id, _ = self._submit("--output", str(self.directory / "artifacts"), timeout=2)
        code, state = self._cli("wait", job_id, "--jobs-dir", str(self.jobs), "--timeout", "0")
        self.assertEqual(code, 2, state)
        self.assertTrue(state["wait_timed_out"])
        self.assertNotEqual(state["status"], "cancelled")
        code, finished = self._cli("wait", job_id, "--jobs-dir", str(self.jobs), "--timeout", "3")
        self.assertEqual(code, 0, finished)
        self.assertEqual(finished["status"], "completed")

    def test_snapshot_is_immutable_when_inputs_and_adapter_change_after_submit(self) -> None:
        adapter = json.loads(self.adapter.read_text())
        adapter["env"]["FAKE_MODE"] = "sleep"
        adapter["env"]["FAKE_DELAY"] = "0.4"
        adapter["env"]["FAKE_INCLUDE_SOURCE"] = "1"
        self.adapter.write_text(json.dumps(adapter))
        job_id, _ = self._submit("--effort", "high")
        self.source.write_text("mutated source\n", encoding="utf-8")
        self.task.write_text("mutated task", encoding="utf-8")
        adapter["env"]["FAKE_MODE"] = "fail"
        self.adapter.write_text(json.dumps(adapter), encoding="utf-8")
        code, result = self._cli("wait", job_id, "--jobs-dir", str(self.jobs), "--timeout", "3")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "completed")
        code, fetched = self._cli("result", job_id, "--jobs-dir", str(self.jobs))
        self.assertEqual(code, 0, fetched)
        self.assertEqual(
            fetched["result"]["answer"],
            "answer:Find the answer.|source:1: original source",
        )
        self.assertEqual(fetched["result"]["effort"], "high")
        self.assertEqual(fetched["result"]["model"], "fake-model")

    def test_failed_worker_is_terminal_and_result_contains_error(self) -> None:
        adapter = json.loads(self.adapter.read_text())
        adapter["env"]["FAKE_MODE"] = "fail"
        self.adapter.write_text(json.dumps(adapter))
        job_id, _ = self._submit()
        code, state = self._cli("wait", job_id, "--jobs-dir", str(self.jobs), "--timeout", "3", relay=RELAY)
        self.assertEqual(code, 1, state)
        self.assertEqual(state["status"], "failed")
        code, fetched = self._cli("result", job_id, "--jobs-dir", str(self.jobs))
        self.assertEqual(code, 1, fetched)
        self.assertTrue(fetched["result_ready"])
        self.assertEqual(fetched["result"]["status"], "error")
        self.assertIn("code 9", fetched["result"]["error"])

    def test_cancel_is_idempotent_and_stops_process_group(self) -> None:
        child_marker = self.directory / "child-marker"
        worker = self.directory / "cancel_worker.py"
        worker.write_text(
            textwrap.dedent(
                f"""
                import pathlib, subprocess, sys, time
                marker = pathlib.Path({str(child_marker)!r})
                child = subprocess.Popen([sys.executable, '-c', 'import pathlib,sys,time; time.sleep(2); pathlib.Path(sys.argv[1]).write_text("escaped")', str(marker)])
                pathlib.Path({str(self.directory / "cancel-started")!r}).write_text("started")
                time.sleep(10)
                """
            ),
            encoding="utf-8",
        )
        worker.chmod(worker.stat().st_mode | stat.S_IXUSR)
        adapter = json.loads(self.adapter.read_text())
        adapter["args"] = [str(worker)]
        adapter["env"]["FAKE_STARTED"] = str(self.directory / "cancel-started")
        self.adapter.write_text(json.dumps(adapter))
        job_id, _ = self._submit("--timeout", "10")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not (self.directory / "cancel-started").exists():
            time.sleep(0.05)
        self.assertTrue((self.directory / "cancel-started").exists())
        code, first = self._cli("cancel", job_id, "--jobs-dir", str(self.jobs))
        self.assertEqual(code, 0, first)
        code, second = self._cli("cancel", job_id, "--jobs-dir", str(self.jobs))
        self.assertEqual(code, 0, second)
        self.assertIn(second["status"], {"cancelling", "cancelled"})
        code, state = self._cli("wait", job_id, "--jobs-dir", str(self.jobs), "--timeout", "3", relay=RELAY)
        self.assertEqual(code, 1, state)
        self.assertEqual(state["status"], "cancelled")
        time.sleep(2.2)
        self.assertFalse(child_marker.exists())

    def test_independent_jobs_start_concurrently(self) -> None:
        adapter = json.loads(self.adapter.read_text())
        adapter["env"]["FAKE_MODE"] = "sleep"
        adapter["env"]["FAKE_DELAY"] = "0.8"
        self.adapter.write_text(json.dumps(adapter), encoding="utf-8")
        first, _ = self._submit()
        second, _ = self._submit()
        started = self.directory / "started"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if started.exists() and len(started.read_text(encoding="utf-8").splitlines()) >= 2:
                break
            time.sleep(0.05)
        self.assertGreaterEqual(len(started.read_text(encoding="utf-8").splitlines()), 2)
        for job_id in (first, second):
            code, state = self._cli("wait", job_id, "--jobs-dir", str(self.jobs), "--timeout", "3")
            self.assertEqual(code, 0, state)

    def test_detached_worker_survives_removal_of_original_plugin_tree(self) -> None:
        adapter = json.loads(self.adapter.read_text())
        adapter["env"]["FAKE_MODE"] = "sleep"
        adapter["env"]["FAKE_DELAY"] = "0.4"
        self.adapter.write_text(json.dumps(adapter), encoding="utf-8")
        job_id, _ = self._submit()
        shutil.rmtree(self.plugin_copy)
        code, state = self._cli("wait", job_id, "--jobs-dir", str(self.jobs), "--timeout", "3", relay=RELAY)
        self.assertEqual(code, 0, state)
        self.assertEqual(state["status"], "completed")

    def test_worker_waits_for_startup_reader_lock_instead_of_exiting(self) -> None:
        task = prepare_task(
            type(
                "Args",
                (),
                {
                    "provider": "fake",
                    "adapter_file": self.adapter,
                    "model": None,
                    "effort": None,
                    "root": self.project,
                    "files": ["source.txt"],
                    "task_file": self.task,
                    "kind": "read",
                    "max_input_bytes": 10000,
                    "timeout": 2,
                    "max_answer_chars": 12000,
                },
            )()
        )
        job_id = uuid.uuid4().hex
        job_dir = self.jobs / job_id
        output = job_dir / "artifacts"
        output.mkdir(parents=True)
        (job_dir / "worker.lock").touch()
        write_json(
            job_dir / "state.json",
            {
                "schema_version": 1,
                "job_id": job_id,
                "status": "queued",
                "created_at": time.time(),
                "output_dir": str(output),
            },
        )
        write_json(job_dir / "task.json", task)
        lock = (job_dir / "worker.lock").open("r+b")
        fcntl.flock(lock, fcntl.LOCK_SH)
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "jobs.py"), str(job_dir)],
            cwd=job_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.3)
            self.assertIsNone(process.poll())
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
            self.assertEqual(process.wait(timeout=3), 0)
            result = json.loads((output / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "ok")
        finally:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
            except (OSError, ValueError):
                pass
            try:
                lock.close()
            except OSError:
                pass
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)

    def test_malformed_result_json_does_not_crash_status_or_result(self) -> None:
        job_id, _ = self._submit()
        code, state = self._cli("wait", job_id, "--jobs-dir", str(self.jobs), "--timeout", "3")
        self.assertEqual(code, 0, state)
        path = Path(jobs.result(job_id, self.jobs)["output_dir"]) / "result.json"
        path.write_text("[1, 2, 3]\n", encoding="utf-8")
        status = jobs.status(job_id, self.jobs)
        self.assertEqual(status["status"], "completed")
        payload = jobs.result(job_id, self.jobs)
        self.assertFalse(payload["result_ready"])
        path.write_text('{"status":"ok","answer":"', encoding="utf-8")
        truncated = jobs.result(job_id, self.jobs)
        self.assertFalse(truncated["result_ready"])

    def test_invalid_job_ids_and_path_traversal_are_rejected(self) -> None:
        for job_id in ("bad", "../" + "a" * 32, "a" * 31):
            code, payload = self._cli("status", job_id, "--jobs-dir", str(self.jobs))
            self.assertEqual(code, 1, payload)
            self.assertIn("Invalid job ID", payload["error"])

    def test_stale_queued_job_is_reported_interrupted(self) -> None:
        job_id = uuid.uuid4().hex
        directory = self.jobs / job_id
        directory.mkdir(parents=True)
        state = {
            "job_id": job_id,
            "status": "queued",
            "created_at": time.time() - 30,
            "output_dir": str(directory / "artifacts"),
        }
        (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")
        (directory / "worker.lock").touch()
        code, payload = self._cli("status", job_id, "--jobs-dir", str(self.jobs))
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "interrupted")

    def test_submit_does_not_overwrite_explicit_output_directory(self) -> None:
        output = self.directory / "existing"
        output.mkdir()
        code, payload = self._cli(
            "submit",
            "--provider",
            "fake",
            "--adapter-file",
            str(self.adapter),
            "--task-file",
            str(self.task),
            "--root",
            str(self.project),
            "--files",
            "source.txt",
            "--jobs-dir",
            str(self.jobs),
            "--output",
            str(output),
        )
        self.assertEqual(code, 1, payload)
        self.assertIn("File exists", payload["error"])


if __name__ == "__main__":
    unittest.main()
