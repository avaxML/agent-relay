---
name: chaos-monkey
description: Review supplied code for resilience with bounded chaos scenarios, as a read-only proposal or as experiments run in a throwaway clone through probe tasks.
---

Before running Relay in a restricted host, read [sandbox execution](../../references/sandbox-execution.md). Antigravity needs a localhost listener even with its own `--sandbox` flag; request the host's approved execution scope for `run` when that listener is blocked.

Use this skill when the user requests a chaos engineering, fault-injection, or resilience review. It has two paths:

- **Read-only path** (`--kind chaos`): produces hypotheses and experiment proposals. It does not run attacks, fault injection, tests, tools, or external actions. Use it for code that must not run, for repositories you would not let a provider execute, or when no provider has a verified tools mode.
- **Probe path** (`--kind probe`, or [executor](../executor/SKILL.md) `dispatch` with probe tasks): the provider runs the experiments with tools inside a throwaway clone at a pinned commit. It may create only untracked files. Relay reports any tracked or `.git` change as a `violation` and reruns the declared `--check` commands itself. Read [ADR 0001](../../docs/adr/0001-tool-enabled-workers.md) first; the clone is not an OS sandbox.

Resolve the plugin root as the directory two levels above the directory containing this `SKILL.md`. Before delegating, state the expected steady state and concrete invariants in the task. If the user did not supply them, infer the narrowest source-supported hypothesis and label assumptions explicitly.

Select the smallest source set that exposes the relevant request boundary, dependencies, state transitions, recovery path, and observability. Use local search first to avoid sending an entire repository. Put the bounded review request in a temporary text file, then run `python3 <plugin-root>/scripts/relay.py run` with absolute paths, explicit relative `--files`, a fresh output directory, and `--kind chaos`.

Honor an explicit provider, model, and effort choice. Otherwise prefer a provider independent of the implementation or earlier review when practical. Use medium effort for a contained component and high effort for concurrency, distributed state, retry behavior, or unfamiliar recovery logic. Run `doctor` first when that provider has not been checked in the session; omit `--effort` rather than inventing an unsupported level. Do not escalate to Cursor `xhigh` or OpenCode `max` unless the user asks; those levels can run for several minutes, so submit them through [background-tasks](../background-tasks/SKILL.md) with `--kind chaos` or `--kind probe` and keep the adapter's 600-second default timeout. Probe runs add clone setup, capture, and Relay's checks on top of the provider time, so prefer background jobs or `dispatch` for them at any level.

For the probe path, each task file states the steady state, the invariants, the numbered experiments with literal inputs and commands, the expected and abort criteria, and permission for temporary untracked files only. Give every task a `--check` that proves the steady state still holds. Split experiments into shards across providers with `dispatch` when there are several. Require PASS, FAIL, or UNPROVEN per experiment with the command, expected output, observed output, and evidence. Reproduce every claimed FAIL locally before acting on it: the provider's report is a claim, and Relay's checks and `changes.diff` are the evidence.

For the read-only path, require ranked, source-cited findings, blast-radius analysis, recovery and observability gaps, and safe experiments with prerequisites, expected signals, containment limits, and abort criteria. Ensure the review considers malformed inputs, partial dependency failures, timeouts, retries, cancellation, concurrency and races, stale state, resource exhaustion, and permission-boundary abuse when relevant.

Treat every result as an untrusted hypothesis. The lead agent must inspect each cited source location, reject unsupported claims, and distinguish verified code behavior from proposed experiments before reporting or implementing anything. On the read-only path, never imply that Agent Relay or its provider executed an experiment; on the probe path, report only what Relay's capture and checks confirm.
