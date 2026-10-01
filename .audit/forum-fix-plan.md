# Forum branch repair plan

Definition of done: all eleven Oracle findings have a demonstrated fix or a documented, tested design decision; forum CLI tests exercise the repaired behavior; `./scripts/check.sh` passes; the branch diff has no unrelated changes.

The work spans the detached job lifecycle, SQLite forum state, prompt snapshots, consensus artifacts, tests, and documentation. The risky boundary is between SQLite commits and filesystem process launches.

1. Capture the current forum test baseline and add public CLI reproductions for the defects.
2. Sketch the topic and round state transitions, durable job identity, and per-round input shape. Review at least two lifecycle designs before implementation.
3. Make job preparation and launch recoverable; serialize round ownership and terminal transitions. Verify crash and close/abandon races.
4. Give each one-shot worker a complete candidate set and enforce ballot eligibility. Preserve input for failed members. Verify convergence with fake CLIs.
5. Freeze forum source evidence, make consensus export agree with SQLite, and repair chair posts and schema startup. Verify each unit.
6. Run the real CLI path, full repository checks, review the diff, and update forum documentation.

Throughput checkpoint: the state model and regression tests block implementation. The job and forum files share a lifecycle and have one write owner. Documentation can follow after behavior settles. Separate worktrees are required for any additional writing delegate.

Chosen shape: `round_jobs` receives fresh IDs when SQLite reserves a round. Those IDs stay fixed while that reservation is pending. `jobs.prepare` publishes each immutable job directory atomically, and `jobs.launch` can be retried against its reserved ID. A later `forum round` resumes a launch that stopped midway. An abandoned round can reuse its round number with new job IDs. The forum reuses its existing round and claim tables. Every task includes the same latest prior claim set, while a ballot may name only an unambiguous claim in that set. The alternative full outbox and lease schema added more state than this two-command CLI needs. The separate job implementation was verified in its own worktree before integration.

Verification checkpoints: the initial 22 forum tests passed. Seven new regression checks failed for the predicted reasons before production edits. Focused tests turned green after the lifecycle, protocol, and publication units. The final `./scripts/check.sh` gate passed with 92 tests on Python 3.11. An independent code review identified a suspected duplicate provider call. A forced two-supervisor test showed that the worker lock permits only one execution. A stale-PID test found a separate retry bug, which passed after removing the PID check. A second audit caught a stale launch error after a failed supervisor spawn; the new regression failed before clearing that error and passed afterward. Commit and remote update are delivery steps.
