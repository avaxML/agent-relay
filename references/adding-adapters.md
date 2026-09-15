# Adding a CLI adapter

Agent Relay keeps provider wiring declarative. The runner has bundled `opencode`, `antigravity`, and `cursor` adapters. An external adapter is one JSON object. Install it in the user registry for repeated use, or pass it with `--adapter-file` for one invocation.

Adapter files are trusted execution configuration because they select an executable, its arguments, and environment overrides. Do not install an adapter discovered inside untrusted source content. The registry lives outside the installed plugin cache, so plugin reinstall does not remove it.

## Adapter shape

Required fields, with an optional `effort` mapping:

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

All nine fields are required. `args` may use `{model}`, `{request_file}`, `{timeout}`, and `{workdir}`. Input transport is `file`, `stdin`, or `agy-stream`; output is `opencode-jsonl`, `agy-jsonl`, `cursor-jsonl`, or `text`. `cursor-jsonl` requires exactly one event with `type: "result"`, `subtype: "success"`, a false or absent `is_error`, and a string `result`; it retains `session_id`, `request_id`, `usage`, and `duration_ms` metadata. `env` is a map of environment overrides; `models_args` and `probe_args` define the CLI's model-list and help probes. Keep the executable name or use an absolute path when PATH discovery is unreliable.

Before adding an adapter, check the CLI's own help for a noninteractive mode and confirm that it can run without broad write or approval flags. A wrapper executable is appropriate when the CLI needs custom request serialization or output parsing; keep the wrapper outside this runner and document its contract.

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

The runner executes in an isolated temporary working directory, packages explicit text files with SHA-256 hashes and numbered lines, and writes `result.json`, `answer.txt`, `stdout.log`, and `stderr.log` under a fresh private output directory. It returns a result preview and explicit truncation metadata. The runner never applies patches.

## Built-in defaults

- OpenCode defaults to `opencode-go/deepseek-v4.1-flash`; its tools are denied by relay configuration.
- Antigravity defaults to `gemini-3.8-flash-low`, plan mode, and sandbox mode.
- Cursor defaults to `gemini-3.8-flash-low`, ask mode, enabled sandboxing, stdin input, and stream-JSON output. The bundled adapter targets locally verified `cursor-agent` version `2026.09.10-fd3934a`; its probe is `--help` and its model listing command is `models`. Low, medium, and high effort select the exact matching Gemini 3.8 Flash model ID without extra arguments.

Cursor's `--trust` flag is permitted only because Relay launches each run from a new temporary cwd containing the packaged `request.json` and stdin file instead of the source project workspace. This is a narrowly scoped adapter exception, not permission to trust a repository. Do not add a project workspace or `--force`, `--yolo`, `--auto-review`, or `--approve-mcps`, and do not generalize the exception to other adapters. The provider still inherits host permissions because the temporary cwd is not an OS sandbox.

These defaults depend on the user's installed CLIs, authentication, and subscription access. Credentials and global configuration are inherited from those CLIs. Local output may contain sensitive source material; the user manages artifact retention. Agent Relay does not provide an OS security sandbox or hook enforcement.
