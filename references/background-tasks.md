# Background task lifecycle

Agent Relay jobs provide a small, provider-neutral lifecycle around the existing worker invocation. The CLI starts a detached runner after validating and snapshotting all inputs, then exposes inspection commands that work from any later shell or Codex session.

## Commands

```sh
python3 scripts/relay.py submit [run flags] [--output DIR] [--jobs-dir DIR]
python3 scripts/relay.py status JOB_ID [--jobs-dir DIR]
python3 scripts/relay.py wait JOB_ID [--timeout 30] [--jobs-dir DIR]
python3 scripts/relay.py result JOB_ID [--jobs-dir DIR]
python3 scripts/relay.py cancel JOB_ID [--jobs-dir DIR]
```

Submit accepts `--provider`, `--model`, `--effort`, `--task-file`, `--root`, `--files`, `--kind`, `--timeout`, `--max-input-bytes`, `--max-answer-chars`, and `--adapter-file`, matching `run`. `--timeout` is the worker timeout. `--output` defaults to `job_dir/artifacts`; a supplied directory must not already exist. `--jobs-dir` defaults to `AGENT_RELAY_JOBS_DIR` or `~/.local/state/agent-relay/jobs`.

Submit returns promptly with JSON containing `job_id`, `status`, `jobs_dir`, `job_dir`, and `output_dir`. The initial status is `queued` or `running`, depending on how quickly the detached runner starts. Jobs transition through `queued`, `running`, `cancelling`, `completed`, `failed`, `cancelled`, or `interrupted`. The existing worker result status `ok` is represented by the job status `completed`; the saved result JSON remains available under the artifact directory.

`status` is read-only inspection and exits 0 even for failed jobs. `wait` waits no longer than its timeout. If the job has not reached a terminal state, it exits 2 and returns `wait_timed_out: true`; this does not cancel the worker. `result` returns the existing result JSON for completed jobs, exits 2 while pending, and exits 1 for failed or cancelled jobs. `cancel` requests cooperative cancellation and is idempotent. It can return `cancelling` while the worker drains. The client does not kill a PID.

## Persistence and concurrency

The job directory contains snapshots of the task, explicit source corpus, adapter, and runtime Python files, plus state, logs, and terminal artifacts. Snapshots preserve the submitted inputs if the source tree changes later. Jobs live outside the plugin cache, so plugin reinstall does not remove them. Detached runners can outlive the submitter and a new Codex session while the operating system keeps them alive.

There is no built-in queue or concurrency limiter. Submit independent jobs concurrently only within provider quota and machine capacity. Distinct job and artifact directories allow their records and results to be collected independently.

Reboot or hard kill produces `interrupted`; there is no automatic retry or resume. A worker that cannot start within 15 seconds also becomes interrupted and will not execute the task later. An abruptly killed runner may leave an external provider CLI process behind, and cancellation cannot guarantee cleanup in that case. Job records are local files, not hard OS isolation. The runner inherits the invoking environment and CLI credentials. Snapshots, logs, terminal results, and provider session history may contain sensitive source material and should be retained consciously.
