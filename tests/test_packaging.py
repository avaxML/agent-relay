from __future__ import annotations

import json
import stat
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_manifests_describe_the_same_release(self) -> None:
        codex = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        claude = json.loads((ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
        marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(codex["name"], "agent-relay")
        self.assertEqual(codex["name"], claude["name"])
        self.assertEqual(codex["version"], claude["version"])
        self.assertEqual(codex["version"], marketplace["plugins"][0]["version"])
        self.assertEqual(codex["version"], project["project"]["version"])
        self.assertEqual(marketplace["plugins"][0]["source"], ".")

        interface = codex["interface"]
        for field in ("composerIcon", "logo", "logoDark"):
            self.assertTrue((ROOT / interface[field]).is_file())
        for screenshot in interface["screenshots"]:
            self.assertTrue((ROOT / screenshot).is_file())

    def test_plugin_entrypoints_and_skills_are_complete(self) -> None:
        wrapper = ROOT / "bin/agent-relay"
        self.assertTrue(wrapper.stat().st_mode & stat.S_IXUSR)
        expected = {
            "add-cli-adapter",
            "background-tasks",
            "bulk-read",
            "delegate",
            "propose-patch",
            "relay-doctor",
            "second-opinion",
        }
        actual = {path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md")}
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
