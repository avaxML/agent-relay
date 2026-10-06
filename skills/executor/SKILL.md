---
name: executor
description: Distribute independent, bounded probe and implementation tasks across Agent Relay providers as subagents, then verify every result as the lead.
---

Before launching providers in a restricted host, read [sandbox execution](../../references/sandbox-execution.md). Tool-enabled kinds run each provider with tools in a throwaway clone; Antigravity also needs a host-approved localhost listener, and a detached job inherits the submitter's restrictions.

Use this skill when work splits into independent, bounded tasks with clear file ownership: experiments that should actually run (`probe`), small implementations (`execute`), or reads and reviews that several providers can share. The lead keeps the plan, the decisions, and every verification. Providers act as subagents. They never commit, push, update issues or pull requests, delegate further, or decide acceptance.

Do not distribute work that needs shared state between tasks, open-ended exploration, changes to files several tasks must touch, or anything you would not let the provider run directly on this repository. Relay's clone is not an OS sandbox; read [ADR 0001](../../docs/adr/0001-tool-enabled-workers.md) for what it constrains and what it cannot stop.

Resolve the plugin root as the directory two levels above the directory containing this `SKILL.md`, and invoke `python3 <plugin-root>/scripts/relay.py` (or `agent-relay` when the host adds `bin/` to PATH).

## 1. Check providers

Run `doctor`, `doctor --probe`, and `models --provider NAME` for each provider you plan to use, following [relay-doctor](../relay-doctor/SKILL.md). `dispatch` runs doctor once per provider again and gives a failing provider no tasks. A provider without a verified `tools` block can take `read` and `review` tasks but not `probe` or `execute`.

## 2. Write the manifest

Write one task file per task in the same directory as the manifest, or below it. Every task file states one bounded deliverable, its inputs, and the expected evidence. For a probe, also state the steady state, invariants, and abort criteria. Then write the manifest:

```json
{
  "tasks": [
    {"id": "probe-parser", "kind": "probe", "task_file": "probe-parser.txt", "files": ["src/parser.py"],
     "checks": ["python3 -m pytest -q tests/test_parser.py"]},
    {"id": "fix-limits", "kind": "execute", "task_file": "fix-limits.txt", "files": ["src/limits.py"],
     "owns": ["src/limits.py", "tests/test_limits.py"], "checks": ["python3 -m pytest -q tests/test_limits.py"],
     "setup": ["python3 -m compileall -q src"]}
  ]
}
```

Keys are closed: `id`, `kind`, `task_file`, `files`, `owns`, `checks`, `setup`, and optional `provider`, `effort`, `model`. Unknown keys, duplicate ids, task files outside the manifest directory, absolute or `..` globs, and overlapping `owns` between `execute` tasks are rejected before anything is submitted.

- **`owns` is required for `execute`** and must be disjoint across tasks. `*` matches within one path segment; `**` matches whole segments. Any change outside `owns`, and any `.git` change, makes the result a `violation`.
- **Give every `probe` and `execute` task `checks`**: commands Relay reruns itself in the clone, without a shell, under `--check-timeout`. A provider's claim that tests passed is never evidence.
- **Use the same task wording you would give a native subagent**, so results stay comparable if a task falls back.

## 3. Dispatch

```sh
python3 <plugin-root>/scripts/relay.py dispatch --manifest /abs/tasks/manifest.json --root /abs/repository \
  --providers antigravity opencode cursor --max-per-provider 2
```

Assignment is equal. Each task without an explicit `provider` goes to the eligible provider with the fewest assigned tasks; ties follow the `--providers` order. The per-provider cap holds work back instead of skewing it. A task that no provider can take is recorded under `needs_native_fallback` with its task file and reason; Relay never drops it and never runs it elsewhere. Save the printed ledger path.

Slow levels (Cursor `xhigh`, OpenCode `max`) can run for several minutes; do not escalate to them unless the user asks. Pass `--effort` or `--model` per task in the manifest when needed.

## 4. Collect

```sh
python3 <plugin-root>/scripts/relay.py collect --dispatch /abs/path/ledger.json --timeout 600
```

`collect` waits at most `--timeout` seconds, submits tasks that the cap held back, and prints one aggregate. For each task it shows status, violations, Relay checks, and diff path; it also shows per-provider counts and the `needs_native_fallback` list. Exit code 2 means work is still running, so call it again. Rerunning it on a finished ledger submits nothing.

## 5. Verify, as the lead

For every task:

1. Read `result.json`: `status`, `violations`, `checks`, `provider_claims`, `env_removed`. Treat `provider_claims` as unverified.
2. Read the whole `changes.diff`. Confirm it touches only owned paths and does what the task asked.
3. Confirm Relay's checks ran and passed. `check_failed` or `violation` is never acceptable as is.
4. Apply in the real checkout with `git apply --check changes.diff`, then `git apply changes.diff`, then rerun the checks there yourself.
5. Commit only after your own verification. Workers never commit.

## 6. Fallback and cleanup

Use native host subagents only for `needs_native_fallback` tasks, or when no provider passes doctor. Give them the same task file text, the explicit ownership, and the rule that they share the workspace, preserve other work, and do not delegate further. If a provider fails a run, its tasks are failed results, not retries. Relay never falls back automatically, so decide whether to re-dispatch them to the other providers.

When finished, remove the kept `execute` clones (logs, diffs, and the ledger stay):

```sh
python3 <plugin-root>/scripts/relay.py cleanup --dispatch /abs/path/ledger.json
```

For one task at a time, `run` or `submit` accept the same `--kind probe|execute`, `--owns`, `--check`, `--setup`, `--ref`, and `--check-timeout` options; see [background-tasks](../background-tasks/SKILL.md).
