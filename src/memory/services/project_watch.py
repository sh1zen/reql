"""Project watch orchestration for incremental compilation."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import sleep as default_sleep
from threading import Event
from typing import Callable, Iterator

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover - exercised in environments without optional package installs
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment]

from ..domain.timeutils import utcnow_iso
from .incremental_compilation import CompileProjectResult, IncrementalCompilationService


SleepFn = Callable[[float], None]


@dataclass(slots=True)
class ProjectWatchEvent:
    """One project watch polling result."""

    iteration: int
    checked_at: str
    project_path: str
    total_artifacts: int
    dirty_artifacts: int
    deleted_artifacts: int
    compiled: bool
    result: CompileProjectResult | None = None
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "iteration": self.iteration,
            "checked_at": self.checked_at,
            "project_path": self.project_path,
            "total_artifacts": self.total_artifacts,
            "dirty_artifacts": self.dirty_artifacts,
            "deleted_artifacts": self.deleted_artifacts,
            "compiled": self.compiled,
            "errors": list(self.errors),
            "result": self.result.to_dict() if self.result is not None else None,
        }


class ProjectWatchService:
    """Watch a project filesystem and compile only when fingerprints are dirty."""

    def __init__(self, incremental: IncrementalCompilationService) -> None:
        self.incremental = incremental

    def watch_path(
        self,
        path: str | Path,
        *,

        max_file_size_bytes: int = 10 * 1024 * 1024,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        cache_enabled: bool = True,
        parsing_options: dict[str, object] | None = None,
        interval_seconds: float = 0.5,
        debounce_seconds: float = 0.1,
        max_iterations: int | None = None,
        sleeper: SleepFn = default_sleep,
    ) -> Iterator[ProjectWatchEvent]:
        """Yield watch events until interrupted or ``max_iterations`` is reached."""
        if max_iterations is not None and max_iterations < 1:
            raise ValueError("max_iterations must be at least 1 when provided")
        if interval_seconds < 0:
            raise ValueError("interval_seconds cannot be negative")
        if debounce_seconds < 0:
            raise ValueError("debounce_seconds cannot be negative")
        if Observer is None:
            raise RuntimeError("watchdog is required for monitor mode; install the watchdog package")

        root = Path(path).expanduser().resolve(strict=False)
        change_event = Event()
        observer = Observer()
        observer.schedule(_WatchdogChangeHandler(change_event), str(root), recursive=True)
        observer.start()

        iteration = 1
        try:
            yield self._poll_once(
                root,
                iteration=iteration,

                max_file_size_bytes=max_file_size_bytes,
                include_patterns=include_patterns,
                exclude_patterns=exclude_patterns,
                cache_enabled=cache_enabled,
                parsing_options=parsing_options,
                debounce_seconds=0,
                sleeper=sleeper,
            )
            while max_iterations is None or iteration < max_iterations:
                timeout = interval_seconds if max_iterations is not None else None
                changed = change_event.wait(timeout)
                if not changed and max_iterations is None:
                    continue
                if changed and debounce_seconds > 0:
                    sleeper(debounce_seconds)
                if changed:
                    change_event.clear()
                iteration += 1
                yield self._poll_once(
                    root,
                    iteration=iteration,

                    max_file_size_bytes=max_file_size_bytes,
                    include_patterns=include_patterns,
                    exclude_patterns=exclude_patterns,
                    cache_enabled=cache_enabled,
                    parsing_options=parsing_options,
                    debounce_seconds=0,
                    sleeper=sleeper,
                )
        finally:
            observer.stop()
            observer.join(timeout=5)

    def poll_once(
        self,
        path: str | Path,
        *,

        max_file_size_bytes: int = 10 * 1024 * 1024,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        cache_enabled: bool = True,
        parsing_options: dict[str, object] | None = None,
    ) -> ProjectWatchEvent:
        return self._poll_once(
            path,
            iteration=1,

            max_file_size_bytes=max_file_size_bytes,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            cache_enabled=cache_enabled,
            parsing_options=parsing_options,
            debounce_seconds=0,
            sleeper=default_sleep,
        )

    def _poll_once(
        self,
        path: str | Path,
        *,
        iteration: int,

        max_file_size_bytes: int,
        include_patterns: list[str] | None,
        exclude_patterns: list[str] | None,
        cache_enabled: bool,
        parsing_options: dict[str, object] | None,
        debounce_seconds: float,
        sleeper: SleepFn,
    ) -> ProjectWatchEvent:
        if debounce_seconds > 0:
            sleeper(debounce_seconds)
        # A watch cycle must observe one coherent filesystem snapshot. The old
        # status-then-compile flow scanned twice and could both waste time and
        # compile a different state than the one it reported.
        result = self.incremental.compile_path(
            path,
            max_file_size_bytes=max_file_size_bytes,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            cache_enabled=cache_enabled,
            parsing_options=parsing_options,
        )
        dirty = len(result.dirty_set.changed_artifact_ids)
        deleted = len(result.dirty_set.deleted_artifact_ids)
        total = len(result.scan.artifacts)
        errors = tuple(result.run.errors)

        return ProjectWatchEvent(
            iteration=iteration,
            checked_at=utcnow_iso(),
            project_path=str(Path(path).expanduser().resolve(strict=False)),
            total_artifacts=total,
            dirty_artifacts=dirty,
            deleted_artifacts=deleted,
            compiled=bool(dirty or deleted),
            result=result,
            errors=errors,
        )


class _WatchdogChangeHandler(FileSystemEventHandler):
    def __init__(self, changed: Event) -> None:
        super().__init__()
        self.changed = changed

    def on_any_event(self, event: object) -> None:
        self.changed.set()
