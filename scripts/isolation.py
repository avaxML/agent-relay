"""Isolation lifecycle, hardened git execution, capture, and ownership matching."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import stat
from collections.abc import Collection, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from execution import execute
from providers import RelayError

SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|ACCESS_KEY|PRIVATE_KEY", re.IGNORECASE)

GIT_CONFIG = [
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.excludesFile=/dev/null",
    "-c",
    "core.autocrlf=false",
    "-c",
    "protocol.file.allow=always",
    "-c",
    "core.attributesFile=/dev/null",
]

METADATA_IGNORED = {"index", "objects", "logs", "FETCH_HEAD", "ORIG_HEAD"}


def tool_environment(base: Mapping[str, str], keep: Collection[str] = ()) -> tuple[dict[str, str], list[str]]:
    keep_set = set(keep)
    env: dict[str, str] = {}
    removed: list[str] = []
    for key, value in base.items():
        if key.startswith("GIT_") or key == "SSH_AUTH_SOCK":
            removed.append(key)
        elif key in keep_set:
            env[key] = value
        elif SECRET_NAME.search(key) is not None:
            removed.append(key)
        else:
            env[key] = value
    return env, sorted(removed)


def validate_globs(globs: Sequence[str]) -> list[str]:
    for pattern in globs:
        if not pattern or "\\" in pattern or pattern.startswith("/"):
            raise RelayError(f"--owns pattern is invalid: {pattern!r}")
        parts = pattern.split("/")
        for part in parts:
            if part == "" or part in {".", "..", ".git"}:
                raise RelayError(f"--owns pattern is invalid: {pattern!r}")
    return list(globs)


def _match_segments(p_parts: list[str], g_parts: list[str], p_idx: int = 0, g_idx: int = 0) -> bool:
    if g_idx == len(g_parts):
        return p_idx == len(p_parts)

    if g_parts[g_idx] == "**":
        min_segs = 1 if (g_idx == len(g_parts) - 1 and g_idx > 0) else 0
        if g_idx == len(g_parts) - 1:
            remaining = len(p_parts) - p_idx
            return remaining >= min_segs
        for count in range(min_segs, len(p_parts) - p_idx + 1):
            if _match_segments(p_parts, g_parts, p_idx + count, g_idx + 1):
                return True
        return False

    if p_idx == len(p_parts):
        return False

    if not fnmatch.fnmatchcase(p_parts[p_idx], g_parts[g_idx]):
        return False

    return _match_segments(p_parts, g_parts, p_idx + 1, g_idx + 1)


def owned(path: str, globs: Sequence[str]) -> bool:
    if not globs:
        return False
    path_parts = path.split("/")
    for pattern in globs:
        glob_parts = pattern.split("/")
        if _match_segments(path_parts, glob_parts):
            return True
    return False


def globs_overlap(first: str, second: str) -> bool:
    first_parts = first.split("/")
    second_parts = second.split("/")
    common_len = min(len(first_parts), len(second_parts))

    for i in range(common_len):
        seg1 = first_parts[i]
        seg2 = second_parts[i]

        if seg1 == "**" or seg2 == "**":
            return True

        is_lit1 = not any(c in seg1 for c in "*?[")
        is_lit2 = not any(c in seg2 for c in "*?[")

        if is_lit1 and is_lit2 and seg1 != seg2:
            return False
        if is_lit1 and not is_lit2 and not fnmatch.fnmatchcase(seg1, seg2):
            return False
        if is_lit2 and not is_lit1 and not fnmatch.fnmatchcase(seg2, seg1):
            return False

    return len(first_parts) == len(second_parts)


def clones_root() -> Path:
    configured = os.environ.get("AGENT_RELAY_CLONES_DIR")
    if configured:
        root = Path(configured).expanduser()
    elif state_home := os.environ.get("XDG_STATE_HOME"):
        root = Path(state_home).expanduser() / "agent-relay" / "clones"
    else:
        root = Path.home() / ".local" / "state" / "agent-relay" / "clones"

    if root.is_symlink():
        raise RelayError(f"Clones root must not be a symlink: {root}")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir():
        raise RelayError(f"Clones root must be a directory: {root}")
    root.chmod(0o700)
    return root.resolve()


def git_environment() -> dict[str, str]:
    env, _ = tool_environment(os.environ)
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_TERMINAL_PROMPT="0",
        GIT_OPTIONAL_LOCKS="0",
        LC_ALL="C",
    )
    return env


def run_git(
    args: Sequence[str],
    logs: Path,
    timeout: float,
    max_bytes: int = 8_000_000,
    git_dir: Path | None = None,
    work_tree: Path | None = None,
    cwd: Path | None = None,
    index: Path | None = None,
) -> bytes:
    git_executable = shutil.which("git")
    if not git_executable:
        raise RelayError("git executable not found")

    argv = [git_executable, *GIT_CONFIG]
    if git_dir is not None:
        argv.append(f"--git-dir={git_dir}")
    if work_tree is not None:
        argv.append(f"--work-tree={work_tree}")
    argv.extend(args)

    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing_count = sum(1 for p in logs.glob("git-*") if p.is_dir())
    call_dir = logs / f"git-{existing_count:03d}"
    call_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stdin_file = call_dir / "stdin"
    stdin_file.touch()

    env = git_environment()
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)

    run_cwd = cwd or work_tree or git_dir or logs
    code = execute(argv, run_cwd, env, stdin_file, call_dir, timeout, max_log_bytes=max_bytes)

    stderr_path = call_dir / "stderr.log"
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
    if code != 0:
        cmd_name = args[0] if args else "command"
        raise RelayError(f"git {cmd_name} failed with exit code {code}: {stderr_text[-500:]}")

    stdout_path = call_dir / "stdout.log"
    return stdout_path.read_bytes() if stdout_path.exists() else b""


@dataclass(frozen=True)
class Clone:
    directory: Path
    work: Path
    git_dir: Path
    index: Path
    sha: str
    fingerprint: tuple[tuple[str, str], ...]


def _private_fingerprint(git_dir: Path) -> tuple[tuple[str, str], ...]:
    records: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(git_dir, followlinks=False):
        current = Path(dirpath)
        if current == git_dir:
            dirnames[:] = [name for name in dirnames if name != "objects"]
        linked = [name for name in dirnames if (current / name).is_symlink()]
        for name in linked:
            dirnames.remove(name)
            path = current / name
            records[path.relative_to(git_dir).as_posix()] = f"symlink:{os.readlink(path)}"
        for name in filenames:
            path = current / name
            rel = path.relative_to(git_dir).as_posix()
            if path.is_symlink():
                records[rel] = f"symlink:{os.readlink(path)}"
            elif path.is_file():
                records[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return tuple(sorted(records.items()))


def resolve_ref(root: Path, ref: str, logs: Path, timeout: float) -> str:
    args = ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"]
    try:
        raw = run_git(args, logs, timeout, cwd=root)
    except RelayError as exc:
        raise RelayError(f"--ref does not name a commit: {ref}") from exc

    sha = raw.decode("utf-8", errors="replace").strip()
    if not ((len(sha) == 40 or len(sha) == 64) and all(c in "0123456789abcdefABCDEF" for c in sha)):
        raise RelayError(f"--ref does not name a commit: {ref}")
    return sha


def create_clone(root: Path, sha: str, logs: Path, timeout: float) -> Clone:
    directory = clones_root() / uuid4().hex
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    directory.chmod(0o700)

    try:
        repo_git = directory / "repo.git"
        work = directory / "work"
        work_git = work / ".git"
        index = directory / "relay.index"

        run_git(["init", "--quiet", "--bare", str(repo_git)], logs, timeout)
        run_git(
            ["fetch", "--no-tags", "--quiet", str(root), f"{sha}:refs/heads/relay-base"],
            logs,
            timeout,
            git_dir=repo_git,
        )
        run_git(["symbolic-ref", "HEAD", "refs/heads/relay-base"], logs, timeout, git_dir=repo_git)
        run_git(
            ["clone", "--quiet", "--no-local", "--no-hardlinks", "--no-checkout", str(repo_git), str(work)],
            logs,
            timeout,
            cwd=directory,
        )
        run_git(["remote", "remove", "origin"], logs, timeout, git_dir=work_git, work_tree=work)
        run_git(["checkout", "--quiet", "--detach", sha], logs, timeout, git_dir=work_git, work_tree=work)
        run_git(["read-tree", sha], logs, timeout, git_dir=repo_git, work_tree=work, index=index)
        info = repo_git / "info"
        info.mkdir(mode=0o700, exist_ok=True)
        (info / "attributes").write_text("* -filter -ident -working-tree-encoding\n", encoding="utf-8")
        return Clone(
            directory=directory,
            work=work,
            git_dir=repo_git,
            index=index,
            sha=sha,
            fingerprint=_private_fingerprint(repo_git),
        )
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def remove_clone(directory: Path) -> None:
    if directory.is_symlink():
        raise RelayError(f"Clone directory must not be a symlink: {directory}")
    root = clones_root()
    try:
        resolved = directory.resolve(strict=False)
        resolved.relative_to(root)
    except ValueError:
        raise RelayError(f"Clone directory must be inside clones root: {directory}") from None
    if resolved == root:
        raise RelayError(f"Clone directory cannot be clones root itself: {directory}")
    if not directory.exists():
        return
    shutil.rmtree(directory)


def snapshot_metadata(work: Path) -> dict[str, str]:
    dot_git = work / ".git"
    if dot_git.is_symlink() or not dot_git.is_dir():
        return {".git": "<not a directory>"}

    records: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(dot_git, followlinks=False):
        if Path(dirpath) == dot_git:
            dirnames[:] = [d for d in dirnames if d not in METADATA_IGNORED]
            filenames = [f for f in filenames if f not in METADATA_IGNORED]

        symlink_dirs: list[str] = []
        for d in dirnames:
            p = Path(dirpath) / d
            if p.is_symlink():
                symlink_dirs.append(d)
                rel = f".git/{p.relative_to(dot_git).as_posix()}"
                records[rel] = f"symlink:{os.readlink(p)}"
        for d in symlink_dirs:
            dirnames.remove(d)

        for f in filenames:
            p = Path(dirpath) / f
            rel = f".git/{p.relative_to(dot_git).as_posix()}"
            if p.is_symlink():
                records[rel] = f"symlink:{os.readlink(p)}"
            elif p.is_file():
                with suppress(OSError):
                    records[rel] = hashlib.sha256(p.read_bytes()).hexdigest()

    return records


@dataclass(frozen=True)
class Capture:
    tracked_changes: list[str]
    untracked_files: list[str]
    metadata_changes: list[str]
    diff: bytes


def capture(
    clone: Clone,
    before: dict[str, str],
    logs: Path,
    timeout: float,
    max_bytes: int = 8_000_000,
    max_entries: int = 10_000,
) -> Capture:
    if _private_fingerprint(clone.git_dir) != clone.fingerprint:
        raise RelayError("Relay's private repository changed during the run; capture refused.")
    clone.index.unlink(missing_ok=True)
    run_git(
        ["read-tree", clone.sha],
        logs=logs,
        timeout=timeout,
        git_dir=clone.git_dir,
        work_tree=clone.work,
        index=clone.index,
    )
    after_meta = snapshot_metadata(clone.work)
    all_keys = set(before) | set(after_meta)
    metadata_changes = sorted(k for k in all_keys if before.get(k) != after_meta.get(k))

    raw_status = run_git(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"],
        logs=logs,
        timeout=timeout,
        git_dir=clone.git_dir,
        work_tree=clone.work,
        index=clone.index,
        cwd=clone.work,
    )

    tracked_changes: list[str] = []
    untracked_files: list[str] = []
    entries = [e for e in raw_status.split(b"\0") if e]
    for raw_entry in entries:
        decoded = raw_entry.decode("utf-8", errors="replace")
        code = decoded[:2]
        path = decoded[3:]
        if code == "??":
            untracked_files.append(path)
        else:
            tracked_changes.append(path)

    total_entries = len(tracked_changes) + len(untracked_files)
    if total_entries > max_entries:
        raise RelayError(f"Capture exceeds {max_entries} entries: {total_entries}")

    total_size = 0
    for rel_path in [*tracked_changes, *untracked_files]:
        full_path = clone.work / rel_path
        try:
            st = os.lstat(full_path)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            total_size += st.st_size

    if total_size > max_bytes:
        raise RelayError(f"Capture exceeds the {max_bytes} byte limit: {total_size}")

    run_git(
        ["add", "--all", "--", "."],
        logs=logs,
        timeout=timeout,
        git_dir=clone.git_dir,
        work_tree=clone.work,
        index=clone.index,
        cwd=clone.work,
    )
    diff = run_git(
        ["diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames", clone.sha],
        logs=logs,
        timeout=timeout,
        max_bytes=max_bytes,
        git_dir=clone.git_dir,
        work_tree=clone.work,
        index=clone.index,
        cwd=clone.work,
    )

    return Capture(
        tracked_changes=sorted(tracked_changes),
        untracked_files=sorted(untracked_files),
        metadata_changes=metadata_changes,
        diff=diff,
    )


def find_violations(result: Capture, owns: Sequence[str] | None) -> list[str]:
    violations: set[str] = set(result.metadata_changes)
    if owns is None:
        violations.update(result.tracked_changes)
    else:
        for path in result.tracked_changes:
            if not owned(path, owns):
                violations.add(path)
        for path in result.untracked_files:
            if not owned(path, owns):
                violations.add(path)
    return sorted(violations)
