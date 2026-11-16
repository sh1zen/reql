"""Protect owned lock cleanup under Windows metadata-reader contention."""
from __future__ import annotations

import json
import errno
import multiprocessing
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from memory.domain.exceptions import StorageError
from memory.storage.adapters.block_store import _ReaderLock, _StoreLock


def hold_metadata_reader(path: str, opened, release) -> None:
    """Keep a real Windows handle open until the owner encounters contention."""
    with Path(path).open("r", encoding="utf-8"):
        opened.set()
        if not release.wait(15):
            raise TimeoutError("Lock owner never signaled its sharing violation")


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

    def test_missing_and_foreign_sidecars_remain_safe(self) -> None:
        for mode in ("write", "read"):
            for missing in (True, False):
                with self.subTest(mode=mode, missing=missing):
                    lock = self.acquire(mode)
                    if missing:
                        lock.lock_path.unlink()
                    else:
                        lock.lock_path.write_text(json.dumps({"token": "another-owner"}), encoding="utf-8")
                    lock.release()
                    self.assertFalse(lock.acquired)
                    self.assertEqual(lock.lock_path.exists(), not missing)
                    lock.lock_path.unlink(missing_ok=True)

    def test_acquisition_tolerates_disappearing_writer_metadata(self) -> None:
        read_text = Path.read_text
        for mode in ("write", "read"):
            with self.subTest(mode=mode):
                owner = _StoreLock(self.target)
                owner.acquire()
                contender = _StoreLock(self.target, timeout_seconds=0)
                reader = _ReaderLock(self.target, timeout_seconds=0)
                vanished = []

                def disappear(path, *args, **kwargs):
                    """Release the old owner between the existence check and read."""
                    if path == owner.lock_path and not vanished:
                        vanished.append(path)
                        path.unlink()
                    return read_text(path, *args, **kwargs)

                try:
                    with patch.object(Path, "read_text", new=disappear):
                        if mode == "write":
                            contender.acquire()
                            self.assertTrue(contender.acquired)
                            self.assertEqual(json.loads(contender.lock_path.read_text())["token"], contender.token)
                        else:
                            reader.acquire(contender)
                            self.assertTrue(reader.acquired)
                            self.assertTrue(reader.lock_path.exists())
                    self.assertEqual(len(vanished), 1)
                finally:
                    reader.release()
                    contender.release()
                    owner.release()

    def test_timeout_diagnostics_tolerate_a_disappeared_writer(self) -> None:
        lock = _StoreLock(self.target)
        self.assertIn("REQL block store is locked for write", lock._locked_message())

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

    def test_reader_creation_recovers_if_an_empty_slot_directory_disappears(self) -> None:
        writer = _StoreLock(self.target)
        reader = _ReaderLock(self.target, timeout_seconds=1)
        open_file = os.open
        removed = []

        def remove_directory(path, *args, **kwargs):
            """A peer can clean up the directory before our exclusive open."""
            if path == str(reader.lock_path) and not removed:
                reader.readers_path.rmdir()
                removed.append(path)
            return open_file(path, *args, **kwargs)

        try:
            with patch.object(os, "open", new=remove_directory), patch("memory.storage.adapters.block_store.time.sleep"):
                reader.acquire(writer)
            self.assertEqual(len(removed), 1)
            self.assertTrue(reader.acquired)
            self.assertTrue(reader.lock_path.exists())
        finally:
            reader.release()

    def test_parent_directory_access_error_is_not_creation_contention(self) -> None:
        for mode in ("write", "read"):
            with self.subTest(mode=mode):
                writer = _StoreLock(self.target)
                lock = writer if mode == "write" else _ReaderLock(self.target)
                parent = lock.lock_path.parent
                error = PermissionError(errno.EACCES, "Parent directory denied", str(parent))
                with patch("memory.storage.adapters.block_store.os.name", "nt"), patch.object(Path, "mkdir", side_effect=error), patch("memory.storage.adapters.block_store.time.sleep", side_effect=AssertionError("Parent denial was retried")):
                    with self.assertRaises(StorageError) as raised:
                        if mode == "write":
                            lock.acquire()
                        else:
                            lock.acquire(writer)
                self.assertIs(raised.exception.__cause__, error)

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics require Windows")
    def test_real_windows_reader_handle_does_not_break_release(self) -> None:
        context = multiprocessing.get_context("spawn")
        unlink = Path.unlink
        for mode in ("write", "read"):
            with self.subTest(mode=mode):
                lock = self.acquire(mode)
                opened, release = context.Event(), context.Event()
                process = context.Process(target=hold_metadata_reader, args=(str(lock.lock_path), opened, release))
                process.start()
                violations = []

                def observe(path, *args, **kwargs):
                    """Observe the real OS error, then allow the peer to close."""
                    try:
                        return unlink(path, *args, **kwargs)
                    except PermissionError as error:
                        if path == lock.lock_path and error.winerror == 32:
                            violations.append(error)
                            release.set()
                        raise

                try:
                    self.assertTrue(opened.wait(15), "Metadata reader failed to open the lock")
                    with patch.object(Path, "unlink", new=observe):
                        lock.release()
                    self.assertTrue(violations, "Test did not encounter a real Windows sharing violation")
                    self.assertFalse(lock.lock_path.exists())
                    self.assertFalse(lock.acquired)
                finally:
                    release.set()
                    process.join(15)
                    if process.is_alive():
                        process.terminate()
                        process.join(5)
                    lock.lock_path.unlink(missing_ok=True)
                self.assertEqual(process.exitcode, 0)


if __name__ == "__main__":
    unittest.main()
