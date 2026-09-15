from __future__ import annotations

import json
import shutil
import stat
import sys
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_release import validate_release


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
            "chaos-monkey",
            "delegate",
            "propose-patch",
            "relay-doctor",
            "second-opinion",
        }
        actual = {path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md")}
        self.assertEqual(actual, expected)

        adapters = {path.stem for path in (ROOT / "adapters").glob("*.json")}
        self.assertEqual(adapters, {"antigravity", "cursor", "opencode"})

    def test_release_tag_must_match_every_manifest(self) -> None:
        self.assertEqual(set(validate_release("v0.2.0", ROOT).values()), {"0.2.0"})

        with TemporaryDirectory() as directory:
            copy = Path(directory)
            for source in (".codex-plugin", ".claude-plugin"):
                shutil.copytree(ROOT / source, copy / source)
            shutil.copy2(ROOT / "pyproject.toml", copy / "pyproject.toml")
            manifest = json.loads((copy / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
            manifest["version"] = "9.9.9"
            (copy / ".codex-plugin" / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Codex manifest=9.9.9"):
                validate_release("v0.2.0", copy)


if __name__ == "__main__":
    unittest.main()
