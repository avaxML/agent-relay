# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub Security Advisories for this repository. Do not open a public issue for a suspected vulnerability.

## Trust boundaries

Agent Relay runs locally installed coding-agent CLIs with the invoking user's environment, credentials, filesystem permissions, and network access. Its temporary working directory, plan-mode flags, and prompt instructions are not an operating-system sandbox.

Adapter files are trusted executable configuration. Review an adapter before using it. Only send files authorized for the selected provider, and treat task snapshots, provider session history, stdout, stderr, and result artifacts as potentially sensitive.

Relay does not automatically apply generated patches or tool-kind diffs. Verify source hashes, inspect the proposed diff, run `git apply --check`, and test changes before applying them.

The `probe` and `execute` kinds run a provider with tools enabled in a throwaway clone under the Relay state directory, never in the user's checkout. Relay captures changes with its own hardened git and reruns declared checks itself, so a provider's claims never decide the result. The clone is not an operating-system sandbox: a tool-enabled provider runs as the user and can read files outside the clone, reach the network, and, where its own sandbox allows, write outside the clone. Relay strips `GIT_*`, `SSH_AUTH_SOCK`, and secret-like variables from tool-kind processes, but cannot hide credentials stored on disk. See [ADR 0001](docs/adr/0001-tool-enabled-workers.md) and [sandbox execution](references/sandbox-execution.md).
