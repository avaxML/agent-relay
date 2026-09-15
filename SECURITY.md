# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub Security Advisories for this repository. Do not open a public issue for a suspected vulnerability.

## Trust boundaries

Agent Relay runs locally installed coding-agent CLIs with the invoking user's environment, credentials, filesystem permissions, and network access. Its temporary working directory, plan-mode flags, and prompt instructions are not an operating-system sandbox.

Adapter files are trusted executable configuration. Review an adapter before using it. Only send files authorized for the selected provider, and treat task snapshots, provider session history, stdout, stderr, and result artifacts as potentially sensitive.

Relay does not automatically apply generated patches. Verify source hashes, inspect the proposed diff, run `git apply --check`, and test changes before applying them.
