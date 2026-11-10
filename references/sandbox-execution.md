# Running CLIs from a restricted host

Relay artifacts and provider runtime files are separate. A writable job directory does not grant the provider CLI access to its log, cache, authentication, or session directories. Detached workers inherit the submitting process's sandbox restrictions; `submit` does not escape them.

The installed OpenCode CLI writes `~/.local/share/opencode/log/opencode.log` even for `run --help`. Antigravity starts a localhost listener and can write runtime files under `~/.gemini/antigravity-cli`; Relay already redirects its supported CLI log to the temporary workspace, which does not remove the listener requirement. Antigravity's `--sandbox` flag restricts the agent's terminal commands inside the CLI. It does not grant the CLI permission to start its own listener inside a host sandbox.

The tool-free Cursor invocation never passes `--trust`. It points `cursor-agent` at a stable Relay-owned empty provider workspace, selects ask mode, and enables Cursor's sandbox. The user must approve Cursor's normal trust prompt in that directory once before unattended runs. Cursor's tools mode (used only by `probe` and `execute`) passes `--trust` scoped to a fresh Relay clone under the state directory, keeps `--sandbox enabled`, and never passes `--force` or `--yolo`. Neither mode makes the provider an OS-sandboxed process: it inherits the caller's filesystem and network permissions. Never set the source project as Cursor's working directory.

## Tool-enabled kinds

`probe` and `execute` run the adapter's `tools` invocation in a throwaway clone at `$AGENT_RELAY_CLONES_DIR`, `$XDG_STATE_HOME/agent-relay/clones`, or `~/.local/state/agent-relay/clones` (mode `0700`). The provider's working directory and `PWD` are the clone. Relay's request file, stdin, and provider logs stay in the output directory, outside the clone.

What the tools mode can reach:
- Everything the user can: files outside the clone (read always; write where the provider's own sandbox allows), the network, and on-disk credentials. OpenCode's shell wrote outside the clone in a live check; Cursor's sandbox blocked writes outside a clone under `$HOME` but allows `/private/tmp`; Antigravity's out-of-clone writes were not proven blocked.
- Its own provider state, such as Cursor's per-path trust marker under `~/.cursor/projects` and Antigravity's runtime files under `~/.gemini`.

What Relay checks:
- Every Relay git call after the provider uses a private bare repository and index outside the work tree, with hooks, fsmonitor, excludes, and system and global config disabled. Hooks or config planted in the clone never run inside Relay.
- Tracked changes (`probe`), paths outside `--owns` (`execute`), and any change to the clone's `.git` metadata are violations. Capture never follows symlinks and fails rather than truncating past its limits.
- Relay reruns every `--check` itself without a shell and records exit codes next to the provider's claims.
- Tool-kind processes run without `GIT_*`, `SSH_AUTH_SOCK`, or secret-like variables (an adapter may exempt names with `tools.pass_env`). The removed names are listed in `result.json`.

What Relay cannot stop: reads and network access outside the clone, writes outside it that the provider's sandbox permits, credential files on disk, and descendants that escape the worker's process group. Check commands run code the provider may have changed. Use tool kinds only on repositories and tasks you would let the provider work on directly, and review `changes.diff` before applying it.

Antigravity's tools mode still needs the host-approved localhost listener described below.

## Before invoking a provider

When these requirements are known to be blocked, request the host's approved execution scope on the shell command that launches `run` or `submit`, before launching it. In Codex's shell tool, use `sandbox_permissions: require_escalated` with a justification naming the localhost listener and CLI runtime access. In Claude Code, retry the Relay Bash command outside Claude's sandbox through its `dangerouslyDisableSandbox` approval flow, if that host permits unsandboxed retries. The host decides whether to allow either action. Approval of `status`, `wait`, or `result` does not change the permissions of an already submitted worker. The plugin cannot grant permissions or change global sandbox settings. Reuse authorization already granted within the host's rules.

`doctor` without `--probe` only checks executable discovery and adapter declarations. A successful help probe does not prove authentication, network access, or listener startup. Keep probes free of model inference unless a live test is needed and authorized.

## Recognizing and recovering from failures

- OpenCode can report `FileSystem.open`, `EPERM`, and `opencode.log` on stdout rather than stderr.
- Antigravity can report `listen tcp 127.0.0.1:0: bind: operation not permitted`. Relay surfaces this as a host sandbox error in `result.json` when the CLI exits with that signature.

Inspect both saved logs and the terminal job record. If they establish a startup permission failure, obtain the required host-approved scope and launch a new run or submit a new job with a fresh output directory. Preserve the failed ID and report its relationship to the replacement. Do not blindly resubmit jobs that may already have executed a model request. If approval is denied, report that specific denial and leave the job failed; do not attempt a permission bypass.

Do not redirect `XDG_DATA_HOME` merely to suppress OpenCode's log error: it can change credential and session discovery. Prefer a documented log-only override when available, but it cannot solve a blocked listener. Keep the provider's existing tool restrictions, including OpenCode's permission configuration and Antigravity's plan/sandbox options for the tool-free kinds, and use the adapter's `tools` block only through `probe` and `execute`.

See the [Antigravity headless flag reference](https://www.agy.dev/docs/cli/headless/#flag-reference), [Claude Code sandboxed Bash retry documentation](https://code.claude.com/docs/en/sandboxing#the-unsandboxed-retry-escape-hatch), and [Codex sandbox and approval documentation](https://learn.chatgpt.com/docs/agent-approvals-security).

`status`, `wait`, `result`, and `cancel` operate on job records and generally need only access to that job directory. They do not launch another model request.
