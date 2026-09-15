# Repository guidance

- Keep production code compatible with Python 3.11 and free of third-party runtime dependencies.
- Use `uv`; do not use `pip` directly.
- Keep provider-specific behavior in `adapters/` or a narrow decoder in `scripts/providers.py`.
- Never embed credentials or auto-approval flags.
- Do not make generated patches self-applying.
- Test transports with fake CLIs. Live model calls are optional manual checks using synthetic inputs.
- Keep Codex and Claude manifests aligned for release fields.
- Run `./scripts/check.sh` before committing.
