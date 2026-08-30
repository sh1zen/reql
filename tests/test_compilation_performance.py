from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from api import MemoryGraph
from memory.artifacts.scanner import MAX_SCAN_WORKERS, ProjectScanner, _scan_worker_count
from memory.services import incremental_compilation as incremental_module
from memory.services.project_watch import _WatchdogChangeHandler


class CompilationPerformanceBehaviorTests(unittest.TestCase):
    def test_scanner_uses_small_bounded_worker_pools(self) -> None:
        self.assertEqual(_scan_worker_count(0), 0)
        self.assertEqual(_scan_worker_count(1), 1)
        self.assertEqual(_scan_worker_count(198), 2)
        self.assertLessEqual(_scan_worker_count(10_000), MAX_SCAN_WORKERS)

    def test_scanner_sha256_detects_same_size_same_mtime_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "sample.py"
            source.write_text("value = 1\n", encoding="utf-8")
            first = ProjectScanner(use_default_ignores=False).scan(root).artifacts[0]
            stat = source.stat()

            source.write_text("value = 2\n", encoding="utf-8")
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            second = ProjectScanner(use_default_ignores=False).scan(root).artifacts[0]

            self.assertEqual(first.size_bytes, second.size_bytes)
            self.assertEqual(source.stat().st_mtime_ns, stat.st_mtime_ns)
            self.assertNotEqual(first.sha256, second.sha256)

    def test_css_surface_refresh_runs_once_per_changed_batch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "one.py").write_text("ONE = 1\n", encoding="utf-8")
            (root / "two.py").write_text("TWO = 2\n", encoding="utf-8")
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                with patch.object(
                    incremental_module,
                    "refresh_project_css_surface_findings",
                    wraps=incremental_module.refresh_project_css_surface_findings,
                ) as refresh:
                    graph.compile_project(root)
                    self.assertEqual(refresh.call_count, 1)
                    graph.compile_project(root)
                    self.assertEqual(refresh.call_count, 1)
            finally:
                graph.close()

    def test_watchdog_ignores_reql_internal_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            changed = Event()
            handler = _WatchdogChangeHandler(changed, ignored_paths=(root / ".reql",))

            handler.on_any_event(SimpleNamespace(src_path=str(root / ".reql" / "memory.reql.wal")))
            self.assertFalse(changed.is_set())

            handler.on_any_event(SimpleNamespace(src_path=str(root / "src" / "app.py")))
            self.assertTrue(changed.is_set())

            changed.clear()
            handler.on_any_event(
                SimpleNamespace(
                    src_path=str(root / ".reql" / "staged.py"),
                    dest_path=str(root / "src" / "restored.py"),
                )
            )
            self.assertTrue(changed.is_set())


if __name__ == "__main__":
    unittest.main()
