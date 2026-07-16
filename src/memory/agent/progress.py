"""Progress reporting for potentially slow Agent Workspace CLI operations."""
from __future__ import annotations

from functools import wraps
import sys
import threading
import time
from types import TracebackType
from typing import Any, TextIO


class AgentCommandProgress:
    """Emit flush-safe progress without contaminating command stdout."""

    def __init__(
        self,
        label: str,
        *,
        enabled: bool = True,
        initial_delay_seconds: float = 2.0,
        interval_seconds: float = 5.0,
        late_threshold_seconds: float = 8.0,
        stream: TextIO | None = None,
    ) -> None:
        self.label = label
        self.enabled = enabled
        self.initial_delay_seconds = max(0.0, initial_delay_seconds)
        self.interval_seconds = max(0.01, interval_seconds)
        self.late_threshold_seconds = max(0.0, late_threshold_seconds)
        self.stream = stream
        self._started_at = 0.0
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "AgentCommandProgress":
        self._started_at = time.perf_counter()
        if not self.enabled:
            return self
        self._write("started")
        self._thread = threading.Thread(target=self._report_while_running, name="reql-agent-progress", daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_value, traceback
        elapsed = max(0.0, time.perf_counter() - self._started_at)
        self._done.set()
        if self._thread is not None:
            self._thread.join(timeout=0.2)
        if self.enabled:
            if exc_type is None:
                suffix = " (late completion; result is final)" if elapsed >= self.late_threshold_seconds else ""
                self._write(f"completed in {elapsed:.1f}s{suffix}")
            else:
                self._write(f"failed after {elapsed:.1f}s; no successful completion was reported")
        return False

    def _report_while_running(self) -> None:
        if self._done.wait(self.initial_delay_seconds):
            return
        while not self._done.is_set():
            elapsed = max(0.0, time.perf_counter() - self._started_at)
            self._write(f"still running after {elapsed:.1f}s; waiting for workspace storage or locks")
            if self._done.wait(self.interval_seconds):
                return

    def _write(self, message: str) -> None:
        stream = self.stream or sys.stderr
        print(f"[agent] {self.label}: {message}", file=stream, flush=True)


class ProgressingAgentWorkspace:
    """Wrap one CLI workspace call in a progress lifecycle."""

    def __init__(self, workspace: Any, *, label: str, enabled: bool = True) -> None:
        self._workspace = workspace
        self._label = label
        self._enabled = enabled

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._workspace, name)
        if not callable(target):
            return target

        @wraps(target)
        def invoke(*args: Any, **kwargs: Any) -> Any:
            with AgentCommandProgress(self._label, enabled=self._enabled):
                return target(*args, **kwargs)

        return invoke
