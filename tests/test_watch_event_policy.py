"""Filesystem watch events should represent source writes rather than observer activity."""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from argparse import Namespace
from contextlib import redirect_stdout
import io

from api import MemoryGraph
from mcp.tools import reql_watch_project
from memory.cli import _handle_detached_project_watch
from memory.config import load_config
from memory.diagnostics import PerformanceLogger
from memory.freshness import read_watch_state, watch_state_path

from memory.services.project_watch import _WatchdogChangeHandler


class FakeObserver:
    """Replace only the external OS observer while keeping real compile paths."""

    def schedule(self, handler, path, *, recursive):
        self.handler = handler

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def join(self, timeout):
        self.joined = True


class WatchEventPolicyTests(unittest.TestCase):
    """Exercise the handler with deterministic events at the watchdog boundary."""

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.changed = Event()
        self.on_change = Mock()
        self.handler = _WatchdogChangeHandler(self.changed, ignored_paths=(self.root / ".reql",),
                                              on_change=self.on_change)

    def event(self, kind: str, path: Path, *, directory: bool = False, destination: Path | None = None):
        """Use the same attributes supplied by watchdog without starting OS threads."""
        return SimpleNamespace(event_type=kind, src_path=str(path), is_directory=directory,
                               dest_path=str(destination) if destination else "")

    def test_read_only_events_and_parent_directory_notifications_are_ignored(self) -> None:
        """Compilation's own source reads must not schedule the next compilation."""
        for kind, directory in (("opened", False), ("closed_no_write", False), ("modified", True)):
            with self.subTest(kind=kind):
                self.handler.on_any_event(self.event(kind, self.root / "source.py", directory=directory))
                self.assertFalse(self.changed.is_set())
                self.on_change.assert_not_called()

    def test_source_writes_creates_deletes_and_moves_still_signal_changes(self) -> None:
        """The read-event filter must preserve all events that can change the project tree."""
        for kind, directory in (("modified", False), ("closed", False), ("created", False), ("deleted", False),
                                ("moved", False), ("created", True), ("deleted", True), ("moved", True)):
            with self.subTest(kind=kind, directory=directory):
                self.changed.clear()
                self.on_change.reset_mock()
                self.handler.on_any_event(self.event(kind, self.root / "source.py", directory=directory))
                self.assertTrue(self.changed.is_set())
                self.on_change.assert_called_once()

    def test_moves_across_ignored_boundary_preserve_source_changes(self) -> None:
        """Only moves with both endpoints inside operational paths are irrelevant."""
        for source, target, expected in (
            (self.root / ".reql" / "one", self.root / ".reql" / "two", False),
            (self.root / ".reql" / "one", self.root / "source.py", True),
            (self.root / "source.py", self.root / ".reql" / "one", True),
        ):
            with self.subTest(source=source, target=target):
                self.changed.clear()
                self.handler.on_any_event(self.event("moved", source, destination=target))
                self.assertEqual(self.changed.is_set(), expected)

    def test_operational_sidecars_and_temporary_files_are_ignored(self) -> None:
        """Custom storage and logging paths have the same policy as the default .reql tree."""
        storage = self.root / "runtime" / "catalog.reql"
        diagnostics = self.root / "runtime" / "profile.jsonl"
        handler = _WatchdogChangeHandler(self.changed, storage_path=storage, diagnostics_path=diagnostics)
        for path in (storage, storage.with_name(storage.name + ".wal"), storage.with_name(storage.name + ".tmp"),
                     storage.with_name(storage.name + ".lock"), storage.with_name(storage.name + ".watch.lock"),
                     storage.with_name(storage.name + ".usage.jsonl.lock"),
                     storage.with_name(storage.name + ".readers") / "reader.lock", watch_state_path(storage),
                     storage.with_name(storage.name + ".watch.json.abcdef.tmp"),
                     storage.with_name("." + storage.name + ".usage.jsonl.prune-abcdef"), diagnostics):
            with self.subTest(path=path):
                handler.on_any_event(self.event("modified", path))
                self.assertFalse(self.changed.is_set())
        for path in (self.root / "runtime" / "source.py", storage.with_name(storage.name + ".watch.json.notes.py")):
            with self.subTest(path=path):
                self.changed.clear()
                handler.on_any_event(self.event("modified", path))
                self.assertTrue(self.changed.is_set())

    def _project(self):
        """Create a real project with explicitly excluded operational output."""
        project = self.root / "project"
        project.mkdir()
        config_path = project / "reql.conf"
        config_path.write_text("scan:\n  exclude:\n    - ./runtime/\n", encoding="utf-8")
        (project / "source.py").write_text("def calculate_total():\n    return 1\n", encoding="utf-8")
        return project, project / "runtime" / "catalog.reql", load_config(config_path)

    def test_api_watch_uses_shared_resource_policy_and_releases_observer(self) -> None:
        """The public API retains its compiler and cleans up the observer on completion."""
        project, storage, config = self._project()
        profile = PerformanceLogger(project / "runtime" / "profile.jsonl")
        graph = MemoryGraph.open(storage, config=config, profile_logger=profile)
        self.addCleanup(graph.close)
        observer = FakeObserver()
        with patch("memory.services.project_watch.Observer", return_value=observer):
            events = list(graph.watch_project(project, exclude_patterns=config.scan.exclude, max_iterations=1))
        self.assertFalse(events[0].errors)
        self.assertTrue(observer.stopped and observer.joined)
        for path in (profile.path, storage.with_name(storage.name + ".tmp")):
            observer.handler.on_any_event(self.event("modified", path))
            self.assertFalse(observer.handler.changed.is_set())

    def test_cli_watch_uses_configured_resource_policy(self) -> None:
        """The CLI adapter excludes custom storage and diagnostics before stale-state callbacks."""
        project, storage, config = self._project()
        profile = PerformanceLogger(project / "runtime" / "profile.jsonl")
        args = Namespace(command="project", path=str(project), storage=str(storage), watch_iterations=1,
                         watch_interval=0, watch_debounce=0, max_file_size_mb=None)
        observer = FakeObserver()
        with patch("memory.services.project_watch.Observer", return_value=observer), redirect_stdout(io.StringIO()):
            code = _handle_detached_project_watch(args, config, profile)
        self.assertEqual(code, 0)
        for path in (storage, profile.path, watch_state_path(storage)):
            observer.handler.on_any_event(self.event("modified", path))
            self.assertFalse(observer.handler.changed.is_set())
        self.assertEqual(read_watch_state(storage)["status"], "current")

    def test_mcp_watch_uses_configured_resource_policy(self) -> None:
        """MCP must not mark freshly compiled output stale because its own sidecars changed."""
        project, storage, _ = self._project()
        observer = FakeObserver()
        with patch("mcp.tools.Observer", return_value=observer):
            result = reql_watch_project(storage_path=str(storage), path=str(project), max_iterations=1)
        self.assertFalse(result["summary"]["errors"])
        for path in (storage, watch_state_path(storage), storage.with_name(storage.name + ".watch.json.abcdef.tmp")):
            observer.handler.on_any_event(self.event("modified", path))
            self.assertFalse(observer.handler.changed.is_set())
        self.assertEqual(read_watch_state(storage)["status"], "current")


if __name__ == "__main__":
    unittest.main()
