---
name: background-tasks
description: Submit, monitor, collect, and cooperatively cancel long-running or concurrent Agent Relay jobs.
---

Before launching a provider in a restricted host, read [sandbox execution](../../references/sandbox-execution.md). These CLIs need runtime/log access and may need a localhost listener. Antigravity's `--sandbox` flag does not grant host permission for its startup listener. A detached job inherits the submitter's restrictions. Use the host's approval mechanism for necessary permissions; do not treat a writable output directory or executable discovery as readiness.

Use this skill when delegated work should continue while the orchestrator does other work, when several independent workers can run concurrently, or when a worker may outlast the current Codex turn.

Resolve the plugin root as the directory two levels above the directory containing this `SKILL.md`. Resolve the jobs directory from `--jobs-dir`, then `AGENT_RELAY_JOBS_DIR`, then `~/.local/state/agent-relay/jobs`. Use absolute paths for task files, repository roots, output paths, adapter files, and jobs directories. Do not use shell background `&`; `submit` detaches the runner and returns promptly.

1. Submit one bounded task. Include the same run settings: `--provider`, optional `--model` and `--effort`, `--task-file`, `--root`, `--files`, `--kind`, `--timeout`, `--max-input-bytes`, `--max-answer-chars`, and optional `--adapter-file`. `--output` is optional for jobs; its default is `job_dir/artifacts`, and a supplied path must be fresh.
2. Save the returned JSON fields, especially `job_id`, `jobs_dir`, `job_dir`, and `output_dir`. Submission validates and snapshots the task, source bundle, adapter, and runtime Python files before returning.
3. Continue independent work, then inspect with `status JOB_ID`. Use `wait JOB_ID --timeout 30` for a bounded wait. A timeout returns exit code 2 and `wait_timed_out: true` without cancelling the worker.
4. Retrieve with `result JOB_ID` once complete. Inspect the saved result and cited source lines yourself before relying on the answer. `result` exits 2 for pending jobs and 1 for failures. `status` exits 0 even for a failed job so it can be used for inspection loops.
5. If work is no longer needed, use `cancel JOB_ID`. Cancellation is cooperative and idempotent; `cancelling` may remain visible until the worker drains. The client does not kill PIDs.

Example:

```sh
python3 <plugin-root>/scripts/relay.py submit \
  --provider antigravity --effort low \
  --task-file /abs/path/extract.txt --root /abs/repository \
  --files src/parser.py docs/format.md --kind read --timeout 120 \
  --jobs-dir /abs/path/relay-jobs
python3 <plugin-root>/scripts/relay.py wait JOB_ID --timeout 30 --jobs-dir /abs/path/relay-jobs
python3 <plugin-root>/scripts/relay.py result JOB_ID --jobs-dir /abs/path/relay-jobs
```

Independent jobs have distinct directories and artifacts and may run concurrently. There is no built-in queue or concurrency limiter; respect provider quotas. Jobs persist outside the plugin cache and snapshots protect them from subsequent source edits. A detached runner may outlive the submitter or a new Codex session while the OS permits. Reboot or hard kill leads to `interrupted` with no automatic retry or resume; an abrupt runner kill may leave an external CLI process, so cancellation cannot guarantee cleanup.

The worker inherits the invoking environment and CLI credentials. Job snapshots, logs, terminal results, and provider session history can contain sensitive source material. Local job files do not provide hard OS isolation.

The bundled Cursor adapter uses the stable Relay-owned workspace that the user explicitly trusted during setup. It never passes `--trust`. Do not point Cursor at the source project workspace or add approval flags when submitting a background job.

For ordinary one-shot delegation, use [delegate](../delegate/SKILL.md). For adapter flags and provider configuration, read [adding-adapters.md](../../references/adding-adapters.md).
