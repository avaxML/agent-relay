from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RELAY = ROOT / "scripts" / "relay.py"
sys.path.insert(0, str(ROOT / "scripts"))

import dispatch
from providers import RelayError

KINDS = {"read": False, "review": False, "probe": True, "execute": True}
ORDER = ["antigravity", "opencode", "cursor"]
HEALTHY = {"ok": True, "tools": True, "reason": "ok"}


def pinned_ref(root: Path, ref: str) -> str:
    return "a" * 40


class Fakes:
    def __init__(self, health: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self.health = dict(health or {name: HEALTHY for name in ORDER})
        self.doctor_calls: list[str] = []
        self.submitted: list[argparse.Namespace] = []
        self.statuses: dict[str, str] = {}
        self.results: dict[str, dict[str, Any]] = {}

    def doctor(self, name: str) -> Mapping[str, Any]:
        self.doctor_calls.append(name)
        return self.health[name]

    def submit(self, args: argparse.Namespace) -> Mapping[str, Any]:
        self.submitted.append(args)
        job_id = f"job{len(self.submitted)}"
        return {"job_id": job_id, "status": "queued", "output_dir": f"/out/{job_id}"}

    def status(self, job_id: str) -> Mapping[str, Any]:
        return {"job_id": job_id, "status": self.statuses.get(job_id, "queued")}

    def result(self, job_id: str) -> Mapping[str, Any]:
        state = self.status(job_id)
        if job_id in self.results:
            return {**state, "result_ready": True, "result": self.results[job_id]}
        return {**state, "result_ready": False}


class DispatchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agent-relay-dispatch-test-")
        self.directory = Path(self.temp.name)
        self.plan = self.directory / "plan"
        self.plan.mkdir()
        self.project = self.directory / "project"
        self.project.mkdir()
        self.runs = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def task(self, task_id: str, kind: str = "probe", **extra: Any) -> dict[str, Any]:
        path = self.plan / f"{task_id}.md"
        path.write_text(f"Task {task_id}", encoding="utf-8")
        return {"id": task_id, "kind": kind, "task_file": path.name, "files": ["src/app.py"], **extra}

    def manifest(self, tasks: list[dict[str, Any]], document: Any = None) -> Path:
        path = self.plan / "manifest.json"
        path.write_text(json.dumps({"tasks": tasks} if document is None else document), encoding="utf-8")
        return path

    def create(
        self,
        tasks: list[dict[str, Any]],
        fakes: Fakes,
        max_per_provider: int = 2,
        pin_ref: Callable[[Path, str], str] = pinned_ref,
    ) -> Path:
        self.runs += 1
        return dispatch.create(
            self.manifest(tasks),
            self.project,
            ORDER,
            {"ref": "HEAD", "jobs_dir": self.directory / "jobs", "timeout": None},
            KINDS,
            fakes.doctor,
            fakes.submit,
            max_per_provider=max_per_provider,
            directory=self.directory / "dispatches" / str(self.runs),
            pin_ref=pin_ref,
        )

    def ledger(self, path: Path) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return value

    def assigned(self, path: Path) -> list[int]:
        counts = self.ledger(path)["counts"]
        return [counts[name]["assigned"] for name in ORDER]

    def submitted(self, path: Path) -> list[int]:
        counts = self.ledger(path)["counts"]
        return [counts[name]["submitted"] for name in ORDER]


class AssignmentTests(DispatchTestCase):
    def test_probe_tasks_spread_in_tie_order_and_each_submitted_once(self) -> None:
        fakes = Fakes()
        path = self.create([self.task(f"t{n}") for n in range(7)], fakes, max_per_provider=3)
        self.assertEqual(self.assigned(path), [3, 2, 2])
        providers = [task["provider"] for task in self.ledger(path)["tasks"]]
        self.assertEqual(providers, ORDER * 2 + ORDER[:1])
        self.assertEqual(sorted(args.task_file.name for args in fakes.submitted), sorted(f"t{n}.md" for n in range(7)))
        self.assertEqual(self.submitted(path), [3, 2, 2])
        first = fakes.submitted[0]
        self.assertEqual(first.kind, "probe")
        self.assertEqual(first.root, self.project.resolve())
        self.assertEqual(first.jobs_dir, self.directory / "jobs")
        self.assertEqual(first.files, ["src/app.py"])
        self.assertEqual((first.ref, first.check_timeout, first.max_input_bytes), ("a" * 40, 180, 400000))

    def test_explicit_provider_is_respected_and_counted(self) -> None:
        fakes = Fakes()
        tasks = [self.task("c1", provider="cursor"), self.task("c2", provider="cursor")]
        tasks += [self.task(f"f{n}") for n in range(4)]
        path = self.create(tasks, fakes)
        self.assertEqual(self.assigned(path), [2, 2, 2])
        by_id = {task["id"]: task["provider"] for task in self.ledger(path)["tasks"]}
        self.assertEqual((by_id["c1"], by_id["c2"]), ("cursor", "cursor"))

    def test_unknown_explicit_provider_is_rejected(self) -> None:
        with self.assertRaises(RelayError):
            self.create([self.task("t", provider="nobody")], Fakes())

    def test_failing_provider_is_skipped_and_doctor_runs_once_each(self) -> None:
        fakes = Fakes({**{name: HEALTHY for name in ORDER}, "opencode": {"ok": False, "tools": True, "reason": "x"}})
        path = self.create([self.task(f"t{n}") for n in range(6)], fakes, max_per_provider=3)
        self.assertEqual(self.assigned(path), [3, 0, 3])
        self.assertEqual(fakes.doctor_calls, ORDER)

    def test_provider_without_tools_gets_only_read_tasks(self) -> None:
        fakes = Fakes({**{name: HEALTHY for name in ORDER}, "cursor": {"ok": True, "tools": False, "reason": "ok"}})
        tasks = [self.task(f"r{n}", kind="read") for n in range(3)] + [self.task(f"p{n}") for n in range(4)]
        path = self.create(tasks, fakes, max_per_provider=5)
        by_id = {task["id"]: task["provider"] for task in self.ledger(path)["tasks"]}
        self.assertIn("cursor", [by_id[f"r{n}"] for n in range(3)])
        self.assertNotIn("cursor", [by_id[f"p{n}"] for n in range(4)])

    def test_all_failing_providers_send_every_task_to_native_fallback(self) -> None:
        down = {"ok": False, "tools": False, "reason": "missing"}
        fakes = Fakes({name: down for name in ORDER})
        path = self.create([self.task("a"), self.task("b", kind="read")], fakes)
        ledger = self.ledger(path)
        self.assertEqual(fakes.submitted, [])
        self.assertEqual([task["state"] for task in ledger["tasks"]], ["needs_native_fallback"] * 2)
        fallback = ledger["needs_native_fallback"]
        self.assertEqual([item["id"] for item in fallback], ["a", "b"])
        self.assertEqual(fallback[0]["task_file"], str((self.plan / "a.md").resolve()))
        self.assertEqual(fallback[0]["reason"], "no provider passed doctor with support for kind probe")


class ManifestTests(DispatchTestCase):
    def assert_rejected(self, tasks: list[dict[str, Any]], document: Any = None) -> None:
        fakes = Fakes()
        path = self.manifest(tasks, document)
        with self.assertRaises(RelayError):
            dispatch.create(
                path, self.project, ORDER, {}, KINDS, fakes.doctor, fakes.submit, directory=self.directory / "d"
            )
        self.assertEqual(fakes.submitted, [])

    def test_overlapping_execute_owns_are_rejected_naming_both(self) -> None:
        tasks = [
            self.task("one", kind="execute", owns=["src/**"]),
            self.task("two", kind="execute", owns=["src/app.py"]),
        ]
        with self.assertRaisesRegex(RelayError, "one.*two"):
            dispatch.load_manifest(self.manifest(tasks), KINDS)
        self.assert_rejected(tasks)

    def test_disjoint_execute_owns_are_accepted(self) -> None:
        tasks = [self.task("one", kind="execute", owns=["src/**"]), self.task("two", kind="execute", owns=["tests/**"])]
        loaded = dispatch.load_manifest(self.manifest(tasks), KINDS)
        self.assertEqual([task["owns"] for task in loaded], [["src/**"], ["tests/**"]])
        self.assertEqual(set(loaded[0]), dispatch.TASK_KEYS)
        self.assertIsNone(loaded[0]["provider"])

    def test_invalid_manifests_are_rejected_before_submit(self) -> None:
        outside = self.directory / "outside.md"
        outside.write_text("x", encoding="utf-8")
        cases = [
            [self.task("a", surprise=True)],
            [self.task("a"), self.task("a")],
            [self.task("a", kind="execute", owns=["../x"])],
            [{**self.task("a"), "task_file": str(outside)}],
            [self.task("a", kind="read", checks=["true"])],
            [self.task("a", owns=["src/**"])],
            [self.task("a", kind="execute")],
            [{**self.task("a"), "files": []}],
            [self.task("a", kind="nope")],
            [{**self.task("a"), "id": "bad id!"}],
        ]
        for tasks in cases:
            with self.subTest(tasks=tasks):
                self.assert_rejected(tasks)
        self.assert_rejected([], {"tasks": [self.task("a")], "extra": 1})
        self.assert_rejected([], {"tasks": []})

    def test_tool_option_errors_reuse_task_runner_messages(self) -> None:
        no_tools = "--owns, --check, and --setup apply only to probe and execute."
        cases = [
            (self.task("a", kind="read", checks=["true"]), no_tools),
            (self.task("a", owns=["src/**"]), "probe must not declare --owns; it may only create untracked files."),
            (self.task("a", kind="execute"), "execute requires at least one --owns glob."),
        ]
        for task, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(RelayError) as caught:
                    dispatch.load_manifest(self.manifest([task]), KINDS)
                self.assertIn(f"Task {task['id']}: {message}", str(caught.exception))


class AdvanceTests(DispatchTestCase):
    def test_concurrency_cap_holds_back_tasks_until_jobs_finish(self) -> None:
        fakes = Fakes()
        path = self.create([self.task(f"t{n}") for n in range(6)], fakes, max_per_provider=1)
        ledger = self.ledger(path)
        self.assertEqual(len(fakes.submitted), 3)
        self.assertEqual([task["state"] for task in ledger["tasks"]].count("pending"), 3)
        self.assertEqual(self.assigned(path), [2, 2, 2])
        self.assertEqual(self.submitted(path), [1, 1, 1])
        dispatch.advance(path, fakes.submit, fakes.status)
        self.assertEqual(len(fakes.submitted), 3)
        for job_id in ("job1", "job2", "job3"):
            fakes.statuses[job_id] = "completed"
        dispatch.advance(path, fakes.submit, fakes.status)
        self.assertEqual(len(fakes.submitted), 6)
        self.assertEqual(self.submitted(path), [2, 2, 2])

    def strand(self, path: Path, output_exists: bool) -> Path:
        ledger = self.ledger(path)
        task = ledger["tasks"][0]
        output = path.parent / "outputs" / task["id"]
        task.update(state="submitting", job_id=None, output_dir=str(output))
        ledger["counts"][task["provider"]]["submitted"] -= 1
        path.write_text(json.dumps(ledger), encoding="utf-8")
        if output_exists:
            output.mkdir(parents=True)
        return output

    def test_stranded_submission_with_a_launched_job_is_recovered_not_resubmitted(self) -> None:
        fakes = Fakes()
        path = self.create([self.task("t0")], fakes)
        output = self.strand(path, output_exists=True)
        before = len(fakes.submitted)
        dispatch.advance(
            path, fakes.submit, fakes.status, locate=lambda found: "job-found" if found == output else None
        )
        task = self.ledger(path)["tasks"][0]
        self.assertEqual(len(fakes.submitted), before)
        self.assertEqual((task["state"], task["job_id"]), ("submitted", "job-found"))
        self.assertEqual(self.submitted(path), [1, 0, 0])

    def test_stranded_submission_that_never_launched_is_submitted_once(self) -> None:
        fakes = Fakes()
        path = self.create([self.task("t0")], fakes)
        self.strand(path, output_exists=False)
        before = len(fakes.submitted)
        dispatch.advance(path, fakes.submit, fakes.status, locate=lambda found: None)
        dispatch.advance(path, fakes.submit, fakes.status, locate=lambda found: None)
        self.assertEqual(len(fakes.submitted), before + 1)
        self.assertEqual(self.ledger(path)["tasks"][0]["state"], "submitted")

    def test_unresolved_submission_is_never_resubmitted_or_reported_complete(self) -> None:
        fakes = Fakes()
        path = self.create([self.task("t0")], fakes)
        self.strand(path, output_exists=True)
        before = len(fakes.submitted)
        clock = iter(range(100))
        aggregate = dispatch.collect(
            path, fakes.submit, fakes.status, fakes.result, timeout=3, sleep=lambda s: None, clock=lambda: next(clock)
        )
        self.assertEqual(len(fakes.submitted), before)
        self.assertFalse(aggregate["complete"])
        self.assertEqual(aggregate["tasks"][0]["state"], "submitting")

    def test_each_task_gets_a_fixed_output_directory(self) -> None:
        fakes = Fakes()
        path = self.create([self.task("t0")], fakes)
        self.assertEqual(fakes.submitted[0].output, path.parent / "outputs" / "t0")

    def test_held_back_tasks_use_the_task_text_snapshotted_at_dispatch(self) -> None:
        fakes = Fakes()
        tasks = [self.task(f"t{n}", provider="cursor") for n in range(2)]
        path = self.create(tasks, fakes, max_per_provider=1)
        (self.plan / "t1.md").write_text("Edited after dispatch", encoding="utf-8")
        (self.plan / "t0.md").unlink()
        fakes.statuses["job1"] = "completed"
        dispatch.advance(path, fakes.submit, fakes.status)
        held = fakes.submitted[1]
        self.assertEqual(held.task_file.read_text(encoding="utf-8"), "Task t1")
        self.assertTrue(held.task_file.is_relative_to(path.parent))
        self.assertEqual(self.ledger(path)["tasks"][1]["source_task_file"], str((self.plan / "t1.md").resolve()))

    def test_submit_failure_is_recorded_and_not_retried(self) -> None:
        fakes = Fakes()
        calls: list[str] = []

        def failing(args: argparse.Namespace) -> Mapping[str, Any]:
            calls.append(args.provider)
            raise RelayError("boom")

        manifest = self.manifest([self.task("t")])
        path = dispatch.create(
            manifest,
            self.project,
            ORDER,
            {},
            KINDS,
            fakes.doctor,
            failing,
            directory=self.directory / "d",
            pin_ref=pinned_ref,
        )
        task = self.ledger(path)["tasks"][0]
        self.assertEqual((task["state"], task["reason"]), ("submit_failed", "boom"))
        dispatch.advance(path, failing, fakes.status)
        self.assertEqual(len(calls), 1)

    def test_ledger_is_valid_json_from_a_new_process(self) -> None:
        path = self.create([self.task(f"t{n}") for n in range(4)], Fakes())
        script = "import json,sys; print(len(json.load(open(sys.argv[1]))['tasks']))"
        completed = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True)
        self.assertEqual(completed.stdout.strip(), "4")
        ledger = self.ledger(path)
        self.assertEqual(ledger["schema_version"], 1)
        self.assertEqual(ledger["providers"], ORDER)
        self.assertEqual(ledger["settings"]["jobs_dir"], str(self.directory / "jobs"))


class PinRefTests(DispatchTestCase):
    def test_create_pins_ref_once_and_every_task_gets_the_sha(self) -> None:
        fakes = Fakes()
        calls: list[str] = []

        def pin(root: Path, ref: str) -> str:
            calls.append(ref)
            return "a" * 40

        path = self.create([self.task(f"t{n}") for n in range(4)], fakes, max_per_provider=1, pin_ref=pin)
        ledger = self.ledger(path)
        self.assertEqual(ledger["settings"]["ref"], "a" * 40)
        self.assertEqual(ledger["settings"]["requested_ref"], "HEAD")
        self.assertEqual([args.ref for args in fakes.submitted], ["a" * 40] * 3)
        fakes.statuses["job1"] = "completed"
        dispatch.advance(path, fakes.submit, fakes.status)
        self.assertEqual([args.ref for args in fakes.submitted], ["a" * 40] * 4)
        self.assertEqual(calls, ["HEAD"])

    def test_default_pin_ref_resolves_a_real_repository_head(self) -> None:
        repository = self.directory / "repository"
        repository.mkdir()
        subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "--allow-empty", "-m", "init"],
            cwd=repository,
            check=True,
            capture_output=True,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
        ).stdout.strip()
        fakes = Fakes()
        path = dispatch.create(
            self.manifest([self.task("a")]),
            repository,
            ORDER,
            {"ref": "HEAD", "jobs_dir": self.directory / "jobs", "timeout": None},
            KINDS,
            fakes.doctor,
            fakes.submit,
            directory=self.directory / "d",
        )
        ledger = self.ledger(path)
        self.assertEqual((ledger["settings"]["requested_ref"], ledger["settings"]["ref"]), ("HEAD", head))
        self.assertEqual([args.ref for args in fakes.submitted], [head])


class CollectTests(DispatchTestCase):
    def test_collect_aggregates_and_is_idempotent(self) -> None:
        fakes = Fakes({**{name: HEALTHY for name in ORDER}, "cursor": {"ok": True, "tools": False, "reason": "ok"}})
        tasks = [self.task(f"t{n}") for n in range(3)]
        path = self.create(tasks, fakes)
        fakes.health = {name: {"ok": False, "tools": False, "reason": "down"} for name in ORDER}
        for n in range(1, 4):
            fakes.statuses[f"job{n}"] = "completed"
            fakes.results[f"job{n}"] = {"status": "ok", "violations": [], "checks": [], "clone": None}
        fakes.results["job3"]["status"] = "violation"
        fakes.statuses["job3"] = "failed"
        aggregate = dispatch.collect(path, fakes.submit, fakes.status, fakes.result, timeout=5)
        self.assertTrue(aggregate["complete"])
        self.assertEqual(aggregate["ledger"], str(path))
        self.assertEqual([task["status"] for task in aggregate["tasks"]], ["ok", "violation", "ok"])
        self.assertEqual(
            aggregate["providers"]["antigravity"], {"assigned": 2, "submitted": 2, "completed": 2, "ok": 2}
        )
        self.assertEqual(aggregate["providers"]["opencode"]["ok"], 0)
        self.assertEqual(aggregate["providers"]["cursor"]["assigned"], 0)
        self.assertEqual(aggregate["needs_native_fallback"], [])
        before = len(fakes.submitted)
        again = dispatch.collect(path, fakes.submit, fakes.status, fakes.result, timeout=5)
        self.assertEqual(again, aggregate)
        self.assertEqual(len(fakes.submitted), before)

    def test_collect_lists_fallbacks(self) -> None:
        fakes = Fakes({name: {"ok": False, "tools": True, "reason": "down"} for name in ORDER})
        path = self.create([self.task("a")], fakes)
        aggregate = dispatch.collect(path, fakes.submit, fakes.status, fakes.result, timeout=0)
        self.assertTrue(aggregate["complete"])
        self.assertEqual([item["id"] for item in aggregate["needs_native_fallback"]], ["a"])
        self.assertEqual(aggregate["tasks"][0]["state"], "needs_native_fallback")

    def test_collect_stops_at_deadline_without_real_waiting(self) -> None:
        fakes = Fakes()
        path = self.create([self.task("a")], fakes)
        now = [0.0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        aggregate = dispatch.collect(
            path, fakes.submit, fakes.status, fakes.result, timeout=3, poll=1, sleep=sleep, clock=lambda: now[0]
        )
        self.assertFalse(aggregate["complete"])
        self.assertGreaterEqual(now[0], 3)
        self.assertEqual(aggregate["tasks"][0]["job_status"], "queued")
        self.assertIsNone(aggregate["tasks"][0]["status"])

    def test_cleanup_removes_each_clone_directory_once(self) -> None:
        fakes = Fakes()
        path = self.create([self.task(f"t{n}") for n in range(3)], fakes)
        fakes.statuses.update(job1="completed", job2="completed", job3="failed")
        fakes.results["job1"] = {"status": "ok", "clone": "/clones/one/work"}
        fakes.results["job2"] = {"status": "ok", "clone": None}
        fakes.results["job3"] = {"status": "check_failed", "clone": "/clones/three/work"}
        removed: list[Path] = []
        outcome = dispatch.cleanup(path, fakes.result, removed.append)
        self.assertEqual(removed, [Path("/clones/one"), Path("/clones/three")])
        self.assertEqual(outcome["removed"], ["/clones/one", "/clones/three"])
        self.assertEqual(outcome["dispatch_id"], self.ledger(path)["dispatch_id"])
        self.assertTrue(path.is_file())

    def test_cleanup_reports_lookup_errors_and_keeps_cleaning(self) -> None:
        fakes = Fakes()
        path = self.create([self.task("first"), self.task("second")], fakes, max_per_provider=1)
        fakes.results["job2"] = {"status": "ok", "clone": "/clones/two/work"}

        def result(job_id: str) -> Mapping[str, Any]:
            if job_id == "job1":
                raise RelayError("result.json is missing")
            return fakes.result(job_id)

        removed: list[Path] = []
        outcome = dispatch.cleanup(path, result, removed.append)
        self.assertEqual(removed, [Path("/clones/two")])
        self.assertEqual(outcome["errors"], [{"id": "first", "error": "result.json is missing"}])


class FindByOutputTests(unittest.TestCase):
    def test_find_by_output_returns_the_owning_job(self) -> None:
        import jobs

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for job_id, output in (("a" * 32, root / "out-a"), ("b" * 32, root / "out-b")):
                (root / job_id).mkdir()
                (root / job_id / "state.json").write_text(
                    json.dumps({"job_id": job_id, "output_dir": str(output)}), encoding="utf-8"
                )
            self.assertEqual(jobs.find_by_output(root / "out-b", root), "b" * 32)
            self.assertIsNone(jobs.find_by_output(root / "out-c", root))


class DispatchRootTests(unittest.TestCase):
    def test_dispatch_root_honors_environment_and_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "nested" / "dispatches"
            old = os.environ.get("AGENT_RELAY_DISPATCH_DIR")
            os.environ["AGENT_RELAY_DISPATCH_DIR"] = str(target)
            try:
                root = dispatch.dispatch_root()
                self.assertEqual(root, target.resolve())
                self.assertEqual(root.stat().st_mode & 0o777, 0o700)
                link = Path(temp) / "link"
                link.symlink_to(target)
                os.environ["AGENT_RELAY_DISPATCH_DIR"] = str(link)
                with self.assertRaises(RelayError):
                    dispatch.dispatch_root()
            finally:
                if old is None:
                    os.environ.pop("AGENT_RELAY_DISPATCH_DIR", None)
                else:
                    os.environ["AGENT_RELAY_DISPATCH_DIR"] = old


class DispatchCliTests(DispatchTestCase):
    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "AGENT_RELAY_DISPATCH_DIR": str(self.directory / "dispatches"),
            "AGENT_RELAY_JOBS_DIR": str(self.directory / "jobs"),
        }
        return subprocess.run([sys.executable, str(RELAY), *args], capture_output=True, text=True, env=env, timeout=60)

    def test_dispatch_collect_cleanup_with_unavailable_provider(self) -> None:
        registry = self.directory / "registry"
        registry.mkdir()
        subprocess.run(["git", "init"], cwd=self.project, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "--allow-empty", "-m", "init"],
            cwd=self.project,
            check=True,
            capture_output=True,
        )
        adapter = {
            "name": "ghost-relay-test",
            "executable": "agent-relay-definitely-missing-cli",
            "default_model": "m",
            "args": [],
            "input": "stdin",
            "output": "text",
            "env": {},
            "models_args": [],
            "probe_args": [],
        }
        (registry / "ghost-relay-test.json").write_text(json.dumps(adapter), encoding="utf-8")
        manifest = self.manifest([self.task("a", provider="ghost-relay-test"), self.task("b", kind="read")])
        created = self.cli(
            "dispatch",
            "--manifest",
            str(manifest),
            "--root",
            str(self.project),
            "--providers",
            "ghost-relay-test",
            "--registry-dir",
            str(registry),
        )
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        ledger = json.loads(created.stdout)
        self.assertEqual([task["state"] for task in ledger["tasks"]], ["needs_native_fallback"] * 2)
        self.assertEqual(ledger["doctor"]["ghost-relay-test"]["ok"], False)
        path = self.directory / "dispatches" / ledger["dispatch_id"] / "ledger.json"
        self.assertTrue(path.is_file())
        collected = self.cli("collect", "--dispatch", str(path), "--timeout", "0")
        self.assertEqual(collected.returncode, 1, collected.stdout + collected.stderr)
        aggregate = json.loads(collected.stdout)
        self.assertEqual([item["id"] for item in aggregate["needs_native_fallback"]], ["a", "b"])
        cleaned = self.cli("cleanup", "--dispatch", str(path))
        self.assertEqual(cleaned.returncode, 0, cleaned.stdout + cleaned.stderr)
        self.assertEqual(json.loads(cleaned.stdout)["removed"], [])


class DoctorToolsTests(unittest.TestCase):
    def test_doctor_reports_whether_each_bundled_adapter_has_a_tools_mode(self) -> None:
        from relay import inspect_provider

        for name in ("antigravity", "cursor", "opencode"):
            with self.subTest(provider=name):
                self.assertIs(inspect_provider(name, None, None, False)["tools"], True)


if __name__ == "__main__":
    unittest.main()
