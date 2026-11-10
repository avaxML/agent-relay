"""Existing kinds stay byte-identical: request, stdin, argv and environment match origin/main."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from task_runner import run

GOLDEN = ROOT / "tests" / "fixtures" / "existing_kinds.json"
EXISTING_KINDS = ("read", "review", "patch", "chaos")
BUNDLED = ("antigravity", "cursor", "opencode")
RESPONSES = {
    "agy-jsonl": {"event": "result", "result": {"status": "SUCCESS", "response": "ok"}},
    "cursor-jsonl": {"type": "result", "subtype": "success", "is_error": False, "result": "ok"},
    "opencode-jsonl": {"type": "text", "part": {"text": "ok"}},
}
FAKE = """#!{python}
import json, os, pathlib, re, sys
argv = sys.argv[1:]
cwd = pathlib.Path.cwd()
request = None
for arg in argv:
    if arg.endswith("request.json") and pathlib.Path(arg).is_file():
        request = pathlib.Path(arg).read_text(encoding="utf-8")
record = {{
    "argv": [re.sub(r"/[^ ]*/agent-relay-[^/]+", "<WORKDIR>", arg) for arg in argv],
    "cwd_files": sorted(path.name for path in cwd.iterdir()),
    "stdin": sys.stdin.read(),
    "request_file": request,
    "env": {{key: os.environ.get(key) for key in ("OPENCODE_CONFIG_CONTENT",)}},
}}
pathlib.Path(os.environ["FAKE_RECORD"]).write_text(json.dumps(record), encoding="utf-8")
print({response})
"""


def record_invocations(directory: Path) -> dict[str, dict[str, object]]:
    """Run every bundled adapter and existing kind against a recording fake CLI."""
    project = directory / "project"
    project.mkdir()
    (project / "src.py").write_text("def answer():\n    return 7319\n", encoding="utf-8")
    task = directory / "task.txt"
    task.write_text("Where is the answer defined?", encoding="utf-8")
    bin_dir = directory / "bin"
    bin_dir.mkdir()
    workspaces = directory / "workspaces"
    records: dict[str, dict[str, object]] = {}
    for provider in BUNDLED:
        adapter = json.loads((ROOT / "adapters" / f"{provider}.json").read_text(encoding="utf-8"))
        script = bin_dir / adapter["executable"]
        script.write_text(
            FAKE.format(python=sys.executable, response=repr(json.dumps(RESPONSES[adapter["output"]]))),
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        for kind in EXISTING_KINDS:
            record = directory / f"{provider}-{kind}.json"
            args = argparse.Namespace(
                provider=provider,
                adapter_file=None,
                registry_dir=directory / "registry",
                model="test-model",
                effort=None,
                root=project,
                files=["src.py"],
                task_file=task,
                kind=kind,
                max_input_bytes=100000,
                output=directory / f"out-{provider}-{kind}",
                timeout=30,
                max_answer_chars=1000,
            )
            environment = {
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "FAKE_RECORD": str(record),
                "AGENT_RELAY_PROVIDER_WORKSPACES_DIR": str(workspaces),
            }
            with patch.dict(os.environ, environment):
                result = run(args)
            if result["status"] != "ok":
                raise AssertionError(f"{provider} {kind}: {result}")
            observed = json.loads(record.read_text(encoding="utf-8"))
            observed["argv"] = [arg.replace(str(workspaces.resolve()), "<WORKSPACES>") for arg in observed["argv"]]
            observed["result_keys"] = sorted(result)
            observed["schema_version"] = result["schema_version"]
            records[f"{provider}/{kind}"] = observed
    return records


class ExistingKindsTests(unittest.TestCase):
    def test_existing_kinds_match_origin_main_bytes(self) -> None:
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            observed = record_invocations(Path(temporary))
        self.assertEqual(sorted(observed), sorted(golden))
        for key, expected in golden.items():
            with self.subTest(case=key):
                self.assertEqual(observed[key], expected)


if __name__ == "__main__":
    if sys.argv[1:] == ["--write-golden"]:
        with tempfile.TemporaryDirectory() as temporary:
            records = record_invocations(Path(temporary))
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        unittest.main()
