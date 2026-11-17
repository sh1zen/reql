"""Protect owned lock cleanup under Windows metadata-reader contention."""
from __future__ import annotations

import json
import errno
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from memory.domain.exceptions import StorageError
from memory.storage.adapters.block_store import _ReaderLock, _StoreLock


def sharing_violation() -> PermissionError:
    """Model the filesystem boundary on platforms without Windows error codes."""
    error = PermissionError(13, "Metadata reader prevents lock deletion")
    error.winerror = 32
    return error


class StoreLockReleaseTests(unittest.TestCase):
    """Exercise both owners without changing their normal acquisition path."""

    def setUp(self) -> None:
        scratch = Path(".tmp")
        scratch.mkdir(exist_ok=True)
        self.directory = TemporaryDirectory(dir=scratch)
        self.addCleanup(self.directory.cleanup)
        self.target = Path(self.directory.name).resolve() / "graph.reql"

    def acquire(self, mode: str):
        """Acquire a writer or a published reader slot using its real owner."""
        writer = _StoreLock(self.target)
        if mode == "write":
            writer.acquire()
            return writer
        reader = _ReaderLock(self.target)
        reader.acquire(writer)
        return reader

    def test_transient_sharing_violation_retries_owned_writer_and_reader_release(self) -> None:
        unlink = Path.unlink
        for mode in ("write", "read"):
            with self.subTest(mode=mode):
                lock = self.acquire(mode)
                failures = []

                def transient(path, *args, **kwargs):
                    """Fail the first two deletes of this sidecar, then use the OS."""
                    if path == lock.lock_path and len(failures) < 2:
                        failures.append(sharing_violation())
                        raise failures[-1]
                    return unlink(path, *args, **kwargs)

                try:
                    with patch.object(Path, "unlink", new=transient), patch("memory.storage.adapters.block_store.time.sleep"):
                        lock.release()
                    self.assertFalse(lock.lock_path.exists())
                    self.assertFalse(lock.acquired)
                    self.assertEqual(len(failures), 2)
                finally:
                    lock.lock_path.unlink(missing_ok=True)

    def test_release_rechecks_token_before_retrying(self) -> None:
        unlink = Path.unlink
        for mode in ("write", "read"):
            with self.subTest(mode=mode):
                lock = self.acquire(mode)
                attempts = []

                def replace_owner(path, *args, **kwargs):
                    """Simulate sidecar replacement after the failed delete."""
                    if path == lock.lock_path:
                        attempts.append(path)
                        path.write_text(json.dumps({"token": "replacement-owner"}), encoding="utf-8")
                        raise sharing_violation()
                    return unlink(path, *args, **kwargs)

                try:
                    with patch.object(Path, "unlink", new=replace_owner), patch("memory.storage.adapters.block_store.time.sleep"):
                        lock.release()
                    self.assertEqual(json.loads(lock.lock_path.read_text())["token"], "replacement-owner")
                    self.assertEqual(len(attempts), 1)
                    self.assertFalse(lock.acquired)
                finally:
                    lock.lock_path.unlink(missing_ok=True)

    def test_persistent_sharing_violation_is_bounded_and_propagated(self) -> None:
        unlink = Path.unlink
        for mode in ("write", "read"):
            with self.subTest(mode=mode):
                lock = self.acquire(mode)
                error = sharing_violation()

                def busy(path, *args, **kwargs):
                    """A persistent peer handle must not be reported as released."""
                    if path == lock.lock_path:
                        raise error
                    return unlink(path, *args, **kwargs)

                try:
                    with patch.object(Path, "unlink", new=busy), patch("memory.storage.adapters.block_store.time.monotonic", side_effect=[0.0, 2.0]):
                        with self.assertRaises(PermissionError) as raised:
                            lock.release()
                    self.assertIs(raised.exception, error)
                    self.assertTrue(lock.lock_path.exists())
                finally:
                    lock.lock_path.unlink(missing_ok=True)

    def test_unrelated_access_error_is_not_retried_or_swallowed(self) -> None:
        unlink = Path.unlink
        for mode in ("write", "read"):
            with self.subTest(mode=mode):
                lock = self.acquire(mode)
                error = PermissionError(13, "Access denied")
                error.winerror = 5

                def denied(path, *args, **kwargs):
                    """Permanent access errors remain actionable to the caller."""
                    if path == lock.lock_path:
                        raise error
                    return unlink(path, *args, **kwargs)

                try:
                    with patch.object(Path, "unlink", new=denied), patch("memory.storage.adapters.block_store.time.sleep", side_effect=AssertionError("Unrelated error was retried")):
                        with self.assertRaises(PermissionError) as raised:
                            lock.release()
                    self.assertIs(raised.exception, error)
                finally:
                    lock.lock_path.unlink(missing_ok=True)

    def test_windows_delete_pending_creation_retries_within_acquisition_budget(self) -> None:
        open_file = os.open
        for mode in ("write", "read"):
            with self.subTest(mode=mode):
                writer = _StoreLock(self.target, timeout_seconds=1)
                lock = writer if mode == "write" else _ReaderLock(self.target, timeout_seconds=1)
                attempts = []

                def pending(path, *args, **kwargs):
                    """The CRT drops winerror for this transient native condition."""
                    if path == str(lock.lock_path) and len(attempts) < 2:
                        attempts.append(path)
                        raise PermissionError(errno.EACCES, "Delete pending", path)
                    return open_file(path, *args, **kwargs)

                try:
                    with patch("memory.storage.adapters.block_store.os.name", "nt"), patch.object(os, "open", new=pending), patch("memory.storage.adapters.block_store.time.sleep"):
                        if mode == "write":
                            lock.acquire()
                        else:
                            lock.acquire(writer)
                    self.assertTrue(lock.acquired)
                    self.assertEqual(len(attempts), 2)
                finally:
                    lock.release()

    def test_persistent_creation_denial_preserves_cause_after_budget(self) -> None:
        open_file = os.open
        for mode in ("write", "read"):
            with self.subTest(mode=mode):
                writer = _StoreLock(self.target, timeout_seconds=0)
                lock = writer if mode == "write" else _ReaderLock(self.target, timeout_seconds=0)
                error = PermissionError(errno.EACCES, "Access denied", str(lock.lock_path))

                def denied(path, *args, **kwargs):
                    if path == str(lock.lock_path):
                        raise error
                    return open_file(path, *args, **kwargs)

                with patch("memory.storage.adapters.block_store.os.name", "nt"), patch.object(os, "open", new=denied), patch("memory.storage.adapters.block_store.time.sleep", side_effect=AssertionError("Expired acquisition was retried")):
                    with self.assertRaises(StorageError) as raised:
                        if mode == "write":
                            lock.acquire()
                        else:
                            lock.acquire(writer)
                self.assertIs(raised.exception.__cause__, error)
                self.assertFalse(lock.acquired)

if __name__ == "__main__":
    unittest.main()
