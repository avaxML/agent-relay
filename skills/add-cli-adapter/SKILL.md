---
name: add-cli-adapter
description: Add a coding-agent CLI to Agent Relay through a declarative adapter JSON definition.
---

Use this skill when a user wants to connect another local coding-agent CLI without changing the relay runner.

Read [adding-adapters.md](../../references/adding-adapters.md) completely before writing an adapter. Confirm the CLI's noninteractive invocation, model flag, timeout behavior, input transport, output format, and probe command. Prefer a wrapper executable when the CLI uses a different wire format.

Create one JSON object with all required fields: `name`, `executable`, `default_model`, `args`, `input`, `output`, `env`, `models_args`, and `probe_args`. Use only the documented placeholders (`{model}`, `{request_file}`, `{timeout}`, `{workdir}`). Pass it with `--adapter-file ABSOLUTE_JSON` to `run`; the external definition selects one named adapter and adds no runner code.

Add the optional `effort` mapping only for verified model IDs and levels. See the reference for argument and model-variant mappings. Existing adapters without it still work when `--effort` is omitted.

Run `doctor --provider NAME --adapter-file ABSOLUTE_JSON`, `models --provider NAME --adapter-file ABSOLUTE_JSON`, and a small read task before recommending the adapter. Never add auto-approval flags, embed credentials, or claim the CLI is supported merely because its executable exists.
