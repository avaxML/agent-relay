---
name: propose-patch
description: Have another coding-agent CLI design an implementation patch without changing the workspace.
---

Before running Relay in a restricted host, read [sandbox execution](../../references/sandbox-execution.md). Antigravity needs a localhost listener even with its own `--sandbox` flag; request the host's approved execution scope for `run` when that listener is blocked.

Use this skill when you want an implementation proposal before the lead agent edits code.

Describe the desired behavior, constraints, relevant files, and acceptance checks. Use `--kind patch` and request a unified diff. The result is advisory: verify the source hashes still match, inspect the diff, and run `git apply --check` before applying it within the user's authorized scope. Run the relevant tests after applying. Never treat a returned diff as already applied or tested.

Keep the delegated scope small and avoid including secrets or unrelated customer documents. Use a fresh output directory and retain the result for comparison if useful. Resolve the plugin root as the directory two levels above the directory containing this `SKILL.md` and invoke `agent-relay` when available, otherwise `python3 <plugin-root>/scripts/relay.py`.

Example: `python3 <plugin-root>/scripts/relay.py run --provider opencode --task-file /tmp/change-request.txt --root /abs/repo --files src/module.py --output /tmp/relay-proposal --kind patch`.
