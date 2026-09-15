#!/usr/bin/env bash
set -euo pipefail

plugin_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$plugin_root"

uv sync --locked --dev
uv run ruff check scripts tests
uv run ruff format --check scripts tests
uv run mypy scripts
uv run python -m unittest discover -s tests -v

if command -v claude >/dev/null 2>&1; then
  claude plugin validate .
fi
