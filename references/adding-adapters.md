# Adding a CLI adapter

Agent Relay keeps provider wiring declarative. The runner currently has built-in `opencode` and `antigravity` adapters. An external adapter is a single JSON object passed with `--adapter-file`; it is selected for that invocation and does not modify the plugin.

Adapter files are trusted execution configuration: they select an executable, its arguments, and environment overrides. Do not load an adapter discovered inside untrusted source content. Keep custom adapters outside the installed plugin cache so reinstalling the plugin does not remove them.

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

All nine fields are required. `args` may use `{model}`, `{request_file}`, `{timeout}`, and `{workdir}`. Input transport is `file`, `stdin`, or `agy-stream`; output is `opencode-jsonl`, `agy-jsonl`, or `text`. `env` is a map of environment overrides; `models_args` and `probe_args` define the CLI's model-list and help probes. Keep the executable name or use an absolute path when PATH discovery is unreliable.

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

Resolve the plugin root as the directory two levels above the directory containing a skill's `SKILL.md`. Use absolute paths for the task file, repository root, output directory, and adapter file:

```sh
python3 <plugin-root>/scripts/relay.py run \
  --provider my-cli \
  --adapter-file /abs/path/my-cli.json \
  --model provider/model \
  --task-file /abs/path/task.txt \
  --root /abs/path/repository \
  --files src/module.py tests/test_module.py \
  --output /abs/path/fresh-output \
  --kind read
```

Use `doctor --provider my-cli --adapter-file /abs/path/my-cli.json --probe` and `models --provider my-cli --adapter-file /abs/path/my-cli.json` before a real task. The runner executes in an isolated temporary working directory, packages explicit text files with SHA-256 hashes and numbered lines, and writes `result.json`, `answer.txt`, `stdout.log`, and `stderr.log` under a fresh private output directory. A result preview and explicit truncation metadata are returned. The runner never applies patches.

## Built-in defaults

- OpenCode defaults to `opencode-go/deepseek-v4.1-flash`; its tools are denied by relay configuration.
- Antigravity defaults to `gemini-3.8-flash-low`, plan mode, and sandbox mode.

These defaults depend on the user's installed CLIs, authentication, and subscription access. Credentials and global configuration are inherited from those CLIs. Local output may contain sensitive source material; the user manages artifact retention. Agent Relay does not provide an OS security sandbox or hook enforcement.
