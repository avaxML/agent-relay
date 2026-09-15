#!/usr/bin/env python3
"""Verify that one release tag matches every published version."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path


def release_versions(root: Path) -> dict[str, str]:
    codex = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    claude = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads((root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return {
        "Codex manifest": codex["version"],
        "Claude manifest": claude["version"],
        "Claude marketplace": marketplace["plugins"][0]["version"],
        "Python project": project["project"]["version"],
    }


def validate_release(tag: str, root: Path) -> dict[str, str]:
    expected = tag.removeprefix("v")
    versions = release_versions(root)
    mismatches = {name: version for name, version in versions.items() if version != expected}
    if mismatches:
        details = ", ".join(f"{name}={version}" for name, version in mismatches.items())
        raise ValueError(f"Release tag {tag} expects version {expected}; {details}")
    return versions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    versions = validate_release(args.tag, args.root)
    print(json.dumps({"tag": args.tag, "versions": versions}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
