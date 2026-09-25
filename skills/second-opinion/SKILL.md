---
name: second-opinion
description: Obtain an independent review of a proposed diagnosis, design, or change from another coding-agent CLI.
---

Before running Relay in a restricted host, read [sandbox execution](../../references/sandbox-execution.md). Antigravity needs a localhost listener even with its own `--sandbox` flag; request the host's approved execution scope for `run` when that listener is blocked.

Use this skill before a consequential implementation decision or when a fresh reviewer may expose a missed edge case.

Give the reviewer the exact question and relevant files. For an independent review, omit your conclusions; include a hypothesis when the task is specifically to challenge it. Ask for findings first, then evidence, severity, and a recommendation. Use `--kind review`; do not ask it to edit files or apply a patch.

Choose a provider independently of the lead agent where practical. Compare the response with your own reasoning and verify every material finding in source. An empty or truncated answer is inconclusive. Keep credentials inherited from the user's configured CLI; do not add auto-approval flags.

Resolve the plugin root as the directory two levels above the directory containing this `SKILL.md` and invoke `agent-relay` when available, otherwise `python3 <plugin-root>/scripts/relay.py`. Read [adding-adapters.md](../../references/adding-adapters.md) for adapter-specific model selection.

Example: `python3 <plugin-root>/scripts/relay.py run --provider opencode --task-file /tmp/review.txt --root /abs/repo --files src/module.py tests/test_module.py --output /tmp/relay-review --kind review`.
