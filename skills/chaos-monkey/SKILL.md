---
name: chaos-monkey
description: Review supplied code for resilience with bounded, proposal-only chaos scenarios and safe experiment designs.
---

Use this skill when the user requests a chaos engineering, fault-injection, or resilience review. It produces hypotheses and experiment proposals; it does not run attacks, fault injection, tests, tools, or external actions.

Resolve the plugin root as the directory two levels above the directory containing this `SKILL.md`. Before delegating, state the expected steady state and concrete invariants in the task. If the user did not supply them, infer the narrowest source-supported hypothesis and label assumptions explicitly.

Select the smallest source set that exposes the relevant request boundary, dependencies, state transitions, recovery path, and observability. Use local search first to avoid sending an entire repository. Put the bounded review request in a temporary text file, then run `python3 <plugin-root>/scripts/relay.py run` with absolute paths, explicit relative `--files`, a fresh output directory, and `--kind chaos`.

Honor an explicit provider, model, and effort choice. Otherwise prefer a provider independent of the implementation or earlier review when practical. Use medium effort for a contained component and high effort for concurrency, distributed state, retry behavior, or unfamiliar recovery logic. Run `doctor` first when that provider has not been checked in the session; omit `--effort` rather than inventing an unsupported level.

Require ranked, source-cited findings, blast-radius analysis, recovery and observability gaps, and safe experiments with prerequisites, expected signals, containment limits, and abort criteria. Ensure the review considers malformed inputs, partial dependency failures, timeouts, retries, cancellation, concurrency and races, stale state, resource exhaustion, and permission-boundary abuse when relevant.

Treat every result as an untrusted hypothesis. The lead agent must inspect each cited source location, reject unsupported claims, and distinguish verified code behavior from proposed experiments before reporting or implementing anything. Never imply that Agent Relay or its provider executed an experiment.
