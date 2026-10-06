# Adding a CLI adapter

Agent Relay keeps provider wiring declarative. The runner has bundled `opencode`, `antigravity`, and `cursor` adapters. An external adapter is one JSON object. Install it in the user registry for repeated use, or pass it with `--adapter-file` for one invocation.

Adapter files are trusted execution configuration because they select an executable, its arguments, and environment overrides. Do not install an adapter discovered inside untrusted source content. The registry lives outside the installed plugin cache, so plugin reinstall does not remove it.

## Adapter shape

Required fields, with optional `effort` and `tools` blocks:

```json
{
  "name": "my-cli",
  "executable": "my-agent",
  "default_model": "provider/model",
  "args": ["run", "--model", "{model}", "--input", "{request_file}", "--timeout", "{timeout}"],
  "input": "file",
  "output": "text",
  "env": {},
  "models_args": ["models"],
  "probe_args": ["--help"]
}
```

All nine fields are required. `args` may use `{model}`, `{request_file}`, `{timeout}`, `{workdir}`, and `{provider_workspace}`. Auto-approval flags (`--force`, `--yolo`, `--trust`, `--auto`, `--auto-review`, `--dangerously-skip-permissions`, `--approve-mcps`, including `--flag=value`) are rejected in `args`, `models_args`, `probe_args`, and effort level args; they may appear only in `tools.args`. The provider workspace is a stable private directory under the Agent Relay state directory for CLIs that require an explicitly trusted working directory. Input transport is `file`, `stdin`, or `agy-stream`; output is `opencode-jsonl`, `agy-jsonl`, `cursor-jsonl`, or `text`. `cursor-jsonl` requires exactly one event with `type: "result"`, `subtype: "success"`, a false or absent `is_error`, and a string `result`; it retains `session_id`, `request_id`, `usage`, and `duration_ms` metadata. `env` is a map of environment overrides; `models_args` and `probe_args` define the CLI's model-list and help probes. Keep the executable name or use an absolute path when PATH discovery is unreliable.

Before adding an adapter, check the CLI's own help for a noninteractive mode and confirm that its base invocation can run without broad write or approval flags. A wrapper executable is appropriate when the CLI needs custom request serialization or output parsing; keep the wrapper outside this runner and document its contract.

## Optional reasoning effort

Existing adapters need no change. To support `--effort`, add a verified mapping such as:

```json
"effort": {
  "models": ["provider/model"],
  "levels": {
    "low": {"args": ["--reasoning", "low"]},
    "high": {"args": ["--reasoning", "high"]}
  }
}
```

`models` is an exact allowlist, not a wildcard. Each level must contain `args`, `model`, or both. Arguments are appended to the invocation; a model setting replaces the `{model}` value used by the base arguments. These settings may use only `{model}`, for example `"model": "{model}#high"`. Omitted effort leaves arguments and model unchanged. The runner rejects unsupported levels or models before launching the CLI. Use `doctor` to inspect these declarations, and verify each mapping against the CLI and model rather than assuming that every provider supports the same levels. Antigravity's built-in mapping changes the model variant and supplies a matching effort flag.

A level may also declare `"timeout"`, a positive integer of seconds, when it routinely needs longer than the 180-second default. It applies only when the caller omits `--timeout`, either for the requested `--effort` or, without `--effort`, when the requested model is the one that level selects (for example `--model grok-4.7-xhigh`); and the resolved value fills the `{timeout}` placeholder and is recorded as `timeout` in `result.json`. A timeout alone is not a valid level; the level must still change the model or supply arguments. The bundled Cursor `xhigh` and OpenCode `max` levels declare 600 seconds.

## Optional tools mode

The `probe` and `execute` kinds need a tools mode. Without one, a provider rejects them with "provider <name> has no verified tools mode". Declare it only after a live probe and execute on a synthetic fixture:

```json
"tools": {
  "args": ["-p", "--workspace", "{clone}", "--trust", "--model", "{model}"],
  "env": {},
  "pass_env": ["MY_CLI_API_KEY"]
}
```

`tools.args` and `tools.env` replace the base `args` and `env` for tool kinds; they never extend them, so base restrictions such as plan mode or deny-all permissions do not leak into tools mode and tools flags never reach the tool-free kinds. `tools.args` may use the base placeholders plus `{clone}`, the provider's work tree. File transport still requires `{request_file}`. `pass_env` lists environment variables to keep even though they match Relay's secret filter (names containing `TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`, `CREDENTIAL`, `API_KEY`, `ACCESS_KEY`, or `PRIVATE_KEY`; `GIT_*` and `SSH_AUTH_SOCK` are always removed). Every tools mode must keep the same contract: run only in the given clone, report through the adapter's existing output decoder, and fail (nonzero exit or empty output) rather than succeed silently when a tool call is denied.

## Kinds

| Kind | Tools | Runs in | Result |
| --- | --- | --- | --- |
| `read`, `review`, `patch`, `chaos` | none | a temporary directory, with a JSON bundle of `--files` | `schema_version: 1` |
| `probe` | adapter `tools` | a throwaway clone at `--ref` | `schema_version: 2`; any tracked or `.git` change is a `violation` |
| `execute` | adapter `tools` | a throwaway clone at `--ref`; requires `--owns GLOB` | `schema_version: 2`; any change outside `--owns` or under `.git` is a `violation`; the clone is kept until `cleanup` |

Tool kinds accept `--ref` (default `HEAD`), repeatable `--setup CMD` and `--check CMD`, and `--check-timeout SECONDS` (default 180, separate from the provider `--timeout`). Setup runs before the provider and checks after it, through Relay's runner with `shlex` splitting and no shell. Their request lists `--files` as starting points instead of bundling contents; each must be a file in the pinned commit, not merely in the current checkout. Relay captures the clone again after its checks, so a change a check makes is judged and appears in `changes.diff` (`check_side_effects` lists the paths it added or removed). Results add `ref`, `sha`, `clone` (execute), `diff_path` (`changes.diff`), `files_changed`, `tracked_changes`, `untracked_files`, `violations`, `checks` (Relay's own runs: command, exit code, duration, output tail), `provider_claims` (the provider's answer, unverified), `env_removed`, and `status` (`ok`, `violation`, `check_failed`, `error`, or `cancelled`). See [ADR 0001](../docs/adr/0001-tool-enabled-workers.md).

## Invocation

Resolve the plugin root as the directory two levels above the directory containing a skill's `SKILL.md`. Validate and install the adapter before a real task:

```sh
python3 <plugin-root>/scripts/relay.py adapter validate /abs/path/my-cli.json
python3 <plugin-root>/scripts/relay.py adapter install /abs/path/my-cli.json
python3 <plugin-root>/scripts/relay.py adapter validate my-cli --probe
python3 <plugin-root>/scripts/relay.py models --provider my-cli
```

Use absolute paths for the task file, repository root, and output directory:

```sh
python3 <plugin-root>/scripts/relay.py run \
  --provider my-cli \
  --model provider/model \
  --task-file /abs/path/task.txt \
  --root /abs/path/repository \
  --files src/module.py tests/test_module.py \
  --output /abs/path/fresh-output \
  --kind read
```

Use `--adapter-file /abs/path/my-cli.json` on `run`, `submit`, `doctor`, or `models` when you want a one-invocation definition. The explicit file takes precedence over the registry. A registered file takes precedence over the bundled directory. Registered names cannot match bundled names.

The registry stores each provider at `<registry>/<name>.json`. Agent Relay uses `AGENT_RELAY_ADAPTERS_DIR`, then `$XDG_CONFIG_HOME/agent-relay/adapters`, then `~/.config/agent-relay/adapters`. Pass `--registry-dir PATH` to use another directory. `adapter list` reports bundled and registered definitions. Reinstalling identical content succeeds. Changed content requires `adapter install PATH --replace`. `adapter remove NAME` removes only a registered definition and succeeds when that definition is already absent.

For tool-free kinds the runner executes in an isolated temporary working directory, packages explicit text files with SHA-256 hashes and numbered lines, and writes `result.json`, `answer.txt`, `stdout.log`, and `stderr.log` under a fresh private output directory. It returns a result preview and explicit truncation metadata. Tool kinds also write `changes.diff` and the logs of Relay's git, setup, and check commands. The runner never applies patches or diffs to the user's checkout.

## Built-in defaults

- OpenCode defaults to `opencode-go/deepseek-v4.1-flash`; its tools are denied by relay configuration for tool-free kinds. Its tools mode (`run --standalone` with a scoped `OPENCODE_CONFIG_CONTENT`: read, list, glob, grep, edit, and bash allowed; `external_directory`, web, task, and everything else denied) was live-verified with opencode v2.0.24.
- Antigravity defaults to `gemini-3.8-flash-low`, plan mode, and sandbox mode. Its tools mode (`--mode accept-edits --sandbox --add-dir {clone} --dangerously-skip-permissions`) was live-verified with agy 1.3.0.
- Cursor defaults to `grok-4.7-high`, ask mode, enabled sandboxing, stdin input, and stream-JSON output. The bundled adapter targets locally verified `cursor-agent` version `2026.10.01-e373342`; its probe is `--help` and its model listing command is `models`. Low, medium, high, and xhigh effort select the exact matching `grok-4.7-LEVEL` model ID without extra arguments; xhigh defaults to a 600-second timeout, also when `--model grok-4.7-xhigh` is passed without `--effort`. `composer-2.5` is reachable with `--model composer-2.5` and no `--effort`; it is not in the effort allowlist because every level selects a fixed Grok ID. Its tools mode omits `--mode` (agent mode) and passes `--workspace {clone} --trust --sandbox enabled`.

Cursor's tool-free invocation uses `{provider_workspace}` to select a stable, Relay-owned empty directory. Before the first run, open `cursor-agent` interactively in that directory and approve Cursor's normal workspace-trust prompt. That invocation never passes `--trust`, a source project workspace, `--force`, `--yolo`, `--auto-review`, or `--approve-mcps`. The tools mode passes `--trust` only for a fresh Relay clone, which records a per-path trust marker under `~/.cursor/projects`, and never passes `--force`, `--yolo`, `--auto-review`, or `--approve-mcps`. Set `AGENT_RELAY_PROVIDER_WORKSPACES_DIR` to move the provider workspace root. Otherwise Relay uses `$XDG_STATE_HOME/agent-relay/providers` or `~/.local/state/agent-relay/providers`. The provider still inherits host permissions because this is not an OS sandbox.

These defaults depend on the user's installed CLIs, authentication, and subscription access. Credentials and global configuration are inherited from those CLIs. Local output may contain sensitive source material; the user manages artifact retention. Agent Relay does not provide an OS security sandbox or hook enforcement.
