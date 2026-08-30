from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import MemoryGraph
from memory.artifacts.cache import artifact_cache_path
from memory.artifacts.options import CompilationOptions
from memory.config import load_effective_config
from memory.domain.exceptions import StorageError
from memory.storage.maintenance import clear_project_storage


class StorageMaintenanceTests(unittest.TestCase):
    def test_storage_clear_rebuilds_only_current_project_state(self) -> None:
        tmp_root = Path(".tmp-tests")
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as td:
            root = Path(td) / "project"
            root.mkdir()
            source = root / "module.py"
            source.write_text("def legacy_function():\n    return 'legacy'\n", encoding="utf-8")
            storage = root / ".reql" / "memory.reql"

            graph = MemoryGraph.open(storage)
            try:
                graph.compile_project(root)
                source.write_text("def current_function():\n    return 'current'\n", encoding="utf-8")
                graph.compile_project(root)
                graph.query_context("current_function")
                self.assertGreater(graph.store.count_nodes(statuses={"archived"}), 0)
                self.assertGreater(graph.store.count_nodes(node_types={"CompilationRun"}), 1)
            finally:
                graph.close()

            usage_path = storage.with_name(f"{storage.name}.usage.jsonl")
            self.assertTrue(usage_path.exists())

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "memory.cli",
                    "storage",
                    "clear",
                    str(root),
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(completed.stdout)

            self.assertEqual(payload["project_path"], str(root.resolve()))
            self.assertEqual(payload["archived_nodes_after"], 0)
            self.assertEqual(payload["files_changed"], payload["files_seen"])
            self.assertFalse(usage_path.exists())
            self.assertFalse(storage.with_name(f"{storage.name}.wal").exists())
            self.assertFalse(list(artifact_cache_path(root).parent.glob("artifact-cache.json.clear-backup-*")))

            rebuilt = MemoryGraph.open(storage, read_only=True)
            try:
                self.assertEqual(rebuilt.store.count_nodes(statuses={"archived", "deleted"}), 0)
                self.assertEqual(rebuilt.store.count_nodes(node_types={"CompilationRun"}), 1)
                self.assertEqual(rebuilt.store.count_nodes(node_types={"GraphDelta"}), 1)
                rendered = "\n".join(
                    f"{node.label or ''}\n{node.text or ''}" for node in rebuilt.store.all_nodes()
                )
                self.assertIn("current_function", rendered)
                self.assertNotIn("legacy_function", rendered)
            finally:
                rebuilt.close()

            cache_payload = json.loads(artifact_cache_path(root).read_text(encoding="utf-8"))
            entries = next(iter(cache_payload["projects"].values()))["entries"].values()
            self.assertTrue(entries)
            self.assertTrue(all(entry["status"] == "active" for entry in entries))

    def test_storage_clear_restores_cache_and_store_when_clean_build_fails(self) -> None:
        tmp_root = Path(".tmp-tests")
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
            storage = Path(td) / "memory.reql"
            storage.write_bytes(b"existing-store")
            cache_path = artifact_cache_path(root)
            cache_path.parent.mkdir(parents=True)
            cache_path.write_text('{"existing": true}\n', encoding="utf-8")
            config = load_effective_config(start_dir=root)

            with patch(
                "memory.storage.maintenance._compile_clean_store",
                side_effect=StorageError("simulated clean-build failure"),
            ):
                with self.assertRaisesRegex(StorageError, "simulated clean-build failure"):
                    clear_project_storage(
                        storage,
                        root,
                        config=config,
                        max_file_size_bytes=1024 * 1024,
                        parsing_options=CompilationOptions.from_config(config),
                    )

            self.assertEqual(storage.read_bytes(), b"existing-store")
            self.assertEqual(cache_path.read_text(encoding="utf-8"), '{"existing": true}\n')
            self.assertFalse(list(cache_path.parent.glob("artifact-cache.json.clear-backup-*")))
            self.assertFalse(storage.with_name(f"{storage.name}.lock").exists())


if __name__ == "__main__":
    unittest.main()
