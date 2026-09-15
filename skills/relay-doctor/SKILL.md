---
name: relay-doctor
description: Diagnose Agent Relay installation, provider availability, model configuration, and CLI probes.
---

Before launching a provider in a restricted host, read [sandbox execution](../../references/sandbox-execution.md). These CLIs need runtime/log access and may need a localhost listener. A detached job inherits the submitter's restrictions. Use the host's approval mechanism for necessary permissions; do not treat a writable output directory or executable discovery as readiness.

Use this skill when delegation fails, a provider is new, or you need to establish which local coding-agent CLIs are usable.

1. Resolve the plugin root as the directory two levels above the directory containing this `SKILL.md`.
2. Run `agent-relay doctor` when available, otherwise `python3 <plugin-root>/scripts/relay.py doctor`, then run `models --provider opencode`, `models --provider antigravity`, and `models --provider cursor` for the installed providers relevant to the task.
3. For a custom adapter, pass `--provider NAME --adapter-file /abs/path/adapter.json` to both `doctor` and `models`. Add `--probe` when checking the provider's actual help invocation is useful. Built-in probes do not run inference.
4. Report executable discovery, authentication/configuration status, model IDs, supported effort mappings, and the smallest corrective action. Doctor reports declared effort capabilities, not a live verification of every level. Do not print credentials or modify global CLI configuration.

The bundled providers are `opencode`, `antigravity`, and `cursor`; additional adapters are documented in [adding-adapters.md](../../references/adding-adapters.md). Cursor's bundled `--trust` flag is limited to Relay's fresh temporary cwd and must never be treated as trust for a source workspace.
