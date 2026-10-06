"""Tests for repository isolation, clone lifecycle, git sandboxing, and capture."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from isolation import (
    capture,
    clones_root,
    create_clone,
    find_violations,
    git_environment,
    globs_overlap,
    owned,
    remove_clone,
    resolve_ref,
    snapshot_metadata,
    tool_environment,
    validate_globs,
)
from providers import RelayError


class IsolationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.clones_dir = self.temp_path / "clones"
        self.clones_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.patcher = patch.dict(os.environ, {"AGENT_RELAY_CLONES_DIR": str(self.clones_dir)})
        self.patcher.start()

        self.source_dir = self.temp_path / "source"
        self.source_dir.mkdir()
        self.logs_dir = self.temp_path / "logs"
        self.logs_dir.mkdir()

        self._git(["init"], cwd=self.source_dir)
        self.app_py = self.source_dir / "app.py"
        self.app_py.write_text("print('hello v1')\n", encoding="utf-8")
        self._git(["add", "app.py"], cwd=self.source_dir)
        self._git(
            ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "commit 1"],
            cwd=self.source_dir,
        )
        self.commit1 = self._git(["rev-parse", "HEAD"], cwd=self.source_dir).strip()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.temp_dir.cleanup()

    def _git(self, args: list[str], cwd: Path) -> str:
        cmd = ["git", *args]
        res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)
        return res.stdout

    def test_clone_is_separate_and_pinned(self) -> None:
        self.app_py.write_text("print('hello v2')\n", encoding="utf-8")
        self._git(["add", "app.py"], cwd=self.source_dir)
        self._git(
            ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "commit 2"],
            cwd=self.source_dir,
        )

        status_before = self._git(["status", "--porcelain"], cwd=self.source_dir)
        index_mtime_before = (self.source_dir / ".git" / "index").stat().st_mtime_ns
        app_mtime_before = self.app_py.stat().st_mtime_ns

        clone = create_clone(self.source_dir, self.commit1, self.logs_dir, timeout=10.0)

        work_app = clone.work / "app.py"
        self.assertEqual(work_app.read_text(encoding="utf-8"), "print('hello v1')\n")

        self.assertEqual((self.source_dir / ".git" / "index").stat().st_mtime_ns, index_mtime_before)
        self.assertEqual(self.app_py.stat().st_mtime_ns, app_mtime_before)
        self.assertEqual(self._git(["status", "--porcelain"], cwd=self.source_dir), status_before)

        self.assertEqual(stat.S_IMODE(clone.directory.stat().st_mode), 0o700)

        remotes = self._git(["remote"], cwd=clone.work)
        self.assertNotIn("origin", remotes.split())

    def test_unchanged_clone_reports_no_changes(self) -> None:
        (self.source_dir / "second.py").write_text("x = 1\n", encoding="utf-8")
        self._git(["add", "second.py"], cwd=self.source_dir)
        self._git(
            ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "second"],
            cwd=self.source_dir,
        )
        sha = self._git(["rev-parse", "HEAD"], cwd=self.source_dir).strip()
        clone = create_clone(self.source_dir, sha, self.logs_dir, timeout=10.0)
        result = capture(clone, snapshot_metadata(clone.work), self.logs_dir, timeout=10.0)
        self.assertEqual((result.tracked_changes, result.untracked_files, result.diff), ([], [], b""))

    def test_hooks_never_run_and_metadata_recorded(self) -> None:
        clone = create_clone(self.source_dir, self.commit1, self.logs_dir, timeout=10.0)
        before = snapshot_metadata(clone.work)

        marker = self.temp_path / "marker.txt"
        hook_script = f"#!/bin/sh\ntouch '{marker}'\n"

        hooks_dir = clone.work / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        post_checkout = hooks_dir / "post-checkout"
        post_checkout.write_text(hook_script, encoding="utf-8")
        post_checkout.chmod(post_checkout.stat().st_mode | stat.S_IXUSR)

        pre_commit = hooks_dir / "pre-commit"
        pre_commit.write_text(hook_script, encoding="utf-8")
        pre_commit.chmod(pre_commit.stat().st_mode | stat.S_IXUSR)

        fsmonitor_script = self.temp_path / "fsmonitor.sh"
        fsmonitor_script.write_text(hook_script, encoding="utf-8")
        fsmonitor_script.chmod(fsmonitor_script.stat().st_mode | stat.S_IXUSR)

        config_path = clone.work / ".git" / "config"
        with config_path.open("a", encoding="utf-8") as f:
            f.write(f"\n[core]\n\tfsmonitor = {fsmonitor_script.as_posix()}\n")

        result = capture(clone, before, self.logs_dir, timeout=10.0)

        self.assertFalse(marker.exists())
        self.assertIn(".git/config", result.metadata_changes)
        self.assertIn(".git/hooks/post-checkout", result.metadata_changes)
        self.assertIn(".git/hooks/pre-commit", result.metadata_changes)

        source_hooks = self.source_dir / ".git" / "hooks"
        self.assertFalse((source_hooks / "post-checkout").exists())
        self.assertFalse((source_hooks / "pre-commit").exists())

    def test_planted_hook_in_clone_does_not_appear_in_source_repo(self) -> None:
        clone = create_clone(self.source_dir, self.commit1, self.logs_dir, timeout=10.0)
        clone_hook = clone.work / ".git" / "hooks" / "pre-push"
        clone_hook.parent.mkdir(parents=True, exist_ok=True)
        clone_hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

        self.assertFalse((self.source_dir / ".git" / "hooks" / "pre-push").exists())

    def test_symlink_to_fake_home_reported_as_untracked_and_not_followed(self) -> None:
        fake_home = self.temp_path / "fake_home"
        fake_home.mkdir()
        secret_file = fake_home / "secret.txt"
        secret_file.write_text("TOPSECRET", encoding="utf-8")

        clone = create_clone(self.source_dir, self.commit1, self.logs_dir, timeout=10.0)
        before = snapshot_metadata(clone.work)

        link = clone.work / "home_link"
        link.symlink_to(fake_home)

        result = capture(clone, before, self.logs_dir, timeout=10.0)
        self.assertIn("home_link", result.untracked_files)
        self.assertNotIn(b"TOPSECRET", result.diff)

    def test_tracked_edit_and_new_file_and_violations(self) -> None:
        clone = create_clone(self.source_dir, self.commit1, self.logs_dir, timeout=10.0)
        before = snapshot_metadata(clone.work)

        (clone.work / "app.py").write_text("print('edited')\n", encoding="utf-8")
        (clone.work / "new.txt").write_text("new content\n", encoding="utf-8")

        result = capture(clone, before, self.logs_dir, timeout=10.0)
        self.assertEqual(result.tracked_changes, ["app.py"])
        self.assertEqual(result.untracked_files, ["new.txt"])
        self.assertIn(b"print('edited')", result.diff)
        self.assertIn(b"new content", result.diff)

        self.assertEqual(find_violations(result, None), ["app.py"])
        self.assertEqual(find_violations(result, ["app.py"]), ["new.txt"])
        self.assertEqual(find_violations(result, ["*"]), [])

    def test_deletion_outside_owns_and_rename_into_owns(self) -> None:
        clone = create_clone(self.source_dir, self.commit1, self.logs_dir, timeout=10.0)
        before = snapshot_metadata(clone.work)

        src_dir = clone.work / "src"
        src_dir.mkdir()
        os.rename(clone.work / "app.py", src_dir / "app.py")

        result = capture(clone, before, self.logs_dir, timeout=10.0)
        self.assertEqual(find_violations(result, ["src/**"]), ["app.py"])

    def test_capture_over_limit_raises_relay_error(self) -> None:
        clone = create_clone(self.source_dir, self.commit1, self.logs_dir, timeout=10.0)
        before = snapshot_metadata(clone.work)

        large_file = clone.work / "large.txt"
        large_file.write_bytes(b"x" * 2000)

        with self.assertRaisesRegex(RelayError, "exceeds"):
            capture(clone, before, self.logs_dir, timeout=10.0, max_bytes=1000)

    def test_remove_clone_and_refuse_outside_path(self) -> None:
        clone = create_clone(self.source_dir, self.commit1, self.logs_dir, timeout=10.0)
        self.assertTrue(clone.directory.exists())
        remove_clone(clone.directory)
        self.assertFalse(clone.directory.exists())
        remove_clone(clone.directory)

        outside = self.temp_path / "outside"
        outside.mkdir()
        with self.assertRaises(RelayError):
            remove_clone(outside)

    def test_resolve_ref_returns_sha_and_rejects_missing_ref(self) -> None:
        sha = resolve_ref(self.source_dir, "HEAD", self.logs_dir, timeout=10.0)
        self.assertEqual(sha, self.commit1)

        with self.assertRaisesRegex(RelayError, "--ref does not name a commit"):
            resolve_ref(self.source_dir, "no-such-ref", self.logs_dir, timeout=10.0)

    def test_tool_environment_drops_secrets_and_keeps_specified(self) -> None:
        base = {
            "GIT_DIR": "/custom/git",
            "SSH_AUTH_SOCK": "/tmp/ssh.sock",
            "GITHUB_TOKEN": "secret-token",
            "MY_API_KEY": "secret-key",
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/user",
        }
        filtered, removed = tool_environment(base)
        self.assertEqual(filtered, {"PATH": "/usr/bin:/bin", "HOME": "/home/user"})
        self.assertEqual(removed, ["GITHUB_TOKEN", "GIT_DIR", "MY_API_KEY", "SSH_AUTH_SOCK"])

        filtered_keep, removed_keep = tool_environment(base, keep=["GITHUB_TOKEN"])
        self.assertEqual(
            filtered_keep,
            {"PATH": "/usr/bin:/bin", "HOME": "/home/user", "GITHUB_TOKEN": "secret-token"},
        )
        self.assertEqual(removed_keep, ["GIT_DIR", "MY_API_KEY", "SSH_AUTH_SOCK"])

    def test_validate_globs_table(self) -> None:
        for invalid in ["/abs", "../x", "a/../b", ".git/config", "a//b", "", "back\\slash", "trailing/"]:
            with self.subTest(invalid=invalid), self.assertRaisesRegex(RelayError, "--owns"):
                validate_globs([invalid])

        for valid in ["src/**", "*.py", "app.py", "src/x/y.py"]:
            with self.subTest(valid=valid):
                self.assertEqual(validate_globs([valid]), [valid])

    def test_owned_table(self) -> None:
        cases = [
            ("src/a.py", ["src/**"], True),
            ("src/x/y.py", ["src/**"], True),
            ("src", ["src/**"], False),
            ("a.py", ["*.py"], True),
            ("d/a.py", ["*.py"], False),
            ("app.py", ["app.py"], True),
            ("other.py", ["app.py"], False),
        ]
        for path, patterns, expected in cases:
            with self.subTest(path=path, patterns=patterns):
                self.assertEqual(owned(path, patterns), expected)

    def test_globs_overlap_table(self) -> None:
        cases = [
            ("src/**", "src/a.py", True),
            ("src/a.py", "lib/**", False),
            ("*.py", "app.py", True),
            ("*.py", "app.txt", False),
            ("*.py", "*.txt", True),
            ("a/b", "a/b/c", False),
            ("a/b", "a/b", True),
            ("a/b", "a/c", False),
        ]
        for first, second, expected in cases:
            with self.subTest(first=first, second=second):
                self.assertEqual(globs_overlap(first, second), expected)

    def test_clones_root_rejects_symlink(self) -> None:
        symlink_target = self.temp_path / "actual_clones"
        symlink_target.mkdir()
        symlink_path = self.temp_path / "symlink_clones"
        symlink_path.symlink_to(symlink_target)
        with (
            patch.dict(os.environ, {"AGENT_RELAY_CLONES_DIR": str(symlink_path)}),
            self.assertRaisesRegex(RelayError, "must not be a symlink"),
        ):
            clones_root()

    def test_git_environment(self) -> None:
        env = git_environment()
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(env["LC_ALL"], "C")
        self.assertNotIn("SSH_AUTH_SOCK", env)

    def test_snapshot_metadata_not_directory(self) -> None:
        empty = self.temp_path / "empty"
        empty.mkdir()
        self.assertEqual(snapshot_metadata(empty), {".git": "<not a directory>"})


if __name__ == "__main__":
    unittest.main()
