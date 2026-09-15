# Running CLIs from a restricted host

Relay artifacts and provider runtime files are separate. A writable job directory does not grant the provider CLI access to its log, cache, authentication, or session directories. Detached workers inherit the submitting process's sandbox restrictions; `submit` does not escape them.

The installed OpenCode CLI writes `~/.local/share/opencode/log/opencode.log` even for `run --help`. Antigravity starts a localhost listener and can write runtime files under `~/.gemini/antigravity-cli`; Relay already redirects its supported CLI log to the temporary workspace, which does not remove the listener requirement.

## Before invoking a provider

When these requirements are known to be blocked, use the host's approval mechanism for the necessary execution permissions before `run`, `submit`, or a provider probe. In Codex's shell tool this is `sandbox_permissions: require_escalated`, with a justification naming the required log/runtime access and localhost listener. The host decides whether to allow it. The plugin cannot grant permissions, and should not change global sandbox settings. Reuse authorization already granted within the host's rules.

`doctor` without `--probe` only checks executable discovery and adapter declarations. A successful help probe does not prove authentication, network access, or listener startup. Keep probes free of model inference unless a live test is needed and authorized.

## Recognizing and recovering from failures

- OpenCode can report `FileSystem.open`, `EPERM`, and `opencode.log` on stdout rather than stderr.
- Antigravity can report `listen tcp 127.0.0.1:0: bind: operation not permitted`.

Inspect both saved logs and the terminal job record. If they establish a startup permission failure, obtain the required host-approved scope and submit a new job. Preserve the failed ID and report its relationship to the replacement. Do not blindly resubmit jobs that may already have executed a model request. If approval is denied, report that specific denial and leave the job failed; do not attempt a permission bypass.

Do not redirect `XDG_DATA_HOME` merely to suppress OpenCode's log error: it can change credential and session discovery. Prefer a documented log-only override when available, but it cannot solve a blocked listener. Keep the provider's existing tool restrictions, including OpenCode's permission configuration and Antigravity's plan/sandbox options.

`status`, `wait`, `result`, and `cancel` operate on job records and generally need only access to that job directory. They do not launch another model request.
