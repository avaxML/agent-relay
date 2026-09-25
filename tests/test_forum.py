from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RELAY = ROOT / "scripts" / "relay.py"
sys.path.insert(0, str(ROOT / "scripts"))

import forum
from providers import RelayError


class ForumTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agent-relay-forum-test-")
        self.directory = Path(self.temp.name)
        self.project = self.directory / "project"
        self.project.mkdir()
        self.source = self.project / "source.txt"
        self.source.write_text("cache uses files\n", encoding="utf-8")
        self.question = self.directory / "question.txt"
        self.question.write_text("How should we cache GET /items?", encoding="utf-8")
        self.forums = self.directory / "forums"
        self.jobs = self.directory / "jobs"
        self.worker = self.directory / "fake_worker.py"
        self.worker.write_text(
            textwrap.dedent(
                """
                import json
                import os
                import sys

                request = json.load(sys.stdin)
                ballots = json.loads(os.environ.get("FAKE_BALLOTS", "[]"))
                print(json.dumps({
                    "claim_id": os.environ.get("FAKE_CLAIM", "default"),
                    "position": os.environ.get("FAKE_POSITION", "unspecified"),
                    "evidence": [{"path": "source.txt", "lines": "1"}],
                    "ballots": ballots,
                    "task_preview": request["task"][:80],
                }))
                """
            ),
            encoding="utf-8",
        )
        self.active_jobs: list[str] = []

    def tearDown(self) -> None:
        for job_id in self.active_jobs:
            subprocess.run(
                [sys.executable, str(RELAY), "cancel", job_id, "--jobs-dir", str(self.jobs)],
                capture_output=True,
                check=False,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(RELAY),
                    "wait",
                    job_id,
                    "--jobs-dir",
                    str(self.jobs),
                    "--timeout",
                    "3",
                ],
                capture_output=True,
                check=False,
            )
        self.temp.cleanup()

    def _adapter(self, name: str, claim: str, position: str, ballots: list[dict[str, str]] | None = None) -> Path:
        path = self.directory / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    "name": name,
                    "executable": sys.executable,
                    "default_model": "fake-model",
                    "args": [str(self.worker)],
                    "input": "stdin",
                    "output": "text",
                    "env": {
                        "FAKE_CLAIM": claim,
                        "FAKE_POSITION": position,
                        "FAKE_BALLOTS": json.dumps(ballots or []),
                    },
                    "models_args": [],
                    "probe_args": [],
                }
            ),
            encoding="utf-8",
        )
        return path

    def _cli(self, *args: str, timeout: float = 12) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(RELAY), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"CLI returned non-JSON output: {completed.stdout!r}; stderr={completed.stderr!r}: {exc}")
        return completed.returncode, payload

    def _open(self, left: Path, right: Path, kind: str = "plan") -> str:
        code, payload = self._cli(
            "forum",
            "open",
            "--kind",
            kind,
            "--question-file",
            str(self.question),
            "--root",
            str(self.project),
            "--files",
            "source.txt",
            "--member",
            f"member_id=alpha,provider=alpha,adapter_file={left}",
            "--member",
            f"member_id=beta,provider=beta,adapter_file={right}",
            "--forums-dir",
            str(self.forums),
            "--jobs-dir",
            str(self.jobs),
            "--timeout",
            "8",
            "--max-rounds",
            "2",
        )
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "open")
        self.assertEqual(payload["unread"]["alpha"], 1)
        self.assertEqual(payload["unread"]["beta"], 1)
        return payload["topic_id"]

    def _track(self, payload: dict) -> None:
        for item in payload.get("jobs", []):
            if job_id := item.get("job_id"):
                self.active_jobs.append(job_id)

    def test_wal_database_serializes_concurrent_posts_without_lost_writes(self) -> None:
        left = self._adapter("alpha", "etag", "Use ETag")
        right = self._adapter("beta", "redis", "Use Redis")
        topic_id = self._open(left, right)
        errors: list[BaseException] = []

        def publish(label: str) -> None:
            try:
                note = self.directory / f"{label}.json"
                note.write_text(json.dumps({"text": label}), encoding="utf-8")
                code, payload = self._cli(
                    "forum",
                    "post",
                    topic_id,
                    "--to",
                    "alpha",
                    "--body-file",
                    str(note),
                    "--forums-dir",
                    str(self.forums),
                )
                if code != 0:
                    raise AssertionError(payload)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=publish, args=(f"n{i}",)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        code, inbox = self._cli("forum", "inbox", topic_id, "--member", "alpha", "--forums-dir", str(self.forums))
        self.assertEqual(code, 0, inbox)
        notes = [message["body"].get("text") for message in inbox["messages"] if message["kind"] == "note"]
        self.assertEqual(sorted(notes), [f"n{i}" for i in range(8)])
        db = sqlite3.connect(self.forums / "forum.sqlite")
        self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        db.close()

    def test_peek_does_not_consume_and_broadcast_fans_out(self) -> None:
        left = self._adapter("alpha", "etag", "Use ETag")
        right = self._adapter("beta", "redis", "Use Redis")
        topic_id = self._open(left, right)
        body = self.directory / "note.json"
        body.write_text(json.dumps({"text": "chair note"}), encoding="utf-8")
        code, posted = self._cli(
            "forum",
            "post",
            topic_id,
            "--broadcast",
            "--body-file",
            str(body),
            "--forums-dir",
            str(self.forums),
        )
        self.assertEqual(code, 0, posted)
        self.assertEqual(sorted(posted["recipients"]), ["alpha", "beta"])
        for member in ("alpha", "beta"):
            code, inbox = self._cli("forum", "inbox", topic_id, "--member", member, "--forums-dir", str(self.forums))
            self.assertEqual(code, 0, inbox)
            self.assertGreaterEqual(len(inbox["messages"]), 2)
        code, again = self._cli("forum", "inbox", topic_id, "--member", "alpha", "--forums-dir", str(self.forums))
        self.assertEqual(code, 0, again)
        self.assertGreaterEqual(len(again["messages"]), 2)

    def test_chair_can_post_a_second_task_before_first_round(self) -> None:
        topic_id = self._open(
            self._adapter("alpha", "etag", "Use ETag"),
            self._adapter("beta", "redis", "Use Redis"),
        )
        body = self.directory / "task.json"
        body.write_text(json.dumps({"question": "Compare cache invalidation"}), encoding="utf-8")

        code, posted = self._cli(
            "forum",
            "post",
            topic_id,
            "--kind",
            "task",
            "--to",
            "alpha",
            "--body-file",
            str(body),
            "--forums-dir",
            str(self.forums),
        )

        self.assertEqual(code, 0, posted)
        code, inbox = self._cli("forum", "inbox", topic_id, "--member", "alpha", "--forums-dir", str(self.forums))
        self.assertEqual(code, 0, inbox)
        tasks = [message["body"]["question"] for message in inbox["messages"] if message["kind"] == "task"]
        self.assertEqual(tasks, ["How should we cache GET /items?", "Compare cache invalidation"])

    def test_first_round_ballots_do_not_create_unreviewed_agreement(self) -> None:
        vote = [{"on": "etag", "ballot": "agree"}]
        topic_id = self._open(
            self._adapter("alpha", "etag", "Use ETag", vote),
            self._adapter("beta", "etag", "Use ETag", vote),
        )
        code, launched = self._cli("forum", "round", topic_id, "--forums-dir", str(self.forums))
        self.assertEqual(code, 0, launched)
        self._track(launched)
        self.assertEqual(self._cli("forum", "wait", topic_id, "--timeout", "8", "--forums-dir", str(self.forums))[0], 0)
        self.assertEqual(self._cli("forum", "ingest", topic_id, "--forums-dir", str(self.forums))[0], 0)

        code, settled = self._cli("forum", "settle", topic_id, "--forums-dir", str(self.forums))

        self.assertEqual(code, 0, settled)
        self.assertEqual(settled["status"], "split")
        self.assertEqual(settled["tally"]["counts"]["agree"], 0)

    def test_forum_uses_the_source_revision_present_when_it_opened(self) -> None:
        topic_id = self._open(
            self._adapter("alpha", "etag", "Use ETag"),
            self._adapter("beta", "redis", "Use Redis"),
        )
        self.source.write_text("cache now uses redis\n", encoding="utf-8")

        launched = forum.start_round(topic_id, self.forums)
        self._track(launched)
        for item in launched["jobs"]:
            task = json.loads((self.jobs / item["job_id"] / "task.json").read_text(encoding="utf-8"))
            request = json.loads(task["request"])
            self.assertEqual(request["sources"][0]["numbered_content"], "1: cache uses files")

    def test_second_round_rejects_ballots_for_claims_outside_its_candidate_set(self) -> None:
        left = self._adapter("alpha", "etag", "Use ETag")
        right = self._adapter("beta", "redis", "Use Redis")
        topic_id = self._open(left, right)
        first = forum.start_round(topic_id, self.forums)
        self._track(first)
        forum.wait_round(topic_id, self.forums, 8)
        forum.ingest_round(topic_id, self.forums)
        for adapter_path in (left, right):
            adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
            adapter["env"]["FAKE_BALLOTS"] = json.dumps([{"on": "invented", "ballot": "agree"}])
            adapter_path.write_text(json.dumps(adapter), encoding="utf-8")

        second = forum.start_round(topic_id, self.forums)
        self._track(second)
        forum.wait_round(topic_id, self.forums, 8)
        ingested = forum.ingest_round(topic_id, self.forums)

        self.assertEqual(
            [(item["member_id"], item["status"]) for item in ingested["failures"]],
            [("alpha", "malformed"), ("beta", "malformed")],
        )

    def test_second_round_is_rejected_while_jobs_are_pending(self) -> None:
        left = self._adapter("alpha", "etag", "Use ETag")
        right = self._adapter("beta", "redis", "Use Redis")
        topic_id = self._open(left, right)
        barrier = threading.Barrier(2)
        results: list[tuple[int, dict]] = []

        def launch() -> None:
            barrier.wait(timeout=3)
            results.append(self._cli("forum", "round", topic_id, "--forums-dir", str(self.forums), timeout=15))

        threads = [threading.Thread(target=launch) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        codes = sorted(code for code, _payload in results)
        self.assertEqual(codes, [0, 1])
        winner = next(payload for code, payload in results if code == 0)
        self._track(winner)
        loser = next(payload for code, payload in results if code == 1)
        self.assertIn("round_pending", loser["error"])
        wait_code, waited = self._cli("forum", "wait", topic_id, "--timeout", "8", "--forums-dir", str(self.forums))
        self.assertEqual(wait_code, 0, waited)

    def test_close_cannot_pass_a_round_that_started_before_its_write_lock(self) -> None:
        topic_id = self._open(
            self._adapter("alpha", "etag", "Use ETag"),
            self._adapter("beta", "redis", "Use Redis"),
        )
        paused = threading.Event()
        resume = threading.Event()
        close_result: list[str] = []
        original_immediate = forum.immediate

        def delayed_immediate(connection):  # type: ignore[no-untyped-def]
            if threading.current_thread().name == "closer":
                paused.set()
                self.assertTrue(resume.wait(5))
            return original_immediate(connection)

        def close() -> None:
            try:
                forum.close_topic(topic_id, self.forums)
                close_result.append("closed")
            except RelayError as exc:
                close_result.append(str(exc))

        with patch("forum.immediate", side_effect=delayed_immediate):
            thread = threading.Thread(target=close, name="closer")
            thread.start()
            self.assertTrue(paused.wait(5))
            launched = forum.start_round(topic_id, self.forums)
            self._track(launched)
            resume.set()
            thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(forum.topic_status(topic_id, self.forums)["status"], "round_pending")
        self.assertEqual(len(close_result), 1)
        self.assertIn("pending", close_result[0])

    def test_abandon_owns_the_round_before_cancelling_jobs(self) -> None:
        topic_id = self._open(
            self._adapter("alpha", "etag", "Use ETag"),
            self._adapter("beta", "redis", "Use Redis"),
        )
        launched = forum.start_round(topic_id, self.forums)
        self._track(launched)
        forum.wait_round(topic_id, self.forums, 8)
        paused = threading.Event()
        resume = threading.Event()
        original_cancel = forum.jobs.cancel
        abandon_result: list[dict] = []

        def delayed_cancel(job_id, jobs_dir):  # type: ignore[no-untyped-def]
            paused.set()
            self.assertTrue(resume.wait(5))
            return original_cancel(job_id, jobs_dir)

        def abandon() -> None:
            abandon_result.append(forum.abandon_round(topic_id, self.forums))

        with patch("forum.jobs.cancel", side_effect=delayed_cancel):
            thread = threading.Thread(target=abandon)
            thread.start()
            self.assertTrue(paused.wait(5))
            ingest_error: RelayError | None = None
            try:
                forum.ingest_round(topic_id, self.forums)
            except RelayError as exc:
                ingest_error = exc
            finally:
                resume.set()
            thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(ingest_error)
        self.assertEqual(abandon_result[0]["status"], "open")
        with forum.database(self.forums) as connection:
            count = connection.execute("SELECT COUNT(*) FROM claims WHERE topic_id=?", (topic_id,)).fetchone()[0]
        self.assertEqual(count, 0)

    def test_round_ingest_broadcasts_claims_and_settle_can_override(self) -> None:
        left = self._adapter("alpha", "etag", "Use ETag")
        right = self._adapter("beta", "redis", "Use Redis")
        topic_id = self._open(left, right)
        code, launched = self._cli("forum", "round", topic_id, "--forums-dir", str(self.forums))
        self.assertEqual(code, 0, launched)
        self._track(launched)
        code, waited = self._cli("forum", "wait", topic_id, "--timeout", "8", "--forums-dir", str(self.forums))
        self.assertEqual(code, 0, waited)
        code, ingested = self._cli("forum", "ingest", topic_id, "--forums-dir", str(self.forums))
        self.assertEqual(code, 0, ingested)
        self.assertEqual({item["claim_id"] for item in ingested["ingested"]}, {"etag", "redis"})
        code, alpha_inbox = self._cli("forum", "inbox", topic_id, "--member", "alpha", "--forums-dir", str(self.forums))
        self.assertEqual(code, 0, alpha_inbox)
        senders = {message["sender"] for message in alpha_inbox["messages"]}
        self.assertIn("beta", senders)
        self.assertNotIn("alpha", senders)
        code, settled = self._cli(
            "forum",
            "settle",
            topic_id,
            "--claim-id",
            "etag",
            "--forums-dir",
            str(self.forums),
        )
        self.assertEqual(code, 0, settled)
        self.assertEqual(settled["status"], "agreed")
        self.assertTrue(settled["chair_override"])
        self.assertTrue(Path(settled["exported_path"]).is_file())
        consensus = json.loads(Path(settled["exported_path"]).read_text(encoding="utf-8"))
        self.assertEqual(consensus["position"], "Use ETag")

    def test_second_round_ballots_can_reach_unanimous_consensus(self) -> None:
        left = self._adapter("alpha", "etag", "Use ETag")
        right = self._adapter("beta", "redis", "Use Redis")
        topic_id = self._open(left, right)
        code, launched = self._cli("forum", "round", topic_id, "--forums-dir", str(self.forums))
        self.assertEqual(code, 0, launched)
        self._track(launched)
        self.assertEqual(self._cli("forum", "wait", topic_id, "--timeout", "8", "--forums-dir", str(self.forums))[0], 0)
        self.assertEqual(self._cli("forum", "ingest", topic_id, "--forums-dir", str(self.forums))[0], 0)
        left.write_text(
            json.dumps(
                {
                    **json.loads(left.read_text(encoding="utf-8")),
                    "env": {
                        "FAKE_CLAIM": "etag",
                        "FAKE_POSITION": "Use ETag",
                        "FAKE_BALLOTS": json.dumps([{"on": "etag", "ballot": "agree"}]),
                    },
                }
            ),
            encoding="utf-8",
        )
        right.write_text(
            json.dumps(
                {
                    **json.loads(right.read_text(encoding="utf-8")),
                    "env": {
                        "FAKE_CLAIM": "etag",
                        "FAKE_POSITION": "Use ETag",
                        "FAKE_BALLOTS": json.dumps([{"on": "etag", "ballot": "agree"}]),
                    },
                }
            ),
            encoding="utf-8",
        )
        code, second = self._cli("forum", "round", topic_id, "--forums-dir", str(self.forums))
        self.assertEqual(code, 0, second)
        self._track(second)
        self.assertEqual(self._cli("forum", "wait", topic_id, "--timeout", "8", "--forums-dir", str(self.forums))[0], 0)
        self.assertEqual(self._cli("forum", "ingest", topic_id, "--forums-dir", str(self.forums))[0], 0)
        code, settled = self._cli("forum", "settle", topic_id, "--forums-dir", str(self.forums))
        self.assertEqual(code, 0, settled)
        self.assertEqual(settled["status"], "agreed")
        self.assertFalse(settled["chair_override"])
        self.assertEqual(settled["tally"]["counts"]["agree"], 2)

    def test_second_round_tasks_contain_the_same_complete_candidate_set(self) -> None:
        topic_id = self._open(
            self._adapter("alpha", "etag", "Use ETag"),
            self._adapter("beta", "redis", "Use Redis"),
        )
        first = forum.start_round(topic_id, self.forums)
        self._track(first)
        forum.wait_round(topic_id, self.forums, 8)
        forum.ingest_round(topic_id, self.forums)

        second = forum.start_round(topic_id, self.forums)
        self._track(second)
        for member in ("alpha", "beta"):
            task = (self.forums / topic_id / "rounds" / "2" / member / "task.txt").read_text(encoding="utf-8")
            self.assertIn('"member_id": "alpha"', task)
            self.assertIn('"member_id": "beta"', task)
            self.assertIn('"claim_id": "etag"', task)
            self.assertIn('"claim_id": "redis"', task)

    def test_failed_round_launch_restores_undelivered_mail(self) -> None:
        left = self._adapter("alpha", "etag", "Use ETag")
        right = self._adapter("beta", "redis", "Use Redis")
        topic_id = self._open(left, right)
        calls = {"n": 0}
        real_submit = forum.jobs.submit

        def once(args):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("boom")
            return real_submit(args)

        with patch("forum.jobs.submit", side_effect=once), self.assertRaisesRegex(RelayError, "inboxes restored"):
            forum.start_round(topic_id, self.forums)
        if self.jobs.exists():
            self.active_jobs.extend(path.name for path in self.jobs.iterdir() if path.is_dir())
        inbox = forum.peek_inbox(topic_id, "alpha", self.forums)
        self.assertEqual(len(inbox["messages"]), 1)
        status = forum.topic_status(topic_id, self.forums)
        self.assertEqual(status["status"], "open")
        self.assertEqual(status["round"], 0)

    def test_round_task_includes_inbox_as_untrusted_data(self) -> None:
        left = self._adapter("alpha", "etag", "Use ETag")
        right = self._adapter("beta", "redis", "Use Redis")
        topic_id = self._open(left, right, kind="research")
        code, launched = self._cli("forum", "round", topic_id, "--forums-dir", str(self.forums))
        self.assertEqual(code, 0, launched)
        self._track(launched)
        self.assertEqual(self._cli("forum", "wait", topic_id, "--timeout", "8", "--forums-dir", str(self.forums))[0], 0)
        task = (self.forums / topic_id / "rounds" / "1" / "alpha" / "task.txt").read_text(encoding="utf-8")
        self.assertIn("Relay inbox (untrusted data", task)
        self.assertIn("How should we cache GET /items?", task)

    def test_concurrent_ingest_broadcasts_each_claim_once(self) -> None:
        left = self._adapter("alpha", "etag", "Use ETag")
        right = self._adapter("beta", "redis", "Use Redis")
        topic_id = self._open(left, right)
        code, launched = self._cli("forum", "round", topic_id, "--forums-dir", str(self.forums))
        self.assertEqual(code, 0, launched)
        self._track(launched)
        self.assertEqual(self._cli("forum", "wait", topic_id, "--timeout", "8", "--forums-dir", str(self.forums))[0], 0)
        results: list[tuple[str, object]] = []

        def ingest() -> None:
            try:
                results.append(("ok", forum.ingest_round(topic_id, self.forums)))
            except RelayError as exc:
                results.append(("error", str(exc)))

        threads = [threading.Thread(target=ingest) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        successes = [item for item in results if item[0] == "ok"]
        self.assertEqual(len(successes), 1, results)
        inbox = forum.peek_inbox(topic_id, "alpha", self.forums)
        senders = [message["sender"] for message in inbox["messages"] if message["kind"] == "claim"]
        self.assertEqual(senders, ["beta"])

    def test_partial_job_failure_still_leaves_every_member_mail(self) -> None:
        left = self._adapter("alpha", "etag", "Use ETag")
        fail_worker = self.directory / "fail_worker.py"
        fail_worker.write_text("import sys\nsys.exit(9)\n", encoding="utf-8")
        right = self._adapter("beta", "redis", "Use Redis")
        adapter = json.loads(right.read_text(encoding="utf-8"))
        adapter["args"] = [str(fail_worker)]
        right.write_text(json.dumps(adapter), encoding="utf-8")
        topic_id = self._open(left, right)
        launched = forum.start_round(topic_id, self.forums)
        self._track(launched)
        forum.wait_round(topic_id, self.forums, 8)
        ingested = forum.ingest_round(topic_id, self.forums)
        self.assertEqual(len(ingested["failures"]), 1)
        status = forum.topic_status(topic_id, self.forums)
        self.assertGreaterEqual(status["unread"]["alpha"], 1)
        self.assertGreaterEqual(status["unread"]["beta"], 1)
        beta_inbox = forum.peek_inbox(topic_id, "beta", self.forums)
        self.assertEqual(
            [message["body"]["question"] for message in beta_inbox["messages"] if message["kind"] == "task"],
            ["How should we cache GET /items?"],
        )
        second = forum.start_round(topic_id, self.forums)
        self._track(second)
        self.assertEqual(second["round"], 2)

    def test_post_rejects_forged_member_claims(self) -> None:
        left = self._adapter("alpha", "etag", "Use ETag")
        right = self._adapter("beta", "redis", "Use Redis")
        topic_id = self._open(left, right)
        with self.assertRaisesRegex(RelayError, "claims come from ingest"):
            forum.post_message(
                topic_id,
                sender="beta",
                kind="claim",
                body={"claim_id": "forged", "position": "nope"},
                forums_dir=self.forums,
                recipient="alpha",
            )
        with self.assertRaisesRegex(RelayError, "Only the chair can post"):
            forum.post_message(
                topic_id,
                sender="beta",
                kind="note",
                body={"text": "nope"},
                forums_dir=self.forums,
                broadcast=True,
            )

    def test_split_consensus_stays_open_for_chair_override(self) -> None:
        left = self._adapter("alpha", "etag", "Use ETag")
        right = self._adapter("beta", "redis", "Use Redis")
        topic_id = self._open(left, right)
        launched = forum.start_round(topic_id, self.forums)
        self._track(launched)
        forum.wait_round(topic_id, self.forums, 8)
        forum.ingest_round(topic_id, self.forums)
        split = forum.settle_topic(topic_id, self.forums)
        self.assertEqual(split["status"], "split")
        self.assertEqual(forum.topic_status(topic_id, self.forums)["status"], "open")
        agreed = forum.settle_topic(topic_id, self.forums, claim_id="etag")
        self.assertEqual(agreed["status"], "agreed")
        self.assertEqual(forum.topic_status(topic_id, self.forums)["status"], "settled")

    def test_collided_claim_ids_cannot_reach_unanimous_consensus(self) -> None:
        left = self._adapter(
            "alpha",
            "etag",
            "Use ETag",
            [{"on": "etag", "ballot": "agree"}],
        )
        right = self._adapter(
            "beta",
            "etag",
            "Use Redis instead",
            [{"on": "etag", "ballot": "agree"}],
        )
        topic_id = self._open(left, right)
        launched = forum.start_round(topic_id, self.forums)
        self._track(launched)
        forum.wait_round(topic_id, self.forums, 8)
        forum.ingest_round(topic_id, self.forums)
        split = forum.settle_topic(topic_id, self.forums)
        self.assertEqual(split["status"], "split")
        self.assertEqual(forum.topic_status(topic_id, self.forums)["status"], "open")
        with self.assertRaisesRegex(RelayError, "Ambiguous claim_id: etag"):
            forum.settle_topic(topic_id, self.forums, claim_id="etag")
        self._adapter(
            "beta",
            "etag",
            "Use ETag",
            [{"on": "etag", "ballot": "agree"}],
        )
        second = forum.start_round(topic_id, self.forums)
        self._track(second)
        forum.wait_round(topic_id, self.forums, 8)
        forum.ingest_round(topic_id, self.forums)
        agreed = forum.settle_topic(topic_id, self.forums)
        self.assertEqual(agreed["status"], "agreed")
        self.assertEqual(agreed["position"], "Use ETag")
        self.assertFalse(agreed["chair_override"])

    def test_rejected_settle_does_not_write_consensus_file(self) -> None:
        left = self._adapter("alpha", "etag", "Use ETag")
        right = self._adapter("beta", "redis", "Use Redis")
        topic_id = self._open(left, right)
        launched = forum.start_round(topic_id, self.forums)
        self._track(launched)
        forum.wait_round(topic_id, self.forums, 8)
        forum.ingest_round(topic_id, self.forums)
        export = self.forums / topic_id / "consensus.json"
        original = forum._select_claim

        def poison(*args: object, **kwargs: object) -> tuple[dict, bool]:
            chosen = original(*args, **kwargs)
            with forum.database(self.forums) as connection, forum.immediate(connection):
                connection.execute(
                    "UPDATE topics SET status='round_pending', updated_at=? WHERE topic_id=?",
                    (time.time(), topic_id),
                )
            return chosen

        with (
            patch.object(forum, "_select_claim", side_effect=poison),
            self.assertRaisesRegex(RelayError, "cannot be settled"),
        ):
            forum.settle_topic(topic_id, self.forums)
        self.assertFalse(export.exists())
        with forum.database(self.forums) as connection:
            row = connection.execute("SELECT * FROM consensus WHERE topic_id=?", (topic_id,)).fetchone()
        self.assertIsNone(row)

    def test_concurrent_settles_keep_consensus_file_aligned_with_db(self) -> None:
        left = self._adapter("alpha", "etag", "Use ETag")
        right = self._adapter("beta", "redis", "Use Redis")
        topic_id = self._open(left, right)
        launched = forum.start_round(topic_id, self.forums)
        self._track(launched)
        forum.wait_round(topic_id, self.forums, 8)
        forum.ingest_round(topic_id, self.forums)
        real_write = forum.write_json

        def slow_write(path: Path, payload: object) -> None:
            time.sleep(0.05)
            real_write(path, payload)

        def attempt(claim_id: str | None) -> None:
            try:
                forum.settle_topic(topic_id, self.forums, claim_id=claim_id)
            except RelayError:
                return

        with patch.object(forum, "write_json", side_effect=slow_write):
            threads = [
                threading.Thread(target=attempt, args=(None,)),
                threading.Thread(target=attempt, args=("etag",)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        export = self.forums / topic_id / "consensus.json"
        with forum.database(self.forums) as connection:
            row = connection.execute("SELECT payload FROM consensus WHERE topic_id=?", (topic_id,)).fetchone()
        self.assertIsNotNone(row)
        db_payload = json.loads(row["payload"])
        file_payload = json.loads(export.read_text(encoding="utf-8"))
        self.assertEqual(file_payload["status"], db_payload["status"])
        self.assertEqual(file_payload["claim_id"], db_payload["claim_id"])
        self.assertEqual(file_payload["chair_override"], db_payload["chair_override"])
        status = forum.topic_status(topic_id, self.forums)["status"]
        if status == "settled":
            self.assertEqual(file_payload["status"], "agreed")
        else:
            self.assertEqual(status, "open")
            self.assertEqual(file_payload["status"], "split")

    def test_parse_answer_rejects_concatenated_json_objects(self) -> None:
        text = '{"claim_id":"first","position":"A"}\n{"claim_id":"second","position":"B"}'
        self.assertIsNone(forum.parse_answer(text, "alpha", 1))
        fenced = '```json\n{"claim_id":"etag","position":"Use ETag","evidence":[]}\n```'
        parsed = forum.parse_answer(fenced, "alpha", 1)
        assert parsed is not None
        self.assertEqual(parsed["claim_id"], "etag")

    def test_parse_answer_rejects_json_arrays(self) -> None:
        stolen = '[{"claim_id":"stolen","position":"Use ETag","evidence":[]}]'
        self.assertIsNone(forum.parse_answer(stolen, "alpha", 1))
        self.assertIsNone(forum.parse_answer("[]", "alpha", 1))
        padded = forum.parse_answer('  {"claim_id":"etag","position":"  Use ETag  "}  ', "alpha", 1)
        assert padded is not None
        self.assertEqual(padded["position"], "Use ETag")
        bom = "\ufeff" + '{"claim_id":"etag","position":"Use ETag","evidence":[]}'
        parsed = forum.parse_answer(bom, "alpha", 1)
        assert parsed is not None
        self.assertEqual(parsed["claim_id"], "etag")

    def test_concatenated_json_answers_are_malformed_failures(self) -> None:
        worker = self.directory / "two_json.py"
        worker.write_text(
            textwrap.dedent(
                """
                import json, sys
                json.load(sys.stdin)
                print(json.dumps({"claim_id": "first", "position": "A"}))
                print(json.dumps({"claim_id": "second", "position": "B"}))
                """
            ),
            encoding="utf-8",
        )
        left = self._adapter("alpha", "etag", "Use ETag")
        right = self._adapter("beta", "redis", "Use Redis")
        for path in (left, right):
            adapter = json.loads(path.read_text(encoding="utf-8"))
            adapter["args"] = [str(worker)]
            path.write_text(json.dumps(adapter), encoding="utf-8")
        topic_id = self._open(left, right)
        launched = forum.start_round(topic_id, self.forums)
        self._track(launched)
        forum.wait_round(topic_id, self.forums, 8)
        ingested = forum.ingest_round(topic_id, self.forums)
        self.assertEqual({item["status"] for item in ingested["failures"]}, {forum.JOB_MALFORMED})
        self.assertEqual(ingested["ingested"], [])
        inbox = forum.peek_inbox(topic_id, "alpha", self.forums)
        self.assertFalse(any(message["kind"] == "claim" for message in inbox["messages"]))

    def test_deleted_jobs_dir_abandons_round_and_restores_mail(self) -> None:
        left = self._adapter("alpha", "etag", "Use ETag")
        right = self._adapter("beta", "redis", "Use Redis")
        topic_id = self._open(left, right)
        launched = forum.start_round(topic_id, self.forums)
        self._track(launched)
        forum.wait_round(topic_id, self.forums, 8)
        shutil.rmtree(self.jobs)
        waited = forum.wait_round(topic_id, self.forums, 0)
        self.assertTrue(all(item["status"] == forum.JOB_MISSING for item in waited["jobs"]))
        ingested = forum.ingest_round(topic_id, self.forums)
        self.assertTrue(ingested["abandoned"])
        status = forum.topic_status(topic_id, self.forums)
        self.assertEqual(status["status"], "open")
        self.assertEqual(status["round"], 0)
        self.assertEqual(status["unread"]["alpha"], 1)
        self.assertEqual(status["unread"]["beta"], 1)
        retry = forum.start_round(topic_id, self.forums)
        self._track(retry)
        self.assertEqual(retry["round"], 1)

    def test_oversized_claim_is_malformed_and_does_not_wedge_round(self) -> None:
        left = self._adapter("alpha", "etag", "Use ETag")
        right = self._adapter("beta", "redis", "Use Redis")
        topic_id = self._open(left, right)
        launched = forum.start_round(topic_id, self.forums)
        self._track(launched)
        forum.wait_round(topic_id, self.forums, 8)
        alpha_job = next(item["job_id"] for item in launched["jobs"] if item["member_id"] == "alpha")
        path = Path(forum.jobs.result(alpha_job, self.jobs)["output_dir"]) / "result.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["answer"] = json.dumps({"claim_id": "big", "position": "X" * 210000, "evidence": []})
        path.write_text(json.dumps(payload), encoding="utf-8")
        ingested = forum.ingest_round(topic_id, self.forums)
        self.assertEqual({item["member_id"] for item in ingested["ingested"]}, {"beta"})
        self.assertEqual({item["status"] for item in ingested["failures"]}, {forum.JOB_MALFORMED})
        self.assertEqual(forum.topic_status(topic_id, self.forums)["status"], "open")
        second = forum.start_round(topic_id, self.forums)
        self._track(second)
        self.assertEqual(second["round"], 2)

    def test_malformed_result_json_does_not_crash_ingest(self) -> None:
        left = self._adapter("alpha", "etag", "Use ETag")
        right = self._adapter("beta", "redis", "Use Redis")
        topic_id = self._open(left, right)
        launched = forum.start_round(topic_id, self.forums)
        self._track(launched)
        forum.wait_round(topic_id, self.forums, 8)
        jobs_by_member = {item["member_id"]: item["job_id"] for item in launched["jobs"]}
        alpha_path = Path(forum.jobs.result(jobs_by_member["alpha"], self.jobs)["output_dir"]) / "result.json"
        beta_path = Path(forum.jobs.result(jobs_by_member["beta"], self.jobs)["output_dir"]) / "result.json"
        alpha_path.write_text('{"status":"ok","answer":"', encoding="utf-8")
        beta_path.write_text("[1, 2, 3]\n", encoding="utf-8")
        ingested = forum.ingest_round(topic_id, self.forums)
        self.assertEqual(ingested["ingested"], [])
        self.assertEqual({item["status"] for item in ingested["failures"]}, {forum.JOB_MALFORMED})
        self.assertEqual(forum.topic_status(topic_id, self.forums)["status"], "open")

    def test_close_after_settle_updates_consensus_payload(self) -> None:
        left = self._adapter("alpha", "etag", "Use ETag")
        right = self._adapter("beta", "redis", "Use Redis")
        topic_id = self._open(left, right)
        launched = forum.start_round(topic_id, self.forums)
        self._track(launched)
        forum.wait_round(topic_id, self.forums, 8)
        forum.ingest_round(topic_id, self.forums)
        forum.settle_topic(topic_id, self.forums)
        closed = forum.close_topic(topic_id, self.forums)
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["consensus"]["status"], "closed")
        exported = json.loads((self.forums / topic_id / "consensus.json").read_text(encoding="utf-8"))
        self.assertEqual(exported["status"], "closed")
        copy = forum.export_consensus(topic_id, self.forums, self.directory / "closed.json")
        self.assertEqual(copy["status"], "closed")

    def test_open_topic_rejects_path_member_ids(self) -> None:
        left = self._adapter("alpha", "etag", "Use ETag")
        right = self._adapter("beta", "redis", "Use Redis")
        with self.assertRaisesRegex(RelayError, "Member IDs"):
            forum.open_topic(
                kind="plan",
                question="How should we cache GET /items?",
                root=self.project,
                files=["source.txt"],
                members=[
                    {
                        "member_id": "../evil",
                        "provider": "alpha",
                        "model": None,
                        "effort": None,
                        "adapter_file": str(left),
                        "registry_dir": None,
                    },
                    {
                        "member_id": "beta",
                        "provider": "beta",
                        "model": None,
                        "effort": None,
                        "adapter_file": str(right),
                        "registry_dir": None,
                    },
                ],
                forums_dir=self.forums,
                jobs_dir=self.jobs,
                max_rounds=2,
                threshold="unanimous",
                timeout=8,
                max_input_bytes=400000,
                max_answer_chars=12000,
            )


if __name__ == "__main__":
    unittest.main()
