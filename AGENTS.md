# Repository guidance

- Keep production code compatible with Python 3.11 and free of third-party runtime dependencies.
- Use `uv`; do not use `pip` directly.
- Keep provider-specific behavior in `adapters/` or a narrow decoder in `scripts/providers.py`.
- Never embed credentials. Auto-approval flags belong only in an adapter's live-verified `tools` block; validation rejects them anywhere else (see `docs/adr/0001-tool-enabled-workers.md`).
- Never apply a worker's changes to the user's checkout. Tool kinds change only a throwaway clone; the lead reviews `changes.diff` and applies it.
- Test transports with fake CLIs. Live model calls are optional manual checks using synthetic inputs.
- Keep Codex and Claude manifests aligned for release fields.
- Run `./scripts/check.sh` before committing.
