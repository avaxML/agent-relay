# ADR 0001: Tool-enabled workers in throwaway clones

- Status: accepted
- Date: 2026-10-06
- Issue: [#16](https://github.com/avaxML/agent-relay/issues/16)

## Context

Every Relay provider used to run with tools denied, in a temporary directory, on a JSON bundle of files, and return text. A lead agent that wanted a probe run or a small change implemented had to rebuild the work itself from the provider's prose. Tool-enabled providers are useful, but they can run commands, write files, and change git state. Relay must decide where they run, what they may change, how their results are verified, and how the existing tool-free kinds stay tool-free.

## Decision

1. Two new kinds use a provider's tools mode. `probe` runs experiments and may only create untracked files. `execute` implements a bounded task and may only change paths matching its `--owns` globs. `read`, `review`, `patch`, and `chaos` are unchanged: their request bytes, argv, environment, and `schema_version: 1` results are pinned by a golden fixture captured from `origin/main`.
2. Tool runs happen in a throwaway local clone under the Relay state directory, never in the user's checkout and never in a linked `git worktree`.
3. Relay itself decides the outcome. It captures status and diff with its own hardened git, judges violations, and reruns the task's declared `--check` commands. The provider's answer is recorded as `provider_claims`, next to Relay's results, and never decides the status.
4. Each adapter declares its tools mode in an optional `tools` block (`args`, `env`, `pass_env`) that replaces the base invocation for tool kinds. Blanket approval flags (`--force`, `--yolo`, `--trust`, `--auto`, `--auto-review`, `--dangerously-skip-permissions`, `--approve-mcps`, including `=value` forms) are valid only inside that block. Validation rejects them in `args`, `models_args`, `probe_args`, and effort level args, for bundled and registered adapters, at install and at load. A provider without a `tools` block rejects `probe` and `execute` with "provider <name> has no verified tools mode".
5. `relay.py dispatch` distributes a closed manifest of tasks across providers equally. `collect` aggregates results, and `cleanup` removes clones. The `executor` skill lets a lead agent use the providers as subagents and keep verification for itself. Native host subagents are only a fallback for tasks Relay cannot place.

## Isolation

Relay pins `--ref` to a commit SHA when the task is prepared. Before any provider runs, it does the following.

- **Snapshot.** It fetches that commit into a private bare repository (`clones/<id>/repo.git`) and points its `HEAD` at the fetched branch. This is the only time Relay runs git against the user's repository: `rev-parse` to pin the ref, and the fetch source. No provider has run at that point.
- **Work tree.** It clones the provider's work tree (`clones/<id>/work`) from the private repository with `--no-local --no-hardlinks`, removes `origin`, and checks out the SHA detached. Without hardlinks, a provider cannot corrupt the user's or Relay's object store. Without `origin`, a plain `git push` has nowhere to go.
- **Index.** It builds Relay's own index (`clones/<id>/relay.index`) from the SHA.

After the provider exits, Relay never uses the work tree's `.git`.

- **Hardened git.** Status, `add`, and diff run with `--git-dir=repo.git --work-tree=work`, Relay's index, and these settings:
  - `-c core.hooksPath=/dev/null -c core.fsmonitor=false -c core.excludesFile=/dev/null -c core.autocrlf=false`;
  - `GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL=/dev/null`;
  - `--no-ext-diff --no-textconv --no-renames`;
  - no inherited `GIT_*` variables.

  Hooks, `core.fsmonitor`, filter drivers, external diff, or a `.git` file or symlink the provider plants in the clone therefore never run inside Relay.
- **`.git` metadata check.** Changes to the work tree's `.git` are detected in pure Python, by hashing its files before and after the run and reading symlinks without following them. Every path except `index`, `objects/`, `logs/`, `FETCH_HEAD` and `ORIG_HEAD` is watched, including `config`, `hooks/`, `info/`, `HEAD` and `refs/`. A `.git` that is no longer a directory also counts. Any such change is a violation for both kinds; a provider commit is caught because it moves `HEAD` or a ref.
- **Bounds.** Capture is bounded and does not follow symlinks.
  - Status entries are capped at 10,000.
  - The `lstat` sizes of changed and untracked regular files are capped at 8 MB.
  - The diff is capped by `execution.execute`'s log limit.

  An over-limit capture is an `error`, not a truncated `ok`.
- **Statuses.** `probe` reports `violation` for any tracked modification, deletion, or `.git` change. `execute` reports `violation` for any changed, added, or deleted path outside `--owns`, including the deleted side of a rename into owned paths, and for any `.git` change. `--owns` globs are relative POSIX patterns. `*`, `?`, and `[...]` match within one segment, and `**` matches whole segments. Absolute patterns and `.`, `..`, or `.git` segments are rejected. The same matcher checks ownership and rejects overlapping `owns` between `execute` tasks in one dispatch.
- **Order of a run.**
  1. `--setup` commands.
  2. The provider, under the timeout `resolve_timeout` returns.
  3. Capture.
  4. Relay's `--check` commands.

  Setup and checks go through `execution.execute` with `shlex.split` arguments, no shell, a 1 MB log cap, and their own `--check-timeout` (default 180 seconds). The provider timeout does not include clone setup, capture, or checks.
- **Clone lifetime.** `probe` clones are removed after the run. `execute` clones survive an `ok`, `violation`, or `check_failed` result until `relay.py cleanup`. Cancellation, timeout, and errors remove the clone and keep the logs.
- **Environment.** Tool-kind setup, provider, and check processes see `PWD` set to the clone. A live run showed OpenCode resolving its project from an inherited `PWD` and writing into the caller's directory; Relay's own check in the clone caught it as `check_failed`.

### Clone, not a linked worktree

A linked `git worktree` shares the user repository's hooks, config, and object store. A tool-enabled provider could plant a hook or config that runs later in the user's checkout. A throwaway clone shares nothing writable with the user's repository.

## Per-provider permission model

Each mode was settled by a live synthetic check on this machine (agy 1.3.0, opencode 2.0.24, cursor-agent 2026.10.01-e373342).

| Provider | Tools mode | What it constrains | What it does not |
| --- | --- | --- | --- |
| OpenCode | `run --standalone` with `OPENCODE_CONFIG_CONTENT` = deny `*`, allow `read list glob grep edit bash`, deny `external_directory webfetch websearch task` | OpenCode's own write tools cannot leave the project directory (`external_directory` denial observed) | `bash` is allowed, so a shell redirect wrote outside the clone in the live check |
| Cursor | `-p --workspace {clone} --trust --sandbox enabled` without `--mode` (agent mode) and without `--force` | With the clone under `~/.local/state`, Cursor's sandbox rejected writes outside the clone from both its write tool and its shell | Writes to `/private/tmp` are allowed by Cursor's sandbox. `--trust` leaves a per-path `~/.cursor/projects/<slug>/.workspace-trusted` marker; clone paths are fresh UUIDs, so a marker is never reused |
| Antigravity | `--mode accept-edits --sandbox --add-dir {clone} --dangerously-skip-permissions` | Antigravity's terminal sandbox, and the clone as the added workspace | Writes outside the clone were not proven blocked. The localhost listener still needs host approval |

Relay never edits `~/.cursor/cli-config.json`, `~/.gemini/antigravity-cli/settings.json`, or OpenCode's configuration.

The environment for tool-kind processes drops `GIT_*`, `SSH_AUTH_SOCK`, and names matching `TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|ACCESS_KEY|PRIVATE_KEY`. An adapter's `tools.pass_env` can exempt named variables. `result.json` records the removed names (never values) as `env_removed`. All three providers authenticated from their own credential stores with this filter in place.

## What Relay cannot stop

Tool modes are not an OS sandbox. A provider runs as the user, so Relay cannot prevent:
- reading files outside the clone, including credentials stored on disk;
- network egress;
- shell writes outside the clone where the provider's own sandbox allows them;
- daemons that escape the process group.

Relay's checks run code that the provider may have changed. That is inherent in rerunning `execute` checks; the checks still run in the clone with the filtered environment.

## Rejected alternatives

- **Linked `git worktree`:** shares hooks, config, and objects with the user's repository (see above).
- **Running in the user's checkout:** a provider's mistakes or planted hooks would land in real work; the lead could not separate provider changes from its own.
- **Blanket flags in `args`:** would make every existing kind able to use tools. Keeping them only in `tools` makes `read`, `review`, `patch`, and `chaos` tool-free by construction, and validation enforces it.
- **Self-applying patches:** Relay never applies a worker's diff to the user's checkout. The lead reads `changes.diff`, runs `git apply --check`, applies it, and reruns the checks in the real checkout.
- **Provider-reported checks:** the lead cannot see inside a provider's tool calls. A live OpenCode probe claimed success while its file was in the wrong directory, and Relay's own check caught it.
- **Cursor with one trusted parent directory:** trust is recorded per path, so trusting a parent does not cover a fresh clone. Editing `permissions.allow` in Cursor's global config was rejected because Relay never edits provider settings.
- **Antigravity relying on the user's `settings.json`:** fails headless and depends on settings Relay must not edit.
- **OpenCode `--auto`:** widens approval without confining anything.
- **An environment allowlist:** risks breaking undiscovered CLI authentication; a denylist plus `tools.pass_env` keeps authentication working and is recorded.

## Rules rewritten

| Before | After |
| --- | --- |
| "Never embed credentials or auto-approval flags." (`AGENTS.md`) | Never embed credentials. Auto-approval flags may appear only inside an adapter's live-verified `tools` block, and validation rejects them anywhere else. |
| "Do not make generated patches self-applying." (`AGENTS.md`) | Never apply a worker's changes to the user's checkout. Tool kinds change only a throwaway clone; the lead applies `changes.diff` after review. |
| Cursor "never passes `--trust` ... `--force`, `--yolo`" (`README.md`, `references/adding-adapters.md`, `references/sandbox-execution.md`, `skills/relay-doctor`, `skills/background-tasks`) | The tool-free Cursor invocation never passes `--trust`, `--force`, `--yolo`, `--auto-review`, or `--approve-mcps`. The tools mode passes `--trust` scoped to a fresh Relay clone and never `--force` or `--yolo`. |
| "External output remains advisory." (`CONTRIBUTING.md`) | Provider claims remain advisory. Relay's own capture and checks decide a tool run's status, and the lead verifies before applying anything. |
| "Never add auto-approval flags." (`skills/add-cli-adapter`, `skills/second-opinion`) | Never add auto-approval flags outside a live-verified `tools` block. |
| Relay "does not automatically apply generated patches" (`SECURITY.md`) | Kept, and extended to tool-kind diffs; adds the clone trust boundary and what it cannot stop. |
| chaos-monkey "does not run attacks, fault injection, tests" | Its read-only path still does not. Its new probe path runs bounded experiments in a throwaway clone through `--kind probe`. |

## Live verification

All runs used a throwaway fixture repository (tracked `app.py` returning 1, and `test_app.py`) under the session scratch directory, and `scripts/relay.py` from this branch. The fixture's `git status` and the `app.py` and `.git/index` mtimes were identical before and after every run.

| Provider (version) | Model, effort | Command | Observed |
| --- | --- | --- | --- |
| opencode (v2.0.24) | `opencode-go/deepseek-v4.1-flash`, default | `run --kind probe` (write `probe.txt` from `python3 -c 'print(7319)'`) with a Relay `--check` | `ok`; untracked `probe.txt`; check exit 0; provider reported its commands |
| opencode | same | `run --kind probe` asking for an edit to `app.py` | `ok` with no changes: the provider refused, citing the probe rule against modifying tracked files |
| opencode | same | `run --kind execute --owns app.py --check "python3 test_app.py"` | `ok`; diff only in `app.py` (`return 2`); check exit 0; clone kept until cleanup |
| cursor (2026.10.01-e373342) | `grok-4.7-high`, default | probe / probe-edit / execute as above | `ok` / refused (no changes) / `ok` with a diff only in `app.py` |
| antigravity (1.3.0) | `gemini-3.8-flash-high`, `--effort high` | probe / probe-edit / execute as above | `ok` / refused (no changes) / `ok` with a diff only in `app.py` |
| cursor | `grok-4.7-xhigh`, `--effort xhigh` | `run --kind read` | `ok`; `result.json` `model` `grok-4.7-xhigh`, `timeout` 600 |
| cursor | `composer-2.5`, no effort | `run --kind read --model composer-2.5` | `ok`; `timeout` 180 |
| opencode | first run, before the `PWD` fix | probe | Relay `check_failed`. OpenCode wrote `probe.txt` in the caller's directory because it resolved its project from `PWD`; fixed by setting `PWD` to the clone |

A probe that the task explicitly asked to modify `app.py` was also refused by all three providers. The violation path therefore has no live outcome, because providers comply with the probe bound. It is proven by the fake-CLI tests and their mutation checks.
