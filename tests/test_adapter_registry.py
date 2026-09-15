from __future__ import annotations

import concurrent.futures
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from providers import RelayError, list_adapters, load_adapter, registry_directory


def adapter_payload(name: str, executable: str = "fake-cli", default_model: str = "test-model") -> dict[str, object]:
    return {
        "name": name,
        "executable": executable,
        "default_model": default_model,
        "args": [],
        "input": "stdin",
        "output": "text",
        "env": {},
        "models_args": ["--models"],
        "probe_args": ["--probe"],
    }


class AdapterRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.registry = self.directory / "registry"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_adapter(self, name: str, **changes: object) -> Path:
        payload = adapter_payload(name)
        payload.update(changes)
        path = self.directory / f"source-{name}.json"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path

    def relay(self, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "relay.py"), *arguments],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_install_list_resolve_and_idempotent_remove(self) -> None:
        source = self.write_adapter("custom")
        installed = self.relay("adapter", "install", str(source), "--registry-dir", str(self.registry))
        self.assertEqual(installed.returncode, 0, installed.stdout)
        self.assertEqual(json.loads(installed.stdout)["status"], "installed")
        entry = self.registry / "custom.json"
        lock = self.registry / ".custom.lock"
        self.assertEqual(stat.S_IMODE(self.registry.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(entry.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

        source.unlink()
        self.assertEqual(load_adapter("custom", registry_dir=self.registry)["default_model"], "test-model")
        listing = self.relay("adapter", "list", "--registry-dir", str(self.registry))
        records = json.loads(listing.stdout)["adapters"]
        self.assertEqual(
            [(record["name"], record["source"]) for record in records],
            [("antigravity", "bundled"), ("custom", "registered"), ("opencode", "bundled")],
        )

        removed = self.relay("adapter", "remove", "custom", "--registry-dir", str(self.registry))
        absent = self.relay("adapter", "remove", "custom", "--registry-dir", str(self.registry))
        self.assertEqual(json.loads(removed.stdout)["status"], "removed")
        self.assertEqual(json.loads(absent.stdout)["status"], "absent")

    def test_changed_install_requires_replace_and_invalid_source_preserves_entry(self) -> None:
        source = self.write_adapter("custom")
        self.assertEqual(
            self.relay("adapter", "install", str(source), "--registry-dir", str(self.registry)).returncode,
            0,
        )
        unchanged = self.relay("adapter", "install", str(source), "--registry-dir", str(self.registry))
        self.assertEqual(json.loads(unchanged.stdout)["status"], "unchanged")

        source.write_text(json.dumps(adapter_payload("custom")), encoding="utf-8")
        reformatted = self.relay("adapter", "install", str(source), "--registry-dir", str(self.registry))
        self.assertEqual(json.loads(reformatted.stdout)["status"], "unchanged")

        source.write_text(json.dumps(adapter_payload("custom", default_model="replacement")), encoding="utf-8")
        rejected = self.relay("adapter", "install", str(source), "--registry-dir", str(self.registry))
        self.assertEqual(rejected.returncode, 1)
        self.assertEqual(load_adapter("custom", registry_dir=self.registry)["default_model"], "test-model")

        replaced = self.relay("adapter", "install", str(source), "--replace", "--registry-dir", str(self.registry))
        self.assertEqual(replaced.returncode, 0, replaced.stdout)
        self.assertEqual(load_adapter("custom", registry_dir=self.registry)["default_model"], "replacement")

        source.write_text("{", encoding="utf-8")
        invalid = self.relay("adapter", "install", str(source), "--replace", "--registry-dir", str(self.registry))
        self.assertEqual(invalid.returncode, 1)
        self.assertEqual(load_adapter("custom", registry_dir=self.registry)["default_model"], "replacement")

    def test_concurrent_changed_installs_allow_exactly_one_new_registration(self) -> None:
        models = [f"model-{index}" for index in range(8)]
        sources = []
        for model in models:
            source = self.directory / f"source-{model}.json"
            source.write_text(
                json.dumps(adapter_payload("concurrent", default_model=model), indent=2) + "\n",
                encoding="utf-8",
            )
            sources.append(source)

        start = threading.Barrier(len(sources))

        def install(source: Path) -> subprocess.CompletedProcess[str]:
            start.wait()
            return self.relay("adapter", "install", str(source), "--registry-dir", str(self.registry))

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(sources)) as executor:
            results = list(executor.map(install, sources))

        successes = [(model, result) for model, result in zip(models, results, strict=True) if result.returncode == 0]
        self.assertEqual(
            len(successes),
            1,
            [(result.returncode, result.stdout) for result in results],
        )
        winner, result = successes[0]
        self.assertEqual(json.loads(result.stdout)["status"], "installed")
        self.assertEqual(load_adapter("concurrent", registry_dir=self.registry)["default_model"], winner)

    def test_explicit_adapter_file_precedes_registered_adapter(self) -> None:
        registered = self.write_adapter("custom")
        self.assertEqual(
            self.relay("adapter", "install", str(registered), "--registry-dir", str(self.registry)).returncode,
            0,
        )
        explicit = self.directory / "explicit.json"
        explicit.write_text(json.dumps(adapter_payload("custom", default_model="explicit")), encoding="utf-8")
        self.assertEqual(load_adapter("custom", explicit, self.registry)["default_model"], "explicit")
        self.assertEqual(load_adapter("custom", registry_dir=self.registry)["default_model"], "test-model")

    def test_rejects_bundled_names_and_symlink_sources_entries_and_registries(self) -> None:
        bundled = self.write_adapter("opencode")
        collision = self.relay("adapter", "install", str(bundled), "--registry-dir", str(self.registry))
        self.assertEqual(collision.returncode, 1)
        self.assertIn("bundled", json.loads(collision.stdout)["error"])
        removal = self.relay("adapter", "remove", "opencode", "--registry-dir", str(self.registry))
        self.assertEqual(removal.returncode, 1)

        source = self.write_adapter("custom")
        linked_source = self.directory / "linked-source.json"
        linked_source.symlink_to(source)
        source_result = self.relay("adapter", "install", str(linked_source), "--registry-dir", str(self.registry))
        self.assertEqual(source_result.returncode, 1)
        self.assertIn("symlink", json.loads(source_result.stdout)["error"])

        real_registry = self.directory / "real-registry"
        real_registry.mkdir()
        linked_registry = self.directory / "linked-registry"
        linked_registry.symlink_to(real_registry, target_is_directory=True)
        registry_result = self.relay("adapter", "list", "--registry-dir", str(linked_registry))
        self.assertEqual(registry_result.returncode, 1)
        self.assertIn("symlink", json.loads(registry_result.stdout)["error"])

        self.registry.mkdir()
        (self.registry / "custom.json").symlink_to(source)
        with self.assertRaisesRegex(RelayError, "symlink"):
            load_adapter("custom", registry_dir=self.registry)
        with self.assertRaisesRegex(RelayError, "symlink"):
            list_adapters(self.registry)

        locked_source = self.write_adapter("locked")
        (self.registry / ".locked.lock").symlink_to(locked_source)
        locked_result = self.relay("adapter", "install", str(locked_source), "--registry-dir", str(self.registry))
        self.assertEqual(locked_result.returncode, 1)
        self.assertIn("symlink", json.loads(locked_result.stdout)["error"])

        shadow_registry = self.directory / "shadow-registry"
        shadow_registry.mkdir()
        (shadow_registry / "opencode.json").write_text(bundled.read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaisesRegex(RelayError, "shadows bundled"):
            load_adapter("opencode", registry_dir=shadow_registry)
        with self.assertRaisesRegex(RelayError, "shadows bundled"):
            list_adapters(shadow_registry)

    def test_registered_adapter_drives_validate_doctor_models_and_run(self) -> None:
        executable = self.directory / "fake-cli"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "if '--probe' in sys.argv: print('probe-ok')\n"
            "elif '--models' in sys.argv: print('model-a')\n"
            "else: print('answer:' + sys.stdin.read()[:4])\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        source = self.write_adapter("custom", executable=executable.name)
        environment = {**os.environ, "PATH": f"{self.directory}:{os.environ.get('PATH', '')}"}
        self.assertEqual(
            self.relay(
                "adapter", "install", str(source), "--registry-dir", str(self.registry), env=environment
            ).returncode,
            0,
        )

        validated = self.relay(
            "adapter", "validate", "custom", "--probe", "--registry-dir", str(self.registry), env=environment
        )
        self.assertEqual(validated.returncode, 0, validated.stdout)
        self.assertEqual(json.loads(validated.stdout)["output"], "probe-ok\n")
        doctor = self.relay("doctor", "--provider", "custom", "--registry-dir", str(self.registry), env=environment)
        models = self.relay("models", "--provider", "custom", "--registry-dir", str(self.registry), env=environment)
        self.assertEqual(doctor.returncode, 0, doctor.stdout)
        self.assertEqual(json.loads(models.stdout)["providers"][0]["output"], "model-a\n")

        task = self.directory / "task.txt"
        task.write_text("question", encoding="utf-8")
        project = self.directory / "project"
        project.mkdir()
        (project / "source.txt").write_text("source", encoding="utf-8")
        output = self.directory / "output"
        run = self.relay(
            "run",
            "--provider",
            "custom",
            "--registry-dir",
            str(self.registry),
            "--task-file",
            str(task),
            "--root",
            str(project),
            "--files",
            "source.txt",
            "--output",
            str(output),
            env=environment,
        )
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertEqual(json.loads(run.stdout)["answer"], 'answer:{"in')

    def test_validate_accepts_an_explicit_path_and_default_directory_uses_environment(self) -> None:
        source = self.write_adapter("custom")
        validated = self.relay("adapter", "validate", str(source), "--registry-dir", str(self.registry))
        self.assertEqual(validated.returncode, 0, validated.stdout)
        self.assertEqual(json.loads(validated.stdout)["source"], "explicit")

        configured = self.directory / "configured"
        with patch.dict(os.environ, {"AGENT_RELAY_ADAPTERS_DIR": str(configured), "XDG_CONFIG_HOME": ""}):
            self.assertEqual(registry_directory(), configured.resolve())
        xdg = self.directory / "xdg"
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=True):
            self.assertEqual(registry_directory(), (xdg / "agent-relay" / "adapters").resolve())


if __name__ == "__main__":
    unittest.main()
