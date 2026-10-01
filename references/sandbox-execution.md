# Running CLIs from a restricted host

Relay artifacts and provider runtime files are separate. A writable job directory does not grant the provider CLI access to its log, cache, authentication, or session directories. Detached workers inherit the submitting process's sandbox restrictions; `submit` does not escape them.

The installed OpenCode CLI writes `~/.local/share/opencode/log/opencode.log` even for `run --help`. Antigravity starts a localhost listener and can write runtime files under `~/.gemini/antigravity-cli`; Relay already redirects its supported CLI log to the temporary workspace, which does not remove the listener requirement. Antigravity's `--sandbox` flag restricts the agent's terminal commands inside the CLI. It does not grant the CLI permission to start its own listener inside a host sandbox.

The bundled Cursor adapter never passes `--trust`. It points `cursor-agent` at a stable Relay-owned empty provider workspace, selects ask mode, and enables Cursor's sandbox. The user must approve Cursor's normal trust prompt in that directory once before unattended runs. This does not make the provider an OS-sandboxed process: it inherits the caller's filesystem and network permissions. Never set the source project as Cursor's working directory or add approval flags.

## Before invoking a provider

When these requirements are known to be blocked, request the host's approved execution scope on the shell command that launches `run` or `submit`, before launching it. In Codex's shell tool, use `sandbox_permissions: require_escalated` with a justification naming the localhost listener and CLI runtime access. In Claude Code, retry the Relay Bash command outside Claude's sandbox through its `dangerouslyDisableSandbox` approval flow, if that host permits unsandboxed retries. The host decides whether to allow either action. Approval of `status`, `wait`, or `result` does not change the permissions of an already submitted worker. The plugin cannot grant permissions or change global sandbox settings. Reuse authorization already granted within the host's rules.

`doctor` without `--probe` only checks executable discovery and adapter declarations. A successful help probe does not prove authentication, network access, or listener startup. Keep probes free of model inference unless a live test is needed and authorized.

## Recognizing and recovering from failures

- OpenCode can report `FileSystem.open`, `EPERM`, and `opencode.log` on stdout rather than stderr.
- Antigravity can report `listen tcp 127.0.0.1:0: bind: operation not permitted`. Relay surfaces this as a host sandbox error in `result.json` when the CLI exits with that signature.

Inspect both saved logs and the terminal job record. If they establish a startup permission failure, obtain the required host-approved scope and launch a new run or submit a new job with a fresh output directory. Preserve the failed ID and report its relationship to the replacement. Do not blindly resubmit jobs that may already have executed a model request. If approval is denied, report that specific denial and leave the job failed; do not attempt a permission bypass.

Do not redirect `XDG_DATA_HOME` merely to suppress OpenCode's log error: it can change credential and session discovery. Prefer a documented log-only override when available, but it cannot solve a blocked listener. Keep the provider's existing tool restrictions, including OpenCode's permission configuration and Antigravity's plan/sandbox options.

See the [Antigravity headless flag reference](https://www.agy.dev/docs/cli/headless/#flag-reference), [Claude Code sandboxed Bash retry documentation](https://code.claude.com/docs/en/sandboxing#the-unsandboxed-retry-escape-hatch), and [Codex sandbox and approval documentation](https://learn.chatgpt.com/docs/agent-approvals-security).

`status`, `wait`, `result`, and `cancel` operate on job records and generally need only access to that job directory. They do not launch another model request.
