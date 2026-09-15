# Contributing

Agent Relay keeps the runtime small and auditable. Production code uses the Python standard library, provider behavior lives in declarative adapters, and external output remains advisory.

Before opening a pull request:

1. Add behavior-focused tests using a fake CLI. Do not consume provider quota in the test suite.
2. Run `./scripts/check.sh`.
3. Update both plugin manifests and the marketplace version together for a release.
4. Document any new provider's input format, output format, model IDs, effort mappings, log paths, listener requirements, and permission behavior.

Do not commit credentials, provider logs, job snapshots, source corpora, or generated answer artifacts. New runtime dependencies require a concrete justification and should remain avoidable where possible.
