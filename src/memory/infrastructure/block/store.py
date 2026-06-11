"""Block-file backed property graph storage adapter.

The store keeps graph indexes in memory for deterministic local operations and
persists the graph as fixed-size compressed pages. Records for a node and its
ordinary neighborhood are packed close together; high-degree nodes spill their
relationships into dedicated dense-edge records so node pages stay small.
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import json
import math
import signal
import socket
import struct
import threading
import time
import uuid
import zlib
from typing import Any, Iterator, Sequence

from ...domain.constants import ACTIVE_STATUSES
from ...domain.exceptions import StorageError
from ...domain.models import MemoryEdge, MemoryNode
from ...domain.timeutils import utcnow_iso
from ...extraction.normalization import keyword_scores, tokenize

SCHEMA_VERSION = 2
DEFAULT_BLOCK_SIZE = 64 * 1024
DEFAULT_PAGE_CACHE_BLOCKS = 128
DEFAULT_DENSE_NODE_THRESHOLD = 1024
DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0
DEFAULT_AUTO_CHECKPOINT_WAL_BYTES = 8 * 1024 * 1024
DEFAULT_LEXICAL_TEXT_BUDGET = 4096

_FORMAT_NAME = "reql-block-graph"
_SUPERBLOCK_MAGIC = b"RQLSPB01"
_SUPERBLOCK_VERSION = 1
_SUPERBLOCK_HEADER_STRUCT = struct.Struct("<8sIIII32s")
_SUPERBLOCK_HEADER_SIZE = _SUPERBLOCK_HEADER_STRUCT.size
_BLOCK_MAGIC = b"RQLBLK01"
_HEADER_STRUCT = struct.Struct("<8sIIII12s")
_HEADER_SIZE = _HEADER_STRUCT.size
_FRAME_HEADER = struct.Struct("<I")
_BINARY_RECORD_MAGIC = b"RQLREC02"
_BINARY_RECORD_VERSION = 2
_BINARY_RECORD_HEADER = struct.Struct("<8sBBB")
_BINARY_FLAG_COMPRESSED = 1
_RECORD_PART_MAGIC = b"RQLPRT02"
_RECORD_PART_VERSION = 1
_RECORD_PART_HEADER = struct.Struct("<8sHII32s")
_BINARY_KIND_TO_ID = {
    "meta": 1,
    "node": 2,
    "edge": 3,
    "dense_edge": 4,
    "operation": 5,
    "root_index": 6,
    "tombstone": 7,
}
_BINARY_ID_TO_KIND = {value: key for key, value in _BINARY_KIND_TO_ID.items()}
_NULL_STRING_LENGTH = 0xFFFFFFFF
_BINARY_COMPRESSION_THRESHOLD = 512
_WAL_MAGIC = b"RQLWAL02"
_WAL_FRAME_HEADER = struct.Struct("<I32s")
_EncodedRecord = tuple[dict[str, Any], bytes]
_Location = dict[str, int]
_ScalarIndexValue = str | int | float | bool | None
_VOLATILE_RECORD_FIELDS = {"updated_at"}
_VOLATILE_PROPERTY_FIELDS = {"created_at", "updated_at"}


INDEXED_NODE_PROPERTIES = {
    "artifact_id",
    "artifact_type",
    "compiled_at",
    "finding_type",
    "fragment_type",
    "kind",
    "language",
    "module",
    "name",
    "project_id",
    "qualified_name",
    "relative_path",
    "root_path",
    "sha256",
    "severity",
    "symbol_name",
    "symbol_type",
}
INDEXED_EDGE_PROPERTIES = {"artifact_id", "community_id", "finding_type", "project_id", "run_id"}


def _root_index_count(root_index: dict[str, Any], key: str) -> int:
    value = root_index.get(key, 0)
    if isinstance(value, dict):
        return len(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _compact_space_map(space_map: dict[str, Any]) -> dict[str, Any]:
    blocks = space_map.get("blocks", [])
    free_lists = space_map.get("free_lists", {})
    if isinstance(blocks, list):
        block_count = len(blocks)
    else:
        try:
            block_count = int(blocks or 0)
        except (TypeError, ValueError):
            block_count = 0
    free_list_counts: dict[str, int] = {}
    for key in ("small", "medium", "large"):
        value = free_lists.get(key, []) if isinstance(free_lists, dict) else []
        if isinstance(value, list):
            free_list_counts[key] = len(value)
        else:
            try:
                free_list_counts[key] = int(value or 0)
            except (TypeError, ValueError):
                free_list_counts[key] = 0
    return {
        "block_capacity": int(space_map.get("block_capacity", 0) or 0),
        "free_bytes_total": int(space_map.get("free_bytes_total", 0) or 0),
        "blocks": block_count,
        "free_lists": free_list_counts,
    }


def _property_value_key(value: Any) -> str | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return None


def _scalar_property_items(properties: dict[str, Any], allowed: set[str]) -> Iterator[tuple[str, str]]:
    for key, value in properties.items():
        if key not in allowed:
            continue
        if isinstance(value, list):
            for item in value:
                encoded = _property_value_key(item)
                if encoded is not None:
                    yield key, encoded
            continue
        encoded = _property_value_key(value)
        if encoded is not None:
            yield key, encoded


def _tuple_map_to_rows(mapping: dict[tuple[Any, ...], Any]) -> list[list[Any]]:
    return [[*key, value] for key, value in sorted(mapping.items(), key=lambda item: tuple(str(part) for part in item[0]))]


def _rows_to_tuple_map(rows: object, key_size: int) -> dict[tuple[str, ...], Any]:
    out: dict[tuple[str, ...], Any] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, list) or len(row) != key_size + 1:
            continue
        key = tuple(str(part) for part in row[:key_size])
        out[key] = row[key_size]
    return out


def _set_map_to_rows(mapping: dict[tuple[Any, ...], set[str]]) -> list[list[Any]]:
    return [[*key, sorted(values)] for key, values in sorted(mapping.items(), key=lambda item: tuple(str(part) for part in item[0])) if values]


def _rows_to_set_map(rows: object, key_size: int) -> dict[tuple[str, ...], set[str]]:
    out: dict[tuple[str, ...], set[str]] = defaultdict(set)
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, list) or len(row) != key_size + 1 or not isinstance(row[key_size], list):
            continue
        key = tuple(str(part) for part in row[:key_size])
        out[key].update(str(item) for item in row[key_size])
    return out


def _dict_set_to_json(mapping: dict[str, set[str]]) -> dict[str, list[str]]:
    return {key: sorted(values) for key, values in sorted(mapping.items()) if values}


def _json_to_dict_set(raw: object) -> dict[str, set[str]]:
    if not isinstance(raw, dict):
        return defaultdict(set)
    out: dict[str, set[str]] = defaultdict(set)
    for key, values in raw.items():
        if isinstance(values, list):
            out[str(key)].update(str(value) for value in values)
    return out


class _PageCache:
    def __init__(self, limit: int = DEFAULT_PAGE_CACHE_BLOCKS) -> None:
        self.limit = max(1, limit)
        self._pages: OrderedDict[int, bytes] = OrderedDict()

    def get(self, block_id: int) -> bytes | None:
        page = self._pages.get(block_id)
        if page is not None:
            self._pages.move_to_end(block_id)
        return page

    def put(self, block_id: int, page: bytes) -> None:
        self._pages[block_id] = page
        self._pages.move_to_end(block_id)
        while len(self._pages) > self.limit:
            self._pages.popitem(last=False)

    def clear(self) -> None:
        self._pages.clear()


class _TransactionJournal:
    def __init__(self, store: "BlockGraphStore") -> None:
        self.nodes: dict[str, tuple[MemoryNode | None, _Location | None]] = {}
        self.edges: dict[str, tuple[MemoryEdge | None, _Location | None]] = {}
        self.operation_log_len = len(store._operation_log)
        self.usage_by_node: dict[str, dict[str, Any] | None] = {}
        self.meta = dict(store._meta)
        self.dirty = store._dirty
        self.generation_id = store._generation_id
        self.data_offset = store._data_offset
        self.root_index_offset = store._root_index_offset
        self.manifest = dict(store._manifest)
        self.root_index = dict(store._root_index)
        self.space_map = dict(store._space_map)
        self.pending_wal_len = len(store._pending_wal_records)


class _StoreLock:
    def __init__(self, target_path: Path, *, timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS) -> None:
        self.target_path = target_path
        self.lock_path = target_path.with_name(f"{target_path.name}.lock")
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self.token = uuid.uuid4().hex
        self.acquired = False

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self.lock_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if self._remove_stale_lock():
                    continue
                if time.monotonic() >= deadline:
                    raise StorageError(self._locked_message())
                time.sleep(0.05)
                continue
            except OSError as exc:
                raise StorageError(f"Cannot acquire REQL write lock for {self.target_path}: {exc}") from exc
            try:
                os.write(fd, json.dumps(self._payload(), sort_keys=True).encode("utf-8"))
            finally:
                os.close(fd)
            self.acquired = True
            return

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            payload = self._read_payload()
            if payload.get("token") == self.token:
                self.lock_path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self.acquired = False

    def _payload(self) -> dict[str, Any]:
        return {
            "format": "reql-store-lock-v1",
            "path": str(self.target_path),
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "token": self.token,
            "created_at": utcnow_iso(),
        }

    def _read_payload(self) -> dict[str, Any]:
        try:
            raw = self.lock_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise
        except OSError:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _remove_stale_lock(self) -> bool:
        payload = self._read_payload()
        host = str(payload.get("host") or "")
        pid = payload.get("pid")
        if host and host != socket.gethostname():
            return False
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            pid_int = -1
        if pid_int > 0 and _process_is_alive(pid_int):
            return False
        try:
            self.lock_path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def _locked_message(self) -> str:
        payload = self._read_payload()
        owner = ""
        if payload:
            owner = f" by pid {payload.get('pid')} on {payload.get('host')}"
        return f"REQL block store is locked for writing{owner}: {self.target_path}"


class _ReaderLock:
    def __init__(self, target_path: Path, *, timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS) -> None:
        self.target_path = target_path
        self.readers_path = target_path.with_name(f"{target_path.name}.readers")
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self.token = uuid.uuid4().hex
        self.lock_path = self.readers_path / f"{os.getpid()}-{self.token}.lock"
        self.acquired = False

    def acquire(self, writer_lock: _StoreLock) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            self._create_slot()
            if not writer_lock.lock_path.exists() or writer_lock._remove_stale_lock():
                self.acquired = True
                return
            self.release()
            if time.monotonic() >= deadline:
                raise StorageError(writer_lock._locked_message())
            time.sleep(0.05)

    def release(self) -> None:
        try:
            payload = self._read_payload()
            if payload.get("token") == self.token:
                self.lock_path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self.acquired = False
            self._remove_empty_readers_dir()

    def _create_slot(self) -> None:
        while True:
            try:
                self.readers_path.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                break
            except FileExistsError:
                self.token = uuid.uuid4().hex
                self.lock_path = self.readers_path / f"{os.getpid()}-{self.token}.lock"
            except OSError as exc:
                raise StorageError(f"Cannot acquire REQL read lock for {self.target_path}: {exc}") from exc
        try:
            os.write(fd, json.dumps(self._payload(), sort_keys=True).encode("utf-8"))
        finally:
            os.close(fd)

    def _payload(self) -> dict[str, Any]:
        return {
            "format": "reql-store-reader-lock-v1",
            "path": str(self.target_path),
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "token": self.token,
            "created_at": utcnow_iso(),
        }

    def _read_payload(self) -> dict[str, Any]:
        try:
            raw = self.lock_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise
        except OSError:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _remove_empty_readers_dir(self) -> None:
        try:
            self.readers_path.rmdir()
        except OSError:
            pass


class _StoreReadWriteLock:
    def __init__(self, target_path: Path, *, timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS) -> None:
        self.target_path = target_path
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self.writer = _StoreLock(target_path, timeout_seconds=self.timeout_seconds)
        self.reader = _ReaderLock(target_path, timeout_seconds=self.timeout_seconds)
        self.mode: str | None = None

    def acquire_read(self) -> None:
        self.reader.acquire(self.writer)
        self.mode = "read"

    def acquire_write(self) -> None:
        self.writer.acquire()
        try:
            self._wait_for_readers()
        except Exception:
            self.writer.release()
            raise
        self.mode = "write"

    def release(self) -> None:
        if self.mode == "read":
            self.reader.release()
        elif self.mode == "write":
            self.writer.release()
        self.mode = None

    def _wait_for_readers(self) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            active = self._active_reader_payloads()
            if not active:
                return
            if time.monotonic() >= deadline:
                owner = f" by pid {active[0].get('pid')} on {active[0].get('host')}" if active else ""
                raise StorageError(f"REQL block store is locked for reading{owner}: {self.target_path}")
            time.sleep(0.05)

    def _active_reader_payloads(self) -> list[dict[str, Any]]:
        if not self.reader.readers_path.exists():
            return []
        active: list[dict[str, Any]] = []
        for path in sorted(self.reader.readers_path.glob("*.lock")):
            payload = self._read_payload(path)
            if self._reader_is_stale(payload):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    active.append(payload)
                continue
            active.append(payload)
        try:
            self.reader.readers_path.rmdir()
        except OSError:
            pass
        return active

    def _read_payload(self, path: Path) -> dict[str, Any]:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _reader_is_stale(self, payload: dict[str, Any]) -> bool:
        host = str(payload.get("host") or "")
        pid = payload.get("pid")
        if host and host != socket.gethostname():
            return False
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            return True
        return pid_int <= 0 or not _process_is_alive(pid_int)


def _process_is_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_process_is_alive(pid: int) -> bool:
    try:
        import ctypes
    except ImportError:
        return True
    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return int(exit_code.value) == still_active
    finally:
        kernel32.CloseHandle(handle)


def _pack_string(value: Any) -> bytes:
    if value is None:
        return struct.pack("<I", _NULL_STRING_LENGTH)
    encoded = str(value).encode("utf-8")
    return struct.pack("<I", len(encoded)) + encoded


def _read_string(data: bytes, offset: int) -> tuple[str | None, int]:
    if offset + 4 > len(data):
        raise StorageError("Invalid REQL binary string length")
    (length,) = struct.unpack_from("<I", data, offset)
    offset += 4
    if length == _NULL_STRING_LENGTH:
        return None, offset
    end = offset + length
    if end > len(data):
        raise StorageError("Invalid REQL binary string payload")
    return data[offset:end].decode("utf-8"), end


def _pack_json(value: Any) -> bytes:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return struct.pack("<I", len(encoded)) + encoded


def _read_json(data: bytes, offset: int) -> tuple[Any, int]:
    if offset + 4 > len(data):
        raise StorageError("Invalid REQL binary JSON length")
    (length,) = struct.unpack_from("<I", data, offset)
    offset += 4
    end = offset + length
    if end > len(data):
        raise StorageError("Invalid REQL binary JSON payload")
    return json.loads(data[offset:end].decode("utf-8")), end


def _encode_node_payload(value: dict[str, Any]) -> bytes:
    body = bytearray()
    for key in (
        "id",
        "type",
        "label",
        "text",
        "canonical_key",
        "created_at",
        "updated_at",
        "last_activated_at",
        "last_used_at",
        "status",
    ):
        body.extend(_pack_string(value.get(key)))
    body.extend(
        struct.pack(
            "<7dqq",
            float(value.get("activation", 0.0)),
            float(value.get("base_activation", 0.0)),
            float(value.get("salience", 0.0)),
            float(value.get("confidence", 1.0)),
            float(value.get("stability", 0.5)),
            float(value.get("volatility", 0.5)),
            float(value.get("utility", 0.0)),
            int(value.get("usage_count", 0)),
            int(value.get("evidence_count", 0)),
        )
    )
    body.extend(_pack_json(value.get("properties", {})))
    return bytes(body)


def _decode_node_payload(data: bytes) -> dict[str, Any]:
    offset = 0
    values: dict[str, Any] = {}
    for key in (
        "id",
        "type",
        "label",
        "text",
        "canonical_key",
        "created_at",
        "updated_at",
        "last_activated_at",
        "last_used_at",
        "status",
    ):
        values[key], offset = _read_string(data, offset)
    size = struct.calcsize("<7dqq")
    if offset + size > len(data):
        raise StorageError("Invalid REQL binary node metrics")
    (
        values["activation"],
        values["base_activation"],
        values["salience"],
        values["confidence"],
        values["stability"],
        values["volatility"],
        values["utility"],
        values["usage_count"],
        values["evidence_count"],
    ) = struct.unpack_from("<7dqq", data, offset)
    offset += size
    values["properties"], offset = _read_json(data, offset)
    if offset != len(data):
        raise StorageError("Invalid trailing bytes in REQL binary node")
    return values


def _encode_edge_payload(value: dict[str, Any]) -> bytes:
    body = bytearray()
    for key in ("id", "from_id", "to_id", "type", "origin", "last_fired_at", "created_at", "updated_at"):
        body.extend(_pack_string(value.get(key)))
    body.extend(
        struct.pack(
            "<ddiq",
            float(value.get("weight", 1.0)),
            float(value.get("confidence", 1.0)),
            int(value.get("polarity", 1)),
            int(value.get("co_activation_count", 0)),
        )
    )
    body.extend(_pack_json(value.get("properties", {})))
    return bytes(body)


def _decode_edge_payload(data: bytes) -> dict[str, Any]:
    offset = 0
    values: dict[str, Any] = {}
    for key in ("id", "from_id", "to_id", "type", "origin", "last_fired_at", "created_at", "updated_at"):
        values[key], offset = _read_string(data, offset)
    size = struct.calcsize("<ddiq")
    if offset + size > len(data):
        raise StorageError("Invalid REQL binary edge metrics")
    (
        values["weight"],
        values["confidence"],
        values["polarity"],
        values["co_activation_count"],
    ) = struct.unpack_from("<ddiq", data, offset)
    offset += size
    try:
        values["properties"], offset = _read_json(data, offset)
    except Exception as exc:
        raise StorageError(f"Invalid REQL binary edge payload: {exc}") from exc
    if offset != len(data):
        raise StorageError("Invalid trailing bytes in REQL binary edge")
    return values


def _encode_record(record: dict[str, Any]) -> bytes:
    kind = str(record.get("kind", ""))
    kind_id = _BINARY_KIND_TO_ID.get(kind)
    if kind_id is None:
        raise StorageError(f"Unsupported REQL record kind: {kind}")
    value = record.get("value", {})
    if kind == "node":
        payload = _encode_node_payload(dict(value))
    elif kind in {"edge", "dense_edge"}:
        payload = _encode_edge_payload(dict(value))
    else:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    flags = 0
    body = payload
    if len(payload) >= _BINARY_COMPRESSION_THRESHOLD:
        compressed = zlib.compress(payload)
        if len(compressed) < len(payload):
            body = compressed
            flags |= _BINARY_FLAG_COMPRESSED
    return _BINARY_RECORD_HEADER.pack(_BINARY_RECORD_MAGIC, _BINARY_RECORD_VERSION, kind_id, flags) + body


def _encode_record_parts(payload: bytes, *, max_payload_size: int) -> list[bytes]:
    part_capacity = max_payload_size - _RECORD_PART_HEADER.size
    if part_capacity <= 0:
        raise StorageError("REQL block size is too small for large-record parts")
    checksum = hashlib.sha256(payload).digest()
    total_parts = max(1, math.ceil(len(payload) / part_capacity))
    return [
        _RECORD_PART_HEADER.pack(_RECORD_PART_MAGIC, _RECORD_PART_VERSION, index, total_parts, checksum)
        + payload[index * part_capacity : (index + 1) * part_capacity]
        for index in range(total_parts)
    ]


def _decode_record_part(payload: bytes) -> dict[str, Any]:
    if len(payload) < _RECORD_PART_HEADER.size:
        raise StorageError("Invalid REQL record part header")
    magic, version, index, total_parts, checksum = _RECORD_PART_HEADER.unpack(payload[: _RECORD_PART_HEADER.size])
    if magic != _RECORD_PART_MAGIC or version != _RECORD_PART_VERSION:
        raise StorageError("Unsupported REQL record part")
    if total_parts <= 0 or index >= total_parts:
        raise StorageError("Invalid REQL record part sequence")
    return {
        "kind": "record_part",
        "value": {
            "index": index,
            "total_parts": total_parts,
            "checksum": checksum,
            "data": payload[_RECORD_PART_HEADER.size :],
        },
    }


def _decode_record_parts(parts: Sequence[dict[str, Any]]) -> tuple[dict[str, Any], int, bool]:
    if not parts:
        raise StorageError("Missing REQL record parts")
    values = [dict(part.get("value", {})) for part in parts]
    total_parts = int(values[0].get("total_parts", 0))
    checksum = values[0].get("checksum")
    if total_parts != len(values):
        raise StorageError("Incomplete REQL record parts")
    values.sort(key=lambda item: int(item.get("index", -1)))
    for expected, value in enumerate(values):
        if int(value.get("index", -1)) != expected:
            raise StorageError("Out-of-order REQL record parts")
        if int(value.get("total_parts", 0)) != total_parts or value.get("checksum") != checksum:
            raise StorageError("Mismatched REQL record parts")
    payload = b"".join(bytes(value.get("data", b"")) for value in values)
    if hashlib.sha256(payload).digest() != checksum:
        raise StorageError("Invalid REQL record part checksum")
    return _decode_record_payload(payload)


def _decode_record_payload(payload: bytes) -> tuple[dict[str, Any], int, bool]:
    if payload.startswith(_RECORD_PART_MAGIC):
        return _decode_record_part(payload), len(payload), False
    if payload.startswith(_BINARY_RECORD_MAGIC):
        if len(payload) < _BINARY_RECORD_HEADER.size:
            raise StorageError("Invalid REQL binary record header")
        magic, version, kind_id, flags = _BINARY_RECORD_HEADER.unpack(payload[: _BINARY_RECORD_HEADER.size])
        if magic != _BINARY_RECORD_MAGIC or version != _BINARY_RECORD_VERSION:
            raise StorageError("Unsupported REQL binary record")
        kind = _BINARY_ID_TO_KIND.get(kind_id)
        if kind is None:
            raise StorageError("Unknown REQL binary record kind")
        body = payload[_BINARY_RECORD_HEADER.size :]
        compressed = bool(flags & _BINARY_FLAG_COMPRESSED)
        if compressed:
            try:
                body = zlib.decompress(body)
            except zlib.error as exc:
                raise StorageError("Invalid REQL compressed binary record") from exc
        if kind == "node":
            value = _decode_node_payload(body)
        elif kind in {"edge", "dense_edge"}:
            value = _decode_edge_payload(body)
        else:
            value = json.loads(body.decode("utf-8"))
        return {"kind": kind, "value": value}, len(body), compressed
    raise StorageError("Invalid REQL binary record magic")


class BlockGraphStore:
    """Persistent property graph store using fixed-size block files.

    The file format is intentionally dependency-light: every block is exactly
    ``block_size`` bytes and contains length-prefixed binary records with
    selective compression. The adapter rebuilds in-memory indexes on open, so
    routine graph operations do not require a database engine.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        create: bool = True,
        block_size: int = DEFAULT_BLOCK_SIZE,
        dense_node_threshold: int = DEFAULT_DENSE_NODE_THRESHOLD,
        page_cache_blocks: int = DEFAULT_PAGE_CACHE_BLOCKS,
        read_only: bool = False,
        lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        if block_size < 4096:
            raise ValueError("block_size must be at least 4096 bytes")
        self.path = Path(path)
        self._wal_path = self.path.with_name(f"{self.path.name}.wal")
        self._usage_journal_path = self.path.with_name(f"{self.path.name}.usage.jsonl")
        self.block_size = block_size
        self.dense_node_threshold = dense_node_threshold
        self.read_only = read_only
        self._page_cache = _PageCache(page_cache_blocks)
        self._transaction_depth = 0
        self._transaction_journals: list[_TransactionJournal] = []
        self._closed = False
        self._dirty = False
        self._lock: _StoreReadWriteLock | None = None
        self._lock_timeout_seconds = lock_timeout_seconds
        self._generation_id = 0
        self._data_offset = 0
        self._root_index_offset = 0
        self._manifest: dict[str, Any] = {}
        self._root_index: dict[str, Any] = {}
        self._space_map: dict[str, Any] = {}
        self._pending_wal_records: list[dict[str, Any]] = []

        self._nodes: dict[str, MemoryNode] = {}
        self._edges: dict[str, MemoryEdge] = {}
        self._node_locations: dict[str, _Location] = {}
        self._edge_locations: dict[str, _Location] = {}
        self._loaded_all_records = False
        self._node_key_index: dict[tuple[str, str], str] = {}
        self._edge_pattern_index: dict[tuple[str, str, str], str] = {}
        self._out_edges: dict[str, set[str]] = defaultdict(set)
        self._in_edges: dict[str, set[str]] = defaultdict(set)
        self._node_terms: dict[str, dict[str, float]] = defaultdict(dict)
        self._node_type_index: dict[tuple[str], set[str]] = defaultdict(set)
        self._node_status_index: dict[tuple[str], set[str]] = defaultdict(set)
        self._edge_type_index: dict[tuple[str], set[str]] = defaultdict(set)
        self._node_property_index: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._edge_property_index: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._operation_log: list[dict[str, Any]] = []
        self._usage_by_node: dict[str, dict[str, Any]] = defaultdict(dict)
        self._meta = {
            "format": _FORMAT_NAME,
            "schema_version": SCHEMA_VERSION,
            "block_size": self.block_size,
            "dense_node_threshold": self.dense_node_threshold,
        }

        if create and not self.read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = _StoreReadWriteLock(self.path, timeout_seconds=lock_timeout_seconds)
        if self.read_only:
            self._lock.acquire_read()
        else:
            self._lock.acquire_write()
        try:
            if self.path.exists() and self.path.stat().st_size > 0:
                self._load()
            elif self._wal_path.exists() and self._wal_path.stat().st_size > 0:
                replayed = self._replay_wal()
                if replayed:
                    self._dirty = False
                self._rebuild_indexes()
            elif create and not self.read_only:
                self.initialize()
            self._load_usage_journal()
        except Exception:
            self._release_lock()
            raise

    def close(self) -> None:
        if self._closed:
            return
        try:
            if not self.read_only and self._transaction_depth == 0 and self._pending_wal_records:
                self._append_wal_records(self._pending_wal_records)
                self._pending_wal_records = []
        finally:
            self._release_lock()
            self._closed = True

    def _release_lock(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    @contextmanager
    def _defer_keyboard_interrupt(self) -> Iterator[None]:
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        previous_handler = signal.getsignal(signal.SIGINT)
        interrupted = False

        def _handler(signum: int, frame: Any) -> None:
            nonlocal interrupted
            interrupted = True

        signal.signal(signal.SIGINT, _handler)
        try:
            yield
        finally:
            signal.signal(signal.SIGINT, previous_handler)
            if interrupted:
                raise KeyboardInterrupt

    @contextmanager
    def transaction(self) -> Iterator["BlockGraphStore"]:
        self._ensure_writable()
        journal = _TransactionJournal(self)
        self._transaction_depth += 1
        self._transaction_journals.append(journal)
        try:
            yield self
        except BaseException:
            self._transaction_journals.pop()
            self._restore_journal(journal)
            self._transaction_depth = max(0, self._transaction_depth - 1)
            raise
        else:
            self._transaction_journals.pop()
            self._transaction_depth = max(0, self._transaction_depth - 1)
            if self._transaction_depth == 0:
                self._commit()

    def initialize(self) -> None:
        self._ensure_writable()
        self._meta["schema_version"] = SCHEMA_VERSION
        self._meta["updated_at"] = utcnow_iso()
        self._dirty = True
        self._flush(force=True)

    def schema_version(self) -> int:
        return int(self._meta.get("schema_version", 0))

    def generation_id(self) -> int:
        return int(self._generation_id)

    def root_index_offset(self) -> int:
        return int(self._root_index_offset)

    def storage_manifest(self) -> dict[str, Any]:
        return dict(self._manifest)

    def compact_storage(self) -> dict[str, Any]:
        """Rewrite the current logical graph into a compact block file."""
        self._ensure_writable()
        before = self.inspect_storage()
        self._flush(force=True)
        after = self.inspect_storage()
        return {
            "path": str(self.path),
            "generation_id_before": before["generation_id"],
            "generation_id_after": after["generation_id"],
            "blocks_before": before["blocks"]["total"],
            "blocks_after": after["blocks"]["total"],
            "bytes_before": before["file_size"],
            "bytes_after": after["file_size"],
            "bytes_reclaimed": max(0, int(before["file_size"]) - int(after["file_size"])),
            "records_before": before["records"]["total"],
            "records_after": after["records"]["total"],
        }

    def checkpoint_if_needed(self, *, wal_bytes_threshold: int = DEFAULT_AUTO_CHECKPOINT_WAL_BYTES) -> dict[str, Any]:
        """Create a query-ready checkpoint when WAL replay would be expensive.

        The WAL remains the crash-recovery path, but large or WAL-only stores
        should not force every subsequent query process to replay the full log.
        """
        self._ensure_writable()
        wal_bytes = self._wal_path.stat().st_size if self._wal_path.exists() else 0
        base_missing = not self.path.exists() or self.path.stat().st_size == 0
        pending = len(self._pending_wal_records)
        should_checkpoint = base_missing or wal_bytes >= max(0, int(wal_bytes_threshold))
        if not should_checkpoint:
            return {
                "checkpointed": False,
                "reason": "below_threshold",
                "wal_bytes": wal_bytes,
                "threshold": int(wal_bytes_threshold),
                "base_missing": base_missing,
                "pending_wal_records": pending,
            }
        before_generation = self._generation_id
        self._flush(force=True)
        after_wal_bytes = self._wal_path.stat().st_size if self._wal_path.exists() else 0
        return {
            "checkpointed": True,
            "reason": "base_missing" if base_missing else "wal_threshold",
            "wal_bytes": wal_bytes,
            "wal_bytes_after": after_wal_bytes,
            "threshold": int(wal_bytes_threshold),
            "base_missing": base_missing,
            "generation_id_before": before_generation,
            "generation_id_after": self._generation_id,
            "pending_wal_records": pending,
        }

    def inspect_storage(self) -> dict[str, Any]:
        """Return physical block-file and in-memory index statistics."""
        physical = self._inspect_physical_storage()
        indexed_node_ids = set(self._node_locations) | set(self._nodes) | set(self._out_edges) | set(self._in_edges)
        dense_node_ids = sorted(
            node_id
            for node_id in indexed_node_ids
            if len(self._out_edges.get(node_id, set()) | self._in_edges.get(node_id, set())) >= self.dense_node_threshold
        )
        lexical_terms = len(self._node_terms)
        lexical_postings = sum(len(postings) for postings in self._node_terms.values())
        physical.update(
            {
                "generation_id": self._generation_id,
                "data_offset": self._data_offset,
                "root_index_offset": self._root_index_offset,
                "manifest": dict(self._manifest),
                "dense_nodes": {
                    "threshold": self.dense_node_threshold,
                    "count": len(dense_node_ids),
                    "ids": dense_node_ids[:50],
                },
                "index_stats": {
                    "nodes": len(self._node_locations) or len(self._nodes),
                    "edges": len(self._edge_locations) or len(self._edges),
                    "loaded_nodes": len(self._nodes),
                    "loaded_edges": len(self._edges),
                    "node_keys": len(self._node_key_index),
                    "edge_patterns": len(self._edge_pattern_index),
                    "out_adjacency_nodes": len(self._out_edges),
                    "in_adjacency_nodes": len(self._in_edges),
                    "lexical_terms": lexical_terms,
                    "lexical_postings": lexical_postings,
                },
            }
        )
        return physical

    def _commit(self, record: dict[str, Any] | None = None) -> None:
        self._ensure_writable()
        if record is not None:
            self._pending_wal_records.append(record)
        self._dirty = True
        if self._transaction_depth == 0:
            self._append_wal_records(self._pending_wal_records)
            self._pending_wal_records = []

    def _commit_many(self, records: Sequence[dict[str, Any]]) -> None:
        self._ensure_writable()
        if not records:
            return
        self._pending_wal_records.extend(records)
        self._dirty = True
        if self._transaction_depth == 0:
            self._append_wal_records(self._pending_wal_records)
            self._pending_wal_records = []

    def _ensure_writable(self) -> None:
        if self.read_only:
            raise StorageError(f"REQL block store is opened read-only: {self.path}")

    def _snapshot(self) -> dict[str, Any]:
        return {
            "nodes": dict(self._nodes),
            "edges": dict(self._edges),
            "node_locations": dict(self._node_locations),
            "edge_locations": dict(self._edge_locations),
            "loaded_all_records": self._loaded_all_records,
            "node_key_index": dict(self._node_key_index),
            "edge_pattern_index": dict(self._edge_pattern_index),
            "out_edges": {key: set(values) for key, values in self._out_edges.items()},
            "in_edges": {key: set(values) for key, values in self._in_edges.items()},
            "node_terms": {term: dict(postings) for term, postings in self._node_terms.items()},
            "node_type_index": {key: set(values) for key, values in self._node_type_index.items()},
            "node_status_index": {key: set(values) for key, values in self._node_status_index.items()},
            "edge_type_index": {key: set(values) for key, values in self._edge_type_index.items()},
            "node_property_index": {key: set(values) for key, values in self._node_property_index.items()},
            "edge_property_index": {key: set(values) for key, values in self._edge_property_index.items()},
            "operation_log": list(self._operation_log),
            "usage_by_node": {key: dict(value) for key, value in self._usage_by_node.items()},
            "meta": dict(self._meta),
            "dirty": self._dirty,
            "generation_id": self._generation_id,
            "data_offset": self._data_offset,
            "root_index_offset": self._root_index_offset,
            "manifest": dict(self._manifest),
            "root_index": dict(self._root_index),
            "space_map": dict(self._space_map),
            "pending_wal_records": list(self._pending_wal_records),
        }

    def _restore(self, snapshot: dict[str, Any]) -> None:
        self._nodes = snapshot["nodes"]
        self._edges = snapshot["edges"]
        self._node_locations = snapshot["node_locations"]
        self._edge_locations = snapshot["edge_locations"]
        self._loaded_all_records = bool(snapshot["loaded_all_records"])
        self._node_key_index = snapshot["node_key_index"]
        self._edge_pattern_index = snapshot["edge_pattern_index"]
        self._out_edges = defaultdict(set, snapshot["out_edges"])
        self._in_edges = defaultdict(set, snapshot["in_edges"])
        self._node_terms = defaultdict(dict, {term: dict(postings) for term, postings in snapshot["node_terms"].items()})
        self._node_type_index = defaultdict(set, snapshot["node_type_index"])
        self._node_status_index = defaultdict(set, snapshot["node_status_index"])
        self._edge_type_index = defaultdict(set, snapshot["edge_type_index"])
        self._node_property_index = defaultdict(set, snapshot["node_property_index"])
        self._edge_property_index = defaultdict(set, snapshot["edge_property_index"])
        self._operation_log = snapshot["operation_log"]
        self._usage_by_node = defaultdict(dict, {key: dict(value) for key, value in snapshot.get("usage_by_node", {}).items()})
        self._meta = snapshot["meta"]
        self._dirty = bool(snapshot.get("dirty", self._dirty))
        self._generation_id = int(snapshot.get("generation_id", self._generation_id))
        self._data_offset = int(snapshot.get("data_offset", self._data_offset))
        self._root_index_offset = int(snapshot.get("root_index_offset", self._root_index_offset))
        self._manifest = dict(snapshot.get("manifest", self._manifest))
        self._root_index = dict(snapshot.get("root_index", self._root_index))
        self._space_map = dict(snapshot.get("space_map", self._space_map))
        self._pending_wal_records = [dict(record) for record in snapshot.get("pending_wal_records", self._pending_wal_records)]

    def _restore_journal(self, journal: _TransactionJournal) -> None:
        self._materialize_all_records()
        for node_id, (node, location) in journal.nodes.items():
            if node is None:
                self._nodes.pop(node_id, None)
            else:
                self._nodes[node_id] = self._clone_node(node)
            if location is None:
                self._node_locations.pop(node_id, None)
            else:
                self._node_locations[node_id] = dict(location)
        for edge_id, (edge, location) in journal.edges.items():
            if edge is None:
                self._edges.pop(edge_id, None)
            else:
                self._edges[edge_id] = self._clone_edge(edge)
            if location is None:
                self._edge_locations.pop(edge_id, None)
            else:
                self._edge_locations[edge_id] = dict(location)
        del self._operation_log[journal.operation_log_len :]
        for node_id, usage in journal.usage_by_node.items():
            if usage is None:
                self._usage_by_node.pop(node_id, None)
            else:
                self._usage_by_node[node_id] = dict(usage)
        self._meta = dict(journal.meta)
        self._dirty = bool(journal.dirty)
        self._generation_id = int(journal.generation_id)
        self._data_offset = int(journal.data_offset)
        self._root_index_offset = int(journal.root_index_offset)
        self._manifest = dict(journal.manifest)
        self._root_index = dict(journal.root_index)
        self._space_map = dict(journal.space_map)
        del self._pending_wal_records[journal.pending_wal_len :]
        self._rebuild_indexes()

    def _journal_node_before_change(self, node_id: str) -> None:
        if not self._transaction_journals:
            return
        node = self._nodes.get(node_id)
        if node is None and node_id in self._node_locations:
            node = self._load_node_from_location(node_id)
        location = self._node_locations.get(node_id)
        for journal in self._transaction_journals:
            if node_id not in journal.nodes:
                journal.nodes[node_id] = (self._clone_node(node) if node is not None else None, dict(location) if location is not None else None)

    def _journal_edge_before_change(self, edge_id: str) -> None:
        if not self._transaction_journals:
            return
        edge = self._edges.get(edge_id)
        if edge is None and edge_id in self._edge_locations:
            edge = self._load_edge_from_location(edge_id)
        location = self._edge_locations.get(edge_id)
        for journal in self._transaction_journals:
            if edge_id not in journal.edges:
                journal.edges[edge_id] = (self._clone_edge(edge) if edge is not None else None, dict(location) if location is not None else None)

    def _journal_usage_before_change(self, node_id: str) -> None:
        if not self._transaction_journals:
            return
        usage = self._usage_by_node.get(node_id)
        for journal in self._transaction_journals:
            if node_id not in journal.usage_by_node:
                journal.usage_by_node[node_id] = dict(usage) if usage is not None else None

    @staticmethod
    def _clone_node(node: MemoryNode) -> MemoryNode:
        return MemoryNode.from_dict(node.to_dict())

    @staticmethod
    def _clone_edge(edge: MemoryEdge) -> MemoryEdge:
        return MemoryEdge.from_dict(edge.to_dict())

    def _load(self) -> None:
        self._nodes.clear()
        self._edges.clear()
        self._node_locations.clear()
        self._edge_locations.clear()
        self._loaded_all_records = False
        self._operation_log.clear()
        self._usage_by_node.clear()
        self._root_index = {}
        self._space_map = {}
        self._pending_wal_records = []
        self._manifest = {}
        file_size = self.path.stat().st_size
        with self.path.open("rb") as fh:
            prefix = fh.read(_SUPERBLOCK_HEADER_SIZE)
        if len(prefix) < 8 or prefix[:8] != _SUPERBLOCK_MAGIC:
            raise StorageError(f"Invalid REQL storage manifest for {self.path}")
        self._load_manifested(file_size, prefix)
        replayed = self._replay_wal()
        if replayed:
            self._dirty = False

    def _load_manifested(self, file_size: int, header_bytes: bytes) -> None:
        if len(header_bytes) != _SUPERBLOCK_HEADER_SIZE:
            raise StorageError(f"Cannot read complete REQL superblock header from {self.path}")
        magic, manifest_version, schema_version, block_size, manifest_length, manifest_checksum = _SUPERBLOCK_HEADER_STRUCT.unpack(
            header_bytes
        )
        if magic != _SUPERBLOCK_MAGIC or manifest_version != _SUPERBLOCK_VERSION:
            raise StorageError(f"Unsupported REQL storage manifest for {self.path}")
        if schema_version != SCHEMA_VERSION:
            if schema_version < SCHEMA_VERSION:
                raise StorageError(f"Unsupported REQL schema version {schema_version} for {self.path}")
            raise StorageError(f"Unsupported future REQL schema version {schema_version} for {self.path}")
        if block_size < 4096:
            raise StorageError(f"Invalid REQL block size {block_size} for {self.path}")
        if self.block_size != DEFAULT_BLOCK_SIZE and self.block_size != block_size:
            raise StorageError(f"REQL block size mismatch for {self.path}: expected {self.block_size}, found {block_size}")
        self.block_size = block_size
        if manifest_length <= 0 or manifest_length > self.block_size - _SUPERBLOCK_HEADER_SIZE:
            raise StorageError(f"Invalid REQL manifest length for {self.path}")
        with self.path.open("rb") as fh:
            fh.seek(_SUPERBLOCK_HEADER_SIZE)
            manifest_bytes = fh.read(manifest_length)
        if len(manifest_bytes) != manifest_length:
            raise StorageError(f"Cannot read complete REQL manifest from {self.path}")
        if hashlib.sha256(manifest_bytes).digest() != manifest_checksum:
            raise StorageError(f"Invalid REQL manifest checksum for {self.path}")
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StorageError(f"Invalid REQL manifest JSON for {self.path}") from exc
        self._validate_manifest(manifest, file_size)
        self._manifest = dict(manifest)
        self._generation_id = int(manifest["generation_id"])
        self._root_index_offset = int(manifest["root_index_offset"])
        self._data_offset = int(manifest.get("data_offset", self.block_size))
        self._load_root_index()

    def _validate_manifest(self, manifest: dict[str, Any], file_size: int) -> None:
        required = {
            "format",
            "manifest_version",
            "schema_version",
            "block_size",
            "data_offset",
            "root_index_offset",
            "generation_id",
            "data_block_count",
            "checksum",
            "record_codec",
        }
        missing = sorted(required.difference(manifest))
        if missing:
            raise StorageError(f"REQL manifest missing fields for {self.path}: {', '.join(missing)}")
        if manifest["format"] != _FORMAT_NAME:
            raise StorageError(f"Invalid REQL format in manifest for {self.path}")
        manifest_version = self._manifest_int(manifest, "manifest_version")
        schema_version = self._manifest_int(manifest, "schema_version")
        block_size = self._manifest_int(manifest, "block_size")
        data_offset = self._manifest_int(manifest, "data_offset")
        root_index_offset = self._manifest_int(manifest, "root_index_offset")
        data_block_count = self._manifest_int(manifest, "data_block_count")
        generation_id = self._manifest_int(manifest, "generation_id")
        if manifest_version != _SUPERBLOCK_VERSION:
            raise StorageError(f"Unsupported REQL manifest version {manifest['manifest_version']} for {self.path}")
        if schema_version != SCHEMA_VERSION:
            if schema_version < SCHEMA_VERSION:
                raise StorageError(f"Unsupported REQL schema version {manifest['schema_version']} for {self.path}")
            raise StorageError(f"Unsupported future REQL schema version {manifest['schema_version']} for {self.path}")
        if block_size != self.block_size:
            raise StorageError(f"REQL block size mismatch in manifest for {self.path}")
        if data_offset < self.block_size or data_offset % self.block_size != 0:
            raise StorageError(f"Invalid REQL data offset for {self.path}")
        if root_index_offset < data_offset:
            raise StorageError(f"Invalid REQL root index offset for {self.path}")
        if data_block_count < 0:
            raise StorageError(f"Invalid REQL data block count for {self.path}")
        if file_size != data_offset + (data_block_count * self.block_size):
            raise StorageError(f"Invalid REQL block store size for {self.path}")
        if generation_id < 0:
            raise StorageError(f"Invalid REQL generation id for {self.path}")
        if manifest.get("checksum_algorithm", "sha256") != "sha256":
            raise StorageError(f"Unsupported REQL checksum algorithm for {self.path}")
        if manifest.get("record_codec") != "binary-v2":
            raise StorageError(f"Unsupported REQL record codec for {self.path}")
        checksum = str(manifest["checksum"])
        if len(checksum) != 64 or any(ch not in "0123456789abcdef" for ch in checksum):
            raise StorageError(f"Invalid REQL data checksum for {self.path}")

    def _manifest_int(self, manifest: dict[str, Any], field: str) -> int:
        try:
            return int(manifest[field])
        except (TypeError, ValueError) as exc:
            raise StorageError(f"Invalid REQL manifest field {field} for {self.path}") from exc

    def _validate_data_checksum(self, data_offset: int, expected: str) -> None:
        digest = hashlib.sha256()
        with self.path.open("rb") as fh:
            fh.seek(data_offset)
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise StorageError(f"Invalid REQL data checksum for {self.path}")

    def _load_blocks(self, data_offset: int, block_count: int) -> None:
        pending_parts: list[dict[str, Any]] = []
        for block_id in range(block_count):
            page = self._read_block(block_id, data_offset=data_offset)
            magic, version, block_size, current_id, used, _reserved = _HEADER_STRUCT.unpack(page[:_HEADER_SIZE])
            if magic != _BLOCK_MAGIC or version != SCHEMA_VERSION or block_size != self.block_size or current_id != block_id:
                raise StorageError(f"Invalid REQL block header at block {block_id}")
            offset = _HEADER_SIZE
            end = min(self.block_size, _HEADER_SIZE + used)
            while offset + _FRAME_HEADER.size <= end:
                (length,) = _FRAME_HEADER.unpack(page[offset : offset + _FRAME_HEADER.size])
                offset += _FRAME_HEADER.size
                if length == 0:
                    break
                payload = page[offset : offset + length]
                offset += length
                record, _uncompressed_length, _compressed = _decode_record_payload(payload)
                if record.get("kind") == "record_part":
                    pending_parts.append(record)
                    expected_parts = int(dict(record.get("value", {})).get("total_parts", 0))
                    if expected_parts and len(pending_parts) == expected_parts:
                        record, _uncompressed_length, _compressed = _decode_record_parts(pending_parts)
                        pending_parts = []
                    else:
                        continue
                elif pending_parts:
                    raise StorageError("Incomplete REQL large record before next record")
                self._apply_record(record)
        if pending_parts:
            raise StorageError("Incomplete REQL large record at end of block file")

    def _replay_wal(self) -> int:
        if not self._wal_path.exists() or self._wal_path.stat().st_size == 0:
            return 0
        records: list[dict[str, Any]] = []
        with self._wal_path.open("rb") as fh:
            magic = fh.read(len(_WAL_MAGIC))
            if magic != _WAL_MAGIC:
                raise StorageError(f"Invalid REQL WAL for {self.path}")
            while True:
                header = fh.read(_WAL_FRAME_HEADER.size)
                if not header:
                    break
                if len(header) != _WAL_FRAME_HEADER.size:
                    break
                length, checksum = _WAL_FRAME_HEADER.unpack(header)
                payload = fh.read(length)
                if len(payload) != length:
                    break
                if hashlib.sha256(payload).digest() != checksum:
                    raise StorageError(f"Invalid REQL WAL checksum for {self.path}")
                record, _uncompressed_length, _compressed = _decode_record_payload(payload)
                records.append(record)
        for record in self._coalesced_wal_records(records):
            self._apply_record(record)
        return len(records)

    def _coalesced_wal_records(self, records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        meta: dict[str, Any] | None = None
        latest_records: dict[tuple[str, str], dict[str, Any]] = {}
        tombstones: dict[tuple[str, str], dict[str, Any]] = {}
        operations: list[dict[str, Any]] = []
        roots: list[dict[str, Any]] = []
        passthrough: list[dict[str, Any]] = []
        for record in records:
            kind = str(record.get("kind") or "")
            if kind == "meta":
                meta = dict(record)
                continue
            if kind in {"node", "edge", "dense_edge"}:
                value = record.get("value", {})
                record_id = str(value.get("id") or "") if isinstance(value, dict) else ""
                if record_id:
                    record_kind = "edge" if kind in {"edge", "dense_edge"} else "node"
                    key = (record_kind, record_id)
                    latest_records[key] = record
                    tombstones.pop(key, None)
                    continue
            if kind == "tombstone":
                value = record.get("value", {})
                target_kind = str(value.get("target_kind") or "") if isinstance(value, dict) else ""
                target_id = str(value.get("id") or "") if isinstance(value, dict) else ""
                if target_kind in {"node", "edge"} and target_id:
                    key = (target_kind, target_id)
                    latest_records.pop(key, None)
                    tombstones[key] = record
                    continue
            if kind == "operation":
                operations.append(record)
            elif kind == "root_index":
                roots.append(record)
            else:
                passthrough.append(record)
        out: list[dict[str, Any]] = []
        if meta is not None:
            out.append(meta)
        out.extend(passthrough)
        out.extend(latest_records[key] for key in sorted(latest_records))
        out.extend(tombstones[key] for key in sorted(tombstones))
        out.extend(operations)
        if roots:
            out.append(roots[-1])
        return out

    def _append_wal_records(self, records: Sequence[dict[str, Any]]) -> None:
        if not records:
            return
        needs_header = not self._wal_path.exists() or self._wal_path.stat().st_size == 0
        with self._wal_path.open("ab") as fh:
            if needs_header:
                fh.write(_WAL_MAGIC)
            for record in records:
                payload = _encode_record(record)
                fh.write(_WAL_FRAME_HEADER.pack(len(payload), hashlib.sha256(payload).digest()))
                fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())

    def _append_usage_journal_event(self, payload: dict[str, Any], *, created_at: str) -> None:
        item = {
            "format": "reql-usage-journal-v1",
            "op_type": "usage_event",
            "created_at": created_at,
            "payload": payload,
        }
        self._usage_journal_path.parent.mkdir(parents=True, exist_ok=True)
        usage_lock = _StoreLock(self._usage_journal_path, timeout_seconds=self._lock_timeout_seconds)
        usage_lock.acquire()
        try:
            with self._usage_journal_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                fh.write("\n")
        finally:
            usage_lock.release()

    def _load_usage_journal(self) -> None:
        if not self._usage_journal_path.exists():
            return
        usage_lock = _StoreLock(self._usage_journal_path, timeout_seconds=self._lock_timeout_seconds)
        usage_lock.acquire()
        try:
            with self._usage_journal_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(item, dict) or item.get("op_type") != "usage_event":
                        continue
                    payload = item.get("payload")
                    if isinstance(payload, dict):
                        self._apply_usage_event(payload, created_at=str(item.get("created_at") or ""))
        finally:
            usage_lock.release()

    def _inspect_physical_storage(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "path": str(self.path),
                "format": _FORMAT_NAME,
                "file_size": 0,
                "block_size": self.block_size,
                "blocks": {"total": 0, "data": 0, "superblock": 0},
                "records": {"total": 0, "by_kind": {}},
                "bytes": {"used": 0, "compressed_payload": 0, "uncompressed_payload": 0},
                "compression": {"ratio": 1.0, "space_saved_ratio": 0.0},
                "wal": self._inspect_wal(),
                "root_index": {"offset": self._root_index_offset, "nodes": 0, "edges": 0, "node_keys": 0, "edge_patterns": 0},
                "space_map": dict(self._space_map),
            }
        file_size = self.path.stat().st_size
        data_offset = self._data_offset if self._data_offset else 0
        if data_offset < 0 or data_offset % self.block_size != 0:
            raise StorageError(f"Invalid REQL root index offset for {self.path}")
        if file_size < data_offset or (file_size - data_offset) % self.block_size != 0:
            raise StorageError(f"Invalid REQL block store size for {self.path}")
        if self._manifest.get("checksum"):
            self._validate_data_checksum(data_offset, str(self._manifest["checksum"]))
        data_block_count = (file_size - data_offset) // self.block_size
        record_count = 0
        records_by_kind: dict[str, int] = defaultdict(int)
        used_bytes = 0
        compressed_payload_bytes = 0
        uncompressed_payload_bytes = 0
        pending_parts: list[dict[str, Any]] = []
        for block_id in range(data_block_count):
            page = self._read_block(block_id, data_offset=data_offset)
            magic, version, block_size, current_id, used, _reserved = _HEADER_STRUCT.unpack(page[:_HEADER_SIZE])
            if magic != _BLOCK_MAGIC or version != SCHEMA_VERSION or block_size != self.block_size or current_id != block_id:
                raise StorageError(f"Invalid REQL block header at block {block_id}")
            used_bytes += used
            offset = _HEADER_SIZE
            end = min(self.block_size, _HEADER_SIZE + used)
            while offset + _FRAME_HEADER.size <= end:
                (length,) = _FRAME_HEADER.unpack(page[offset : offset + _FRAME_HEADER.size])
                offset += _FRAME_HEADER.size
                if length == 0:
                    break
                payload = page[offset : offset + length]
                offset += length
                compressed_payload_bytes += length
                try:
                    record, uncompressed_length, compressed = _decode_record_payload(payload)
                except (StorageError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise StorageError(f"Invalid REQL record at block {block_id}") from exc
                if record.get("kind") == "record_part":
                    pending_parts.append(record)
                    expected_parts = int(dict(record.get("value", {})).get("total_parts", 0))
                    if expected_parts and len(pending_parts) == expected_parts:
                        try:
                            record, uncompressed_length, compressed = _decode_record_parts(pending_parts)
                        except (StorageError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                            raise StorageError(f"Invalid REQL large record ending at block {block_id}") from exc
                        pending_parts = []
                    else:
                        continue
                elif pending_parts:
                    raise StorageError(f"Incomplete REQL large record before block {block_id}")
                uncompressed_payload_bytes += uncompressed_length
                record_count += 1
                if compressed:
                    pass
                records_by_kind[str(record.get("kind", "unknown"))] += 1
        if pending_parts:
            raise StorageError(f"Incomplete REQL large record in {self.path}")
        if uncompressed_payload_bytes:
            ratio = compressed_payload_bytes / uncompressed_payload_bytes
            saved = 1.0 - ratio
        else:
            ratio = 1.0
            saved = 0.0
        return {
            "path": str(self.path),
            "format": _FORMAT_NAME,
            "file_size": file_size,
            "block_size": self.block_size,
            "blocks": {
                "total": file_size // self.block_size if self.block_size else 0,
                "data": data_block_count,
                "superblock": 1 if data_offset >= self.block_size else 0,
            },
            "records": {
                "total": record_count,
                "by_kind": dict(sorted(records_by_kind.items())),
            },
            "bytes": {
                "used": used_bytes,
                "compressed_payload": compressed_payload_bytes,
                "uncompressed_payload": uncompressed_payload_bytes,
            },
            "compression": {
                "ratio": ratio,
                "space_saved_ratio": saved,
            },
            "wal": self._inspect_wal(),
            "root_index": {
                "offset": self._root_index_offset,
                "nodes": _root_index_count(self._root_index, "nodes"),
                "edges": _root_index_count(self._root_index, "edges"),
                "node_keys": _root_index_count(self._root_index, "node_keys"),
                "edge_patterns": _root_index_count(self._root_index, "edge_patterns"),
            },
            "space_map": dict(self._space_map),
        }

    def _inspect_wal(self) -> dict[str, Any]:
        if not self._wal_path.exists():
            return {"path": str(self._wal_path), "exists": False, "frames": 0, "bytes": 0}
        frames = 0
        size = self._wal_path.stat().st_size
        with self._wal_path.open("rb") as fh:
            magic = fh.read(len(_WAL_MAGIC))
            if magic != _WAL_MAGIC:
                return {"path": str(self._wal_path), "exists": True, "frames": 0, "bytes": size, "valid": False}
            while True:
                header = fh.read(_WAL_FRAME_HEADER.size)
                if not header or len(header) != _WAL_FRAME_HEADER.size:
                    break
                length, _checksum = _WAL_FRAME_HEADER.unpack(header)
                payload = fh.read(length)
                if len(payload) != length:
                    break
                frames += 1
        return {"path": str(self._wal_path), "exists": True, "frames": frames, "bytes": size, "valid": True}

    def _read_block(self, block_id: int, *, data_offset: int | None = None) -> bytes:
        cached = self._page_cache.get(block_id)
        if cached is not None:
            return cached
        offset = (data_offset if data_offset is not None else self._data_offset) + (block_id * self.block_size)
        with self.path.open("rb") as fh:
            fh.seek(offset)
            page = fh.read(self.block_size)
        if len(page) != self.block_size:
            raise StorageError(f"Cannot read complete block {block_id} from {self.path}")
        self._page_cache.put(block_id, page)
        return page

    def _load_root_index(self) -> None:
        if self._root_index_offset < self._data_offset:
            raise StorageError(f"Invalid REQL root index offset for {self.path}")
        relative = self._root_index_offset - self._data_offset
        block_id = relative // self.block_size
        frame_offset = relative % self.block_size
        record = self._read_record_at(block_id, frame_offset)
        if record.get("kind") != "root_index":
            raise StorageError(f"REQL root index record was not found at manifest offset for {self.path}")
        self._apply_root_index(dict(record.get("value", {})))

    def _read_record_at(self, block_id: int, frame_offset: int) -> dict[str, Any]:
        pending_parts: list[dict[str, Any]] = []
        current_block = block_id
        offset = frame_offset
        block_count = int(self._manifest.get("data_block_count", 0) or 0)
        while current_block < block_count:
            page = self._read_block(current_block)
            magic, version, block_size, current_id, used, _reserved = _HEADER_STRUCT.unpack(page[:_HEADER_SIZE])
            if magic != _BLOCK_MAGIC or version != SCHEMA_VERSION or block_size != self.block_size or current_id != current_block:
                raise StorageError(f"Invalid REQL block header at block {current_block}")
            end = min(self.block_size, _HEADER_SIZE + used)
            if offset < _HEADER_SIZE:
                offset = _HEADER_SIZE
            if offset + _FRAME_HEADER.size > end:
                current_block += 1
                offset = _HEADER_SIZE
                continue
            (length,) = _FRAME_HEADER.unpack(page[offset : offset + _FRAME_HEADER.size])
            offset += _FRAME_HEADER.size
            if length == 0 or offset + length > end:
                current_block += 1
                offset = _HEADER_SIZE
                continue
            payload = page[offset : offset + length]
            offset += length
            record, _uncompressed_length, _compressed = _decode_record_payload(payload)
            if record.get("kind") == "record_part":
                pending_parts.append(record)
                expected_parts = int(dict(record.get("value", {})).get("total_parts", 0))
                if expected_parts and len(pending_parts) == expected_parts:
                    record, _uncompressed_length, _compressed = _decode_record_parts(pending_parts)
                    return record
                continue
            if pending_parts:
                raise StorageError("Incomplete REQL large record before next record")
            return record
        raise StorageError(f"Cannot read REQL record at block {block_id}, offset {frame_offset} from {self.path}")

    def _load_node_from_location(self, node_id: str) -> MemoryNode | None:
        if node_id in self._nodes:
            return self._nodes[node_id]
        location = self._node_locations.get(node_id)
        if not location:
            return None
        record = self._read_record_at(int(location["block_id"]), int(location["frame_offset"]))
        if record.get("kind") != "node":
            return None
        node = MemoryNode.from_dict(record["value"])
        self._nodes[node.id] = node
        return node

    def _load_edge_from_location(self, edge_id: str) -> MemoryEdge | None:
        if edge_id in self._edges:
            return self._edges[edge_id]
        location = self._edge_locations.get(edge_id)
        if not location:
            return None
        record = self._read_record_at(int(location["block_id"]), int(location["frame_offset"]))
        if record.get("kind") not in {"edge", "dense_edge"}:
            return None
        edge = MemoryEdge.from_dict(record["value"])
        self._edges[edge.id] = edge
        return edge

    def _materialize_all_records(self) -> None:
        if self._loaded_all_records:
            return
        for node_id in list(self._node_locations):
            self._load_node_from_location(node_id)
        for edge_id in list(self._edge_locations):
            self._load_edge_from_location(edge_id)
        self._loaded_all_records = True

    def _apply_record(self, record: dict[str, Any]) -> None:
        kind = record.get("kind")
        if kind == "meta":
            self._meta.update(record.get("value", {}))
        elif kind == "node":
            node = MemoryNode.from_dict(record["value"])
            if node.id not in self._nodes and node.id in self._node_locations:
                self._load_node_from_location(node.id)
            self._replace_node(node)
        elif kind in {"edge", "dense_edge"}:
            edge = MemoryEdge.from_dict(record["value"])
            if edge.id not in self._edges and edge.id in self._edge_locations:
                self._load_edge_from_location(edge.id)
            self._replace_edge(edge)
        elif kind == "operation":
            item = dict(record.get("value", {}))
            self._operation_log.append(item)
            if item.get("op_type") == "usage_event":
                self._apply_usage_event(dict(item.get("payload") or {}), created_at=str(item.get("created_at") or ""))
        elif kind == "root_index":
            value = dict(record.get("value", {}))
            self._apply_root_index(value)
        elif kind == "tombstone":
            value = dict(record.get("value", {}))
            target_kind = value.get("target_kind")
            target_id = value.get("id")
            if target_kind == "node" and target_id:
                self._remove_node(str(target_id))
            elif target_kind == "edge" and target_id:
                self._remove_edge(str(target_id))

    def _flush(self, *, force: bool = False) -> None:
        if self._closed:
            return
        self._ensure_writable()
        if not force and not self._dirty:
            return
        self._materialize_all_records()
        self._meta.update(
            {
                "schema_version": SCHEMA_VERSION,
                "block_size": self.block_size,
                "dense_node_threshold": self.dense_node_threshold,
                "updated_at": utcnow_iso(),
                "record_codec": "binary-v2",
            }
        )
        base_records = self._ordered_records()
        base_encoded = self._encode_records(base_records)
        generation_id = self._generation_id + 1
        blocks, locations, space_map = self._pack_blocks(base_encoded)
        root_index = self._build_root_index(locations=locations, space_map=space_map, generation_id=generation_id)
        root_record = {"kind": "root_index", "value": root_index}
        records = [*base_encoded, (root_record, _encode_record(root_record))]
        blocks, locations, space_map = self._pack_blocks(records)
        root_index = self._build_root_index(locations=locations, space_map=space_map, generation_id=generation_id)
        root_record = {"kind": "root_index", "value": root_index}
        records[-1] = (root_record, _encode_record(root_record))
        blocks, locations, space_map = self._pack_blocks(records)
        root_location = locations.get("root_index", {}).get("root_index")
        root_index_offset = self.block_size
        if root_location:
            root_index_offset = self.block_size + (int(root_location["block_id"]) * self.block_size) + int(root_location["frame_offset"])
        self._root_index = root_index
        self._space_map = space_map
        self._data_offset = self.block_size
        data_checksum = self._checksum_data_region(blocks)
        self._manifest = self._build_manifest(
            data_checksum=data_checksum,
            data_block_count=len(blocks),
            data_offset=self._data_offset,
            generation_id=generation_id,
            root_index_offset=root_index_offset,
        )
        self._generation_id = generation_id
        self._root_index_offset = root_index_offset
        superblock = self._pack_superblock(self._manifest)
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        with tmp.open("wb") as fh:
            fh.write(superblock)
            self._write_data_region(fh, blocks)
        tmp.replace(self.path)
        if self._wal_path.exists():
            self._wal_path.unlink()
        self._page_cache.clear()
        self._pending_wal_records = []
        self._dirty = False

    def _iter_data_pages(self, blocks: Sequence[bytes]) -> Iterator[bytes]:
        for block_id, body in enumerate(blocks):
            header = _HEADER_STRUCT.pack(
                _BLOCK_MAGIC,
                SCHEMA_VERSION,
                self.block_size,
                block_id,
                len(body),
                b"\x00" * 12,
            )
            yield header + body + (b"\x00" * (self.block_size - _HEADER_SIZE - len(body)))

    def _checksum_data_region(self, blocks: Sequence[bytes]) -> str:
        digest = hashlib.sha256()
        for page in self._iter_data_pages(blocks):
            digest.update(page)
        return digest.hexdigest()

    def _write_data_region(self, fh: Any, blocks: Sequence[bytes]) -> None:
        for page in self._iter_data_pages(blocks):
            fh.write(page)

    def _build_manifest(
        self,
        *,
        data_checksum: str,
        data_block_count: int,
        data_offset: int,
        generation_id: int,
        root_index_offset: int,
    ) -> dict[str, Any]:
        return {
            "format": _FORMAT_NAME,
            "manifest_version": _SUPERBLOCK_VERSION,
            "schema_version": SCHEMA_VERSION,
            "block_size": self.block_size,
            "data_offset": data_offset,
            "root_index_offset": root_index_offset,
            "generation_id": generation_id,
            "data_block_count": data_block_count,
            "checksum": data_checksum,
            "checksum_algorithm": "sha256",
            "record_codec": "binary-v2",
            "wal_path": str(self._wal_path),
            "updated_at": self._meta.get("updated_at"),
        }

    def _pack_superblock(self, manifest: dict[str, Any]) -> bytes:
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(manifest_bytes) > self.block_size - _SUPERBLOCK_HEADER_SIZE:
            raise StorageError(f"REQL manifest is too large for block size {self.block_size}")
        header = _SUPERBLOCK_HEADER_STRUCT.pack(
            _SUPERBLOCK_MAGIC,
            _SUPERBLOCK_VERSION,
            SCHEMA_VERSION,
            self.block_size,
            len(manifest_bytes),
            hashlib.sha256(manifest_bytes).digest(),
        )
        return header + manifest_bytes + (b"\x00" * (self.block_size - len(header) - len(manifest_bytes)))

    def _ordered_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = [{"kind": "meta", "value": dict(self._meta)}]
        written_edges: set[str] = set()
        dense_nodes = {
            node_id
            for node_id in self._nodes
            if len(self._out_edges.get(node_id, set()) | self._in_edges.get(node_id, set())) >= self.dense_node_threshold
        }
        for node in sorted(self._nodes.values(), key=lambda item: (item.type, item.created_at, item.id)):
            records.append({"kind": "node", "value": node.to_dict()})
            if node.id in dense_nodes:
                continue
            local_edges = self._out_edges.get(node.id, set()) | self._in_edges.get(node.id, set())
            for edge_id in sorted(local_edges):
                if edge_id in written_edges:
                    continue
                edge = self._edges.get(edge_id)
                if edge is None:
                    continue
                if edge.from_id in dense_nodes or edge.to_id in dense_nodes:
                    continue
                records.append({"kind": "edge", "value": edge.to_dict()})
                written_edges.add(edge_id)
        for edge in sorted(self._edges.values(), key=lambda item: (item.from_id, item.to_id, item.type, item.id)):
            if edge.id in written_edges:
                continue
            records.append({"kind": "dense_edge", "value": edge.to_dict()})
            written_edges.add(edge.id)
        for item in self._operation_log:
            records.append({"kind": "operation", "value": dict(item)})
        return records

    def _encode_records(self, records: Sequence[dict[str, Any]]) -> list[_EncodedRecord]:
        return [(record, _encode_record(record)) for record in records]

    def _pack_blocks(self, records: Sequence[_EncodedRecord]) -> tuple[list[bytes], dict[str, dict[str, dict[str, int]]], dict[str, Any]]:
        blocks: list[bytes] = []
        current = bytearray()
        locations: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)
        capacity = self.block_size - _HEADER_SIZE
        max_payload_size = capacity - _FRAME_HEADER.size
        for record, payload in records:
            payloads = [payload] if len(payload) <= max_payload_size else _encode_record_parts(payload, max_payload_size=max_payload_size)
            first_location: tuple[int, int, int] | None = None
            total_frame_length = 0
            for part_payload in payloads:
                frame = _FRAME_HEADER.pack(len(part_payload)) + part_payload
                if len(frame) > capacity:
                    raise StorageError(f"Record is too large for block size {self.block_size}")
                if current and len(current) + len(frame) > capacity:
                    blocks.append(bytes(current))
                    current = bytearray()
                block_id = len(blocks)
                frame_offset = _HEADER_SIZE + len(current)
                current.extend(frame)
                total_frame_length += len(frame)
                if first_location is None:
                    first_location = (block_id, frame_offset, len(frame))
            if first_location is not None:
                block_id, frame_offset, frame_length = first_location
                self._remember_record_location(locations, record, block_id=block_id, frame_offset=frame_offset, frame_length=max(frame_length, total_frame_length))
        blocks.append(bytes(current))
        space_map = self._build_space_map(blocks)
        return blocks, {kind: dict(items) for kind, items in locations.items()}, space_map

    def _remember_record_location(
        self,
        locations: dict[str, dict[str, dict[str, int]]],
        record: dict[str, Any],
        *,
        block_id: int,
        frame_offset: int,
        frame_length: int,
    ) -> None:
        kind = str(record.get("kind", ""))
        value = dict(record.get("value", {}))
        if kind == "root_index":
            record_id = "root_index"
        else:
            record_id = str(value.get("id", ""))
        if not record_id:
            return
        locations[kind][record_id] = {
            "block_id": block_id,
            "frame_offset": frame_offset,
            "frame_length": frame_length,
        }

    def _build_space_map(self, blocks: Sequence[bytes]) -> dict[str, Any]:
        entries = []
        free_lists: dict[str, list[int]] = {"small": [], "medium": [], "large": []}
        total_free = 0
        capacity = self.block_size - _HEADER_SIZE
        for block_id, body in enumerate(blocks):
            free = max(0, capacity - len(body))
            total_free += free
            entries.append({"block_id": block_id, "used": len(body), "free": free})
            if free >= 4096:
                free_lists["large"].append(block_id)
            elif free >= 1024:
                free_lists["medium"].append(block_id)
            elif free > 0:
                free_lists["small"].append(block_id)
        return {
            "block_capacity": capacity,
            "free_bytes_total": total_free,
            "blocks": entries,
            "free_lists": free_lists,
        }

    def _build_root_index(
        self,
        *,
        locations: dict[str, dict[str, dict[str, int]]],
        space_map: dict[str, Any],
        generation_id: int,
    ) -> dict[str, Any]:
        node_locations = dict(locations.get("node", {}))
        edge_locations = dict(locations.get("edge", {}))
        edge_locations.update(locations.get("dense_edge", {}))
        node_keys = sum(1 for node in self._nodes.values() if node.canonical_key)
        edge_patterns = len(self._edges)
        adjacency_nodes = len(self._nodes)
        return {
            "version": 2,
            "generation_id": generation_id,
            "codec": "binary-v2",
            "nodes": len(node_locations),
            "edges": len(edge_locations),
            "node_keys": node_keys,
            "edge_patterns": edge_patterns,
            "adjacency": adjacency_nodes,
            "space_map": _compact_space_map(space_map),
            "record_locations": {
                "nodes": node_locations,
                "edges": edge_locations,
            },
            "indexes": {
                "node_keys": _tuple_map_to_rows(self._node_key_index),
                "edge_patterns": _tuple_map_to_rows(self._edge_pattern_index),
                "out_edges": _dict_set_to_json(self._out_edges),
                "in_edges": _dict_set_to_json(self._in_edges),
                "node_terms": {
                    term: {node_id: weight for node_id, weight in sorted(postings.items())}
                    for term, postings in sorted(self._node_terms.items())
                },
                "node_type": _set_map_to_rows(self._node_type_index),
                "node_status": _set_map_to_rows(self._node_status_index),
                "edge_type": _set_map_to_rows(self._edge_type_index),
                "node_properties": _set_map_to_rows(self._node_property_index),
                "edge_properties": _set_map_to_rows(self._edge_property_index),
            },
        }

    def _apply_root_index(self, value: dict[str, Any]) -> None:
        self._root_index = value
        self._space_map = dict(value.get("space_map", {}))
        locations = dict(value.get("record_locations", {}))
        self._node_locations = {
            str(node_id): {str(k): int(v) for k, v in dict(location).items()}
            for node_id, location in dict(locations.get("nodes", {})).items()
        }
        self._edge_locations = {
            str(edge_id): {str(k): int(v) for k, v in dict(location).items()}
            for edge_id, location in dict(locations.get("edges", {})).items()
        }
        indexes = dict(value.get("indexes", {}))
        self._node_key_index = {tuple(key): str(record_id) for key, record_id in _rows_to_tuple_map(indexes.get("node_keys"), 2).items()}
        self._edge_pattern_index = {tuple(key): str(record_id) for key, record_id in _rows_to_tuple_map(indexes.get("edge_patterns"), 3).items()}
        self._out_edges = defaultdict(set, _json_to_dict_set(indexes.get("out_edges")))
        self._in_edges = defaultdict(set, _json_to_dict_set(indexes.get("in_edges")))
        self._node_type_index = defaultdict(set, _rows_to_set_map(indexes.get("node_type"), 1))
        self._node_status_index = defaultdict(set, _rows_to_set_map(indexes.get("node_status"), 1))
        self._edge_type_index = defaultdict(set, _rows_to_set_map(indexes.get("edge_type"), 1))
        self._node_property_index = defaultdict(set, _rows_to_set_map(indexes.get("node_properties"), 2))
        self._edge_property_index = defaultdict(set, _rows_to_set_map(indexes.get("edge_properties"), 2))
        node_terms: dict[str, dict[str, float]] = defaultdict(dict)
        raw_terms = indexes.get("node_terms", {})
        if isinstance(raw_terms, dict):
            for term, postings in raw_terms.items():
                if isinstance(postings, dict):
                    node_terms[str(term)] = {str(node_id): float(weight) for node_id, weight in postings.items()}
        self._node_terms = defaultdict(dict, node_terms)

    def _rebuild_indexes(self) -> None:
        self._node_key_index = {}
        self._edge_pattern_index = {}
        self._out_edges = defaultdict(set)
        self._in_edges = defaultdict(set)
        self._node_terms = defaultdict(dict)
        self._node_type_index = defaultdict(set)
        self._node_status_index = defaultdict(set)
        self._edge_type_index = defaultdict(set)
        self._node_property_index = defaultdict(set)
        self._edge_property_index = defaultdict(set)
        for node in self._nodes.values():
            self._index_node(node)
        for edge in self._edges.values():
            self._index_edge(edge)

    def _replace_node(self, node: MemoryNode) -> None:
        self._journal_node_before_change(node.id)
        old = self._nodes.get(node.id)
        if old:
            self._remove_node_indexes(old)
        self._nodes[node.id] = node
        self._index_node(node)

    def _replace_edge(self, edge: MemoryEdge) -> None:
        self._journal_edge_before_change(edge.id)
        old = self._edges.get(edge.id)
        if old:
            self._remove_edge_indexes(old)
        self._edges[edge.id] = edge
        self._index_edge(edge)

    def _remove_node(self, node_id: str) -> None:
        self._journal_node_before_change(node_id)
        old = self._nodes.pop(node_id, None)
        if old:
            self._remove_node_indexes(old)
        self._node_locations.pop(node_id, None)

    def _remove_edge(self, edge_id: str) -> None:
        self._journal_edge_before_change(edge_id)
        old = self._edges.pop(edge_id, None)
        if old:
            self._remove_edge_indexes(old)
        self._edge_locations.pop(edge_id, None)

    def _index_node(self, node: MemoryNode) -> None:
        if node.canonical_key:
            self._node_key_index[(node.type, node.canonical_key)] = node.id
        self._node_type_index[(node.type,)].add(node.id)
        self._node_status_index[(node.status,)].add(node.id)
        for property_name, encoded in _scalar_property_items(node.properties, INDEXED_NODE_PROPERTIES):
            self._node_property_index[(property_name, encoded)].add(node.id)
        self._reindex_node_terms(node)

    def _index_edge(self, edge: MemoryEdge) -> None:
        self._edge_pattern_index[(edge.from_id, edge.to_id, edge.type)] = edge.id
        self._out_edges[edge.from_id].add(edge.id)
        self._in_edges[edge.to_id].add(edge.id)
        self._edge_type_index[(edge.type,)].add(edge.id)
        for property_name, encoded in _scalar_property_items(edge.properties, INDEXED_EDGE_PROPERTIES):
            self._edge_property_index[(property_name, encoded)].add(edge.id)

    def _remove_node_indexes(self, node: MemoryNode) -> None:
        if node.canonical_key:
            self._node_key_index.pop((node.type, node.canonical_key), None)
        self._node_type_index[(node.type,)].discard(node.id)
        self._node_status_index[(node.status,)].discard(node.id)
        for property_name, encoded in _scalar_property_items(node.properties, INDEXED_NODE_PROPERTIES):
            self._node_property_index[(property_name, encoded)].discard(node.id)
        self._remove_terms(node)

    def _remove_edge_indexes(self, edge: MemoryEdge) -> None:
        self._edge_pattern_index.pop((edge.from_id, edge.to_id, edge.type), None)
        self._out_edges[edge.from_id].discard(edge.id)
        self._in_edges[edge.to_id].discard(edge.id)
        self._edge_type_index[(edge.type,)].discard(edge.id)
        for property_name, encoded in _scalar_property_items(edge.properties, INDEXED_EDGE_PROPERTIES):
            self._edge_property_index[(property_name, encoded)].discard(edge.id)

    def _remove_terms(self, node: MemoryNode) -> None:
        empty_terms: list[str] = []
        for term, postings in self._node_terms.items():
            postings.pop(node.id, None)
            if not postings:
                empty_terms.append(term)
        for term in empty_terms:
            self._node_terms.pop(term, None)

    def _reindex_node_terms(self, node: MemoryNode) -> None:
        index_texts = [node.type, node.label or "", node.canonical_key or ""]
        remaining = DEFAULT_LEXICAL_TEXT_BUDGET
        text = node.text or ""
        if text:
            index_texts.append(text[:remaining])
            remaining = max(0, remaining - len(text))
        for key in sorted(INDEXED_NODE_PROPERTIES):
            value = node.properties.get(key)
            if isinstance(value, (str, int, float, bool)):
                item = str(value)
                index_texts.append(item[:remaining] if remaining else item[:128])
                remaining = max(0, remaining - len(item))
            elif isinstance(value, list):
                for item in value[:20]:
                    if isinstance(item, (str, int, float, bool)):
                        text_item = str(item)
                        index_texts.append(text_item[:remaining] if remaining else text_item[:128])
                        remaining = max(0, remaining - len(text_item))
        for term, score in keyword_scores(" ".join(index_texts), max_terms=80):
            self._node_terms[term][node.id] = float(score)

    def upsert_node(self, node: MemoryNode) -> tuple[MemoryNode, bool]:
        self._ensure_writable()
        stored, created, record = self._prepare_node_upsert(node)
        self._commit(record)
        return stored, created

    def _prepare_node_upsert(self, node: MemoryNode) -> tuple[MemoryNode, bool, dict[str, Any] | None]:
        now = utcnow_iso()
        node = self._clone_node(node)
        node.updated_at = now
        existing: MemoryNode | None = None
        if node.canonical_key:
            existing_id = self._node_key_index.get((node.type, node.canonical_key))
            existing = self._load_node_from_location(existing_id) if existing_id else None
        if existing is None:
            existing = self._load_node_from_location(node.id)
        if existing:
            merged = self._merge_nodes(existing, node)
            if _nodes_equivalent(existing, merged):
                return self._clone_node(existing), False, None
            self._replace_node(merged)
            return self._clone_node(merged), False, {"kind": "node", "value": merged.to_dict()}
        self._replace_node(node)
        return self._clone_node(node), True, {"kind": "node", "value": node.to_dict()}

    def batch_upsert_nodes(self, nodes: Sequence[MemoryNode]) -> list[tuple[MemoryNode, bool]]:
        if self._transaction_depth == 0:
            with self.transaction():
                return self.batch_upsert_nodes(nodes)
        results: list[tuple[MemoryNode, bool]] = []
        records: list[dict[str, Any]] = []
        self._ensure_writable()
        for node in nodes:
            stored, created, record = self._prepare_node_upsert(node)
            results.append((stored, created))
            if record is not None:
                records.append(record)
        self._commit_many(records)
        return results

    def _merge_nodes(self, existing: MemoryNode, incoming: MemoryNode) -> MemoryNode:
        merged = self._clone_node(existing)
        props = dict(merged.properties)
        props.update({key: value for key, value in incoming.properties.items() if value is not None})
        merged.label = incoming.label or merged.label
        merged.text = incoming.text or merged.text
        merged.canonical_key = incoming.canonical_key or merged.canonical_key
        merged.properties = props
        merged.activation = max(merged.activation, incoming.activation)
        merged.base_activation = max(merged.base_activation, incoming.base_activation)
        merged.salience = max(merged.salience, incoming.salience)
        merged.confidence = max(0.0, min(1.0, (merged.confidence + incoming.confidence) / 2))
        merged.stability = max(merged.stability, incoming.stability)
        merged.volatility = min(merged.volatility, incoming.volatility)
        merged.utility = max(merged.utility, incoming.utility)
        merged.updated_at = incoming.updated_at
        merged.last_activated_at = incoming.last_activated_at or merged.last_activated_at
        merged.last_used_at = incoming.last_used_at or merged.last_used_at
        merged.usage_count = max(merged.usage_count, incoming.usage_count)
        merged.evidence_count = max(merged.evidence_count, incoming.evidence_count)
        if merged.status == "archived" and incoming.status in ACTIVE_STATUSES:
            merged.status = incoming.status
        elif incoming.status not in {"candidate", "latent"}:
            merged.status = incoming.status
        return merged

    def get_node(self, node_id: str) -> MemoryNode | None:
        node = self._load_node_from_location(node_id) if node_id not in self._nodes else self._nodes.get(node_id)
        return self._clone_node(node) if node else None

    def get_nodes(self, node_ids: Sequence[str]) -> list[MemoryNode]:
        out: list[MemoryNode] = []
        for node_id in node_ids:
            node = self._load_node_from_location(node_id) if node_id not in self._nodes else self._nodes.get(node_id)
            if node:
                out.append(self._clone_node(node))
        return out

    def get_node_by_key(self, type_: str, canonical_key: str) -> MemoryNode | None:
        node_id = self._node_key_index.get((type_, canonical_key))
        return self.get_node(node_id) if node_id else None

    def find_nodes(
        self,
        *,
        type_: str | None = None,
        status: str | Sequence[str] | None = None,
        limit: int = 100,
        order_by: str = "salience",
        descending: bool = True,
    ) -> list[MemoryNode]:
        allowed_order = {"salience", "activation", "updated_at", "created_at", "confidence", "utility"}
        if order_by not in allowed_order:
            order_by = "salience"
        statuses = {status} if isinstance(status, str) else set(status or [])
        candidate_ids = set(self._node_type_index.get((type_,), set())) if type_ is not None else set(self._nodes) | set(self._node_locations)
        if statuses:
            status_ids: set[str] = set()
            for item_status in statuses:
                status_ids.update(self._node_status_index.get((item_status,), set()))
            candidate_ids &= status_ids
        nodes = [node for node_id in candidate_ids if (node := self._load_node_from_location(node_id)) is not None]
        nodes.sort(key=lambda item: getattr(item, order_by), reverse=descending)
        return [self._clone_node(node) for node in nodes[:limit]]

    def find_nodes_by_property(
        self,
        property_name: str,
        value: Any,
        *,
        type_: str | None = None,
        status: str | Sequence[str] | None = None,
        limit: int = 1000,
    ) -> list[MemoryNode]:
        statuses = {status} if isinstance(status, str) else set(status or [])
        encoded = _property_value_key(value)
        candidate_ids = set(self._node_property_index.get((property_name, encoded), set())) if encoded is not None else set()
        if type_ is not None:
            candidate_ids &= set(self._node_type_index.get((type_,), set()))
        if statuses:
            status_ids: set[str] = set()
            for item_status in statuses:
                status_ids.update(self._node_status_index.get((item_status,), set()))
            candidate_ids &= status_ids
        matches = [node for node_id in candidate_ids if (node := self._load_node_from_location(node_id)) is not None and node.properties.get(property_name) == value]
        matches.sort(key=lambda item: item.updated_at, reverse=True)
        return [self._clone_node(node) for node in matches[:limit]]

    def update_node_fields(self, node_id: str, **fields: Any) -> MemoryNode | None:
        self._ensure_writable()
        node = self.get_node(node_id)
        if node is None:
            return None
        allowed = {
            "label",
            "text",
            "canonical_key",
            "properties",
            "activation",
            "base_activation",
            "salience",
            "confidence",
            "stability",
            "volatility",
            "utility",
            "updated_at",
            "last_activated_at",
            "last_used_at",
            "usage_count",
            "evidence_count",
            "status",
        }
        changed = False
        for key, value in fields.items():
            if key in allowed:
                setattr(node, key, value)
                changed = True
        if changed:
            node.updated_at = utcnow_iso()
            self._replace_node(node)
            self._commit({"kind": "node", "value": node.to_dict()})
        return self.get_node(node_id)

    def increment_node(self, node_id: str, **increments: float) -> MemoryNode | None:
        node = self.get_node(node_id)
        if node is None:
            return None
        fields: dict[str, Any] = {}
        for key, inc in increments.items():
            if not hasattr(node, key):
                continue
            value = getattr(node, key)
            if isinstance(value, (int, float)):
                fields[key] = value + inc
        return self.update_node_fields(node_id, **fields)

    def upsert_edge(self, edge: MemoryEdge) -> tuple[MemoryEdge, bool]:
        self._ensure_writable()
        stored, created, record = self._prepare_edge_upsert(edge)
        self._commit(record)
        return stored, created

    def _prepare_edge_upsert(self, edge: MemoryEdge) -> tuple[MemoryEdge, bool, dict[str, Any] | None]:
        if self._load_node_from_location(edge.from_id) is None or self._load_node_from_location(edge.to_id) is None:
            raise StorageError(f"Cannot create edge {edge.type} {edge.from_id}->{edge.to_id}: missing endpoint")
        now = utcnow_iso()
        edge = self._clone_edge(edge)
        edge.updated_at = now
        existing_id = self._edge_pattern_index.get((edge.from_id, edge.to_id, edge.type))
        existing = self._load_edge_from_location(existing_id) if existing_id else None
        if existing:
            merged = self._merge_edges(existing, edge)
            if _edges_equivalent(existing, merged):
                return self._clone_edge(existing), False, None
            self._replace_edge(merged)
            return self._clone_edge(merged), False, {"kind": "edge", "value": merged.to_dict()}
        self._replace_edge(edge)
        return self._clone_edge(edge), True, {"kind": "edge", "value": edge.to_dict()}

    def batch_upsert_edges(self, edges: Sequence[MemoryEdge]) -> list[tuple[MemoryEdge, bool]]:
        if self._transaction_depth == 0:
            with self.transaction():
                return self.batch_upsert_edges(edges)
        results: list[tuple[MemoryEdge, bool]] = []
        records: list[dict[str, Any]] = []
        self._ensure_writable()
        for edge in edges:
            stored, created, record = self._prepare_edge_upsert(edge)
            results.append((stored, created))
            if record is not None:
                records.append(record)
        self._commit_many(records)
        return results

    def _merge_edges(self, existing: MemoryEdge, incoming: MemoryEdge) -> MemoryEdge:
        merged = self._clone_edge(existing)
        props = dict(merged.properties)
        props.update({key: value for key, value in incoming.properties.items() if value is not None})
        merged.weight = max(0.0, min(1.0, max(merged.weight, incoming.weight)))
        merged.confidence = max(0.0, min(1.0, (merged.confidence + incoming.confidence) / 2))
        merged.polarity = incoming.polarity if incoming.polarity in {-1, 1} else merged.polarity
        merged.origin = incoming.origin if merged.origin == "ambiguous" else merged.origin
        merged.properties = props
        merged.co_activation_count = max(merged.co_activation_count, incoming.co_activation_count)
        merged.last_fired_at = incoming.last_fired_at or merged.last_fired_at
        merged.updated_at = incoming.updated_at
        return merged

    def get_edge(self, edge_id: str) -> MemoryEdge | None:
        edge = self._load_edge_from_location(edge_id) if edge_id not in self._edges else self._edges.get(edge_id)
        return self._clone_edge(edge) if edge else None

    def get_edge_by_pattern(self, from_id: str, to_id: str, type_: str) -> MemoryEdge | None:
        edge_id = self._edge_pattern_index.get((from_id, to_id, type_))
        return self.get_edge(edge_id) if edge_id else None

    def get_edges(
        self,
        *,
        from_id: str | None = None,
        to_id: str | None = None,
        type_: str | Sequence[str] | None = None,
        limit: int = 1000,
    ) -> list[MemoryEdge]:
        types = {type_} if isinstance(type_, str) else set(type_ or [])
        if from_id is not None:
            candidate_ids = set(self._out_edges.get(from_id, set()))
        elif to_id is not None:
            candidate_ids = set(self._in_edges.get(to_id, set()))
        elif types:
            candidate_ids = set()
            for item_type in types:
                candidate_ids.update(self._edge_type_index.get((item_type,), set()))
        else:
            candidate_ids = set(self._edges) | set(self._edge_locations)
        edges = []
        for edge_id in candidate_ids:
            edge = self._load_edge_from_location(edge_id)
            if (
                edge is not None
                and (from_id is None or edge.from_id == from_id)
                and (to_id is None or edge.to_id == to_id)
                and (not types or edge.type in types)
            ):
                edges.append(edge)
        edges.sort(key=lambda item: item.weight, reverse=True)
        return [self._clone_edge(edge) for edge in edges[:limit]]

    def incident_edges(
        self,
        node_ids: Sequence[str],
        *,
        edge_types: set[str] | None = None,
        ignored_edge_types: set[str] | None = None,
        limit: int = 10000,
    ) -> list[MemoryEdge]:
        ids = set(node_ids)
        edge_ids: set[str] = set()
        for node_id in ids:
            edge_ids.update(self._out_edges.get(node_id, set()))
            edge_ids.update(self._in_edges.get(node_id, set()))
        edges = [
            edge
            for edge_id in edge_ids
            if (edge := self._load_edge_from_location(edge_id)) is not None
            and (edge_types is None or edge.type in edge_types)
            and (ignored_edge_types is None or edge.type not in ignored_edge_types)
        ]
        edges.sort(key=lambda item: (item.weight, item.updated_at), reverse=True)
        return [self._clone_edge(edge) for edge in edges[:limit]]

    def find_edges_by_property(
        self,
        property_name: str,
        value: Any,
        *,
        type_: str | None = None,
        limit: int = 1000,
    ) -> list[MemoryEdge]:
        encoded = _property_value_key(value)
        candidate_ids = set(self._edge_property_index.get((property_name, encoded), set())) if encoded is not None else set()
        if type_ is not None:
            candidate_ids &= set(self._edge_type_index.get((type_,), set()))
        edges = [edge for edge_id in candidate_ids if (edge := self._load_edge_from_location(edge_id)) is not None and edge.properties.get(property_name) == value]
        edges.sort(key=lambda item: item.updated_at, reverse=True)
        return [self._clone_edge(edge) for edge in edges[:limit]]

    def neighbors(
        self,
        node_id: str,
        *,
        direction: str = "both",
        edge_types: set[str] | None = None,
        min_weight: float = 0.0,
        limit: int = 500,
    ) -> list[tuple[MemoryEdge, MemoryNode]]:
        results: list[tuple[MemoryEdge, MemoryNode]] = []
        if direction in {"out", "both"}:
            results.extend(self._neighbors_one(node_id, outgoing=True, edge_types=edge_types, min_weight=min_weight, limit=limit))
        if direction in {"in", "both"}:
            results.extend(self._neighbors_one(node_id, outgoing=False, edge_types=edge_types, min_weight=min_weight, limit=limit))
        seen: set[str] = set()
        deduped: list[tuple[MemoryEdge, MemoryNode]] = []
        for edge, node in results:
            if edge.id in seen:
                continue
            seen.add(edge.id)
            deduped.append((edge, node))
        return deduped[:limit]

    def _neighbors_one(
        self,
        node_id: str,
        *,
        outgoing: bool,
        edge_types: set[str] | None,
        min_weight: float,
        limit: int,
    ) -> list[tuple[MemoryEdge, MemoryNode]]:
        edge_ids = self._out_edges.get(node_id, set()) if outgoing else self._in_edges.get(node_id, set())
        candidates: list[MemoryEdge] = []
        for edge_id in edge_ids:
            edge = self._load_edge_from_location(edge_id)
            if edge is None or edge.weight < min_weight:
                continue
            if edge_types and edge.type not in edge_types:
                continue
            candidates.append(edge)
        candidates.sort(key=lambda item: item.weight, reverse=True)
        results: list[tuple[MemoryEdge, MemoryNode]] = []
        for edge in candidates[:limit]:
            other_id = edge.to_id if outgoing else edge.from_id
            node = self._load_node_from_location(other_id)
            if node:
                results.append((self._clone_edge(edge), self._clone_node(node)))
        return results

    def bounded_neighborhood(
        self,
        node_id: str,
        *,
        max_depth: int = 2,
        edge_types: set[str] | None = None,
        limit: int = 1000,
    ) -> tuple[list[MemoryNode], list[MemoryEdge]]:
        start = self._load_node_from_location(node_id)
        if start is None:
            return [], []
        nodes: dict[str, MemoryNode] = {start.id: self._clone_node(start)}
        edges: dict[str, MemoryEdge] = {}
        frontier = {node_id}
        depth = 0
        while frontier and depth < max_depth and len(nodes) < limit:
            next_frontier: set[str] = set()
            per_node_limit = max(10, min(200, limit // max(1, len(frontier))))
            for current_id in sorted(frontier):
                for edge, neighbor in self.neighbors(current_id, direction="both", edge_types=edge_types, limit=per_node_limit):
                    edges.setdefault(edge.id, edge)
                    if neighbor.id not in nodes:
                        nodes[neighbor.id] = neighbor
                        next_frontier.add(neighbor.id)
                    if len(nodes) >= limit:
                        break
                if len(nodes) >= limit:
                    break
            frontier = next_frontier
            depth += 1
        return list(nodes.values()), list(edges.values())[:limit]

    def update_edge_fields(self, edge_id: str, **fields: Any) -> MemoryEdge | None:
        self._ensure_writable()
        edge = self.get_edge(edge_id)
        if edge is None:
            return None
        allowed = {"weight", "confidence", "polarity", "origin", "properties", "co_activation_count", "last_fired_at", "updated_at"}
        changed = False
        for key, value in fields.items():
            if key in allowed:
                setattr(edge, key, value)
                changed = True
        if changed:
            edge.updated_at = utcnow_iso()
            self._replace_edge(edge)
            self._commit({"kind": "edge", "value": edge.to_dict()})
        return self.get_edge(edge_id)

    def archive_nodes_by_artifact(
        self,
        project_id: str,
        artifact_id: str,
        *,
        node_types: Sequence[str] | None = None,
    ) -> int:
        nodes = self.find_nodes_by_property("artifact_id", artifact_id, limit=100000)
        allowed_types = set(node_types) if node_types else None
        now = utcnow_iso()
        count = 0
        with self.transaction():
            for node in nodes:
                if node.properties.get("project_id") != project_id:
                    continue
                if allowed_types and node.type not in allowed_types:
                    continue
                if node.status == "archived":
                    continue
                properties = dict(node.properties)
                properties["status"] = "archived"
                properties["updated_at"] = now
                self.update_node_fields(node.id, status="archived", properties=properties)
                count += 1
        return count

    def update_node_metrics(self, node_id: str, **metrics: Any) -> MemoryNode | None:
        allowed = {
            "activation",
            "base_activation",
            "salience",
            "confidence",
            "stability",
            "volatility",
            "utility",
            "last_activated_at",
            "last_used_at",
            "usage_count",
            "evidence_count",
        }
        return self.update_node_fields(node_id, **{key: value for key, value in metrics.items() if key in allowed})

    def persist_analysis_results(self, nodes: Sequence[MemoryNode], edges: Sequence[MemoryEdge]) -> dict[str, int]:
        node_results = self.batch_upsert_nodes(nodes)
        edge_results = self.batch_upsert_edges(edges)
        return {
            "nodes_created": sum(1 for _, created in node_results if created),
            "nodes_updated": sum(1 for _, created in node_results if not created),
            "edges_created": sum(1 for _, created in edge_results if created),
            "edges_updated": sum(1 for _, created in edge_results if not created),
        }

    def lexical_search(
        self,
        text: str,
        *,
        top_k: int = 20,
        node_types: set[str] | None = None,
        include_archived: bool = False,
    ) -> list[tuple[MemoryNode, float]]:
        tokens = tokenize(text)
        if not tokens:
            return []
        unique_terms = set(tokens)
        raw_by_node: dict[str, float] = defaultdict(float)
        matched_terms: dict[str, int] = defaultdict(int)
        node_count = max(1, len(self._nodes) + len(self._node_locations))
        max_idf = math.log1p(node_count)
        for term in unique_terms:
            postings = self._node_terms.get(term, {})
            if not postings:
                continue
            inverse_frequency = math.log1p(node_count / max(1, len(postings))) / max_idf
            term_weight = 0.20 + 0.80 * min(1.0, inverse_frequency)
            for node_id, weight in postings.items():
                raw_by_node[node_id] += weight * term_weight
                matched_terms[node_id] += 1
        scored: list[tuple[MemoryNode, float]] = []
        for node_id, raw_score in raw_by_node.items():
            node = self._load_node_from_location(node_id)
            if node is None:
                continue
            if node_types and node.type not in node_types:
                continue
            if not include_archived and node.status not in ACTIVE_STATUSES:
                continue
            normalized = min(1.0, math.log1p(raw_score) / math.log1p(len(unique_terms) + 2))
            score = min(1.0, 0.85 * normalized + 0.15 * node.salience + 0.02 * matched_terms[node_id])
            scored.append((self._clone_node(node), score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]

    def degree(self, node_id: str, *, edge_types: set[str] | None = None) -> int:
        count = 0
        for edge_id in self._out_edges.get(node_id, set()) | self._in_edges.get(node_id, set()):
            edge = self._load_edge_from_location(edge_id)
            if edge and (edge_types is None or edge.type in edge_types):
                count += 1
        return count

    def count_nodes(
        self,
        *,
        node_types: set[str] | None = None,
        statuses: set[str] | None = None,
        project_id: str | None = None,
        include_global_project: bool = True,
    ) -> int:
        candidate_ids = set(self._nodes) | set(self._node_locations)
        if node_types is not None:
            typed: set[str] = set()
            for node_type in node_types:
                typed.update(self._node_type_index.get((node_type,), set()))
            candidate_ids &= typed
        if statuses is not None:
            status_ids: set[str] = set()
            for status in statuses:
                status_ids.update(self._node_status_index.get((status,), set()))
            candidate_ids &= status_ids
        if project_id is None:
            return len(candidate_ids)
        return sum(
            1
            for node_id in candidate_ids
            if (node := self._load_node_from_location(node_id)) is not None
            and self._project_matches(node.properties, project_id, include_global_project)
        )

    def count_edges(
        self,
        *,
        edge_types: set[str] | None = None,
        project_id: str | None = None,
        include_global_project: bool = True,
    ) -> int:
        candidate_ids = set(self._edges) | set(self._edge_locations)
        if edge_types is not None:
            typed: set[str] = set()
            for edge_type in edge_types:
                typed.update(self._edge_type_index.get((edge_type,), set()))
            candidate_ids &= typed
        if project_id is None:
            return len(candidate_ids)
        return sum(
            1
            for edge_id in candidate_ids
            if (edge := self._load_edge_from_location(edge_id)) is not None
            and self._project_matches(edge.properties, project_id, include_global_project)
        )

    def node_type_counts(
        self,
        *,
        statuses: set[str] | None = None,
        project_id: str | None = None,
        include_global_project: bool = True,
    ) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        if project_id is None:
            status_ids: set[str] | None = None
            if statuses is not None:
                status_ids = set()
                for status in statuses:
                    status_ids.update(self._node_status_index.get((status,), set()))
            for (node_type,) in self._node_type_index:
                ids = set(self._node_type_index.get((node_type,), set()))
                if status_ids is not None:
                    ids &= status_ids
                counts[node_type] += len(ids)
            return dict(sorted(counts.items()))
        for node_id in set(self._nodes) | set(self._node_locations):
            node = self._load_node_from_location(node_id)
            if node and self._node_matches_filter(node, statuses=statuses, project_id=project_id, include_global_project=include_global_project):
                counts[node.type] += 1
        return dict(sorted(counts.items()))

    def top_nodes_by_degree(
        self,
        *,
        limit: int = 100,
        node_types: set[str] | None = None,
        statuses: set[str] | None = None,
        exclude_types: set[str] | None = None,
        ignored_edge_types: set[str] | None = None,
        project_id: str | None = None,
        include_global_project: bool = True,
    ) -> list[tuple[MemoryNode, int, float]]:
        if limit <= 0:
            return []
        rows: list[tuple[MemoryNode, int, float]] = []
        for node_id in set(self._nodes) | set(self._node_locations):
            node = self._load_node_from_location(node_id)
            if node is None:
                continue
            if not self._node_matches_filter(
                node,
                node_types=node_types,
                statuses=statuses,
                exclude_types=exclude_types,
                project_id=project_id,
                include_global_project=include_global_project,
            ):
                continue
            edge_ids = self._out_edges.get(node.id, set()) | self._in_edges.get(node.id, set())
            degree = 0
            weighted = 0.0
            for edge_id in edge_ids:
                edge = self._load_edge_from_location(edge_id)
                if edge is None:
                    continue
                if ignored_edge_types and edge.type in ignored_edge_types:
                    continue
                degree += 1
                weighted += edge.weight * edge.confidence
            rows.append((self._clone_node(node), degree, weighted))
        rows.sort(key=lambda item: (item[1], item[2], item[0].salience, item[0].updated_at), reverse=True)
        return rows[:limit]

    @staticmethod
    def _project_matches(properties: dict[str, Any], project_id: str | None, include_global_project: bool) -> bool:
        if not project_id:
            return True
        value = properties.get("project_id")
        return value == project_id or (include_global_project and value is None)

    def _node_matches_filter(
        self,
        node: MemoryNode,
        *,
        node_types: set[str] | None = None,
        statuses: set[str] | None = None,
        exclude_types: set[str] | None = None,
        project_id: str | None = None,
        include_global_project: bool = True,
    ) -> bool:
        return (
            (node_types is None or node.type in node_types)
            and (exclude_types is None or node.type not in exclude_types)
            and (statuses is None or node.status in statuses)
            and self._project_matches(node.properties, project_id, include_global_project)
        )

    def all_nodes(self) -> list[MemoryNode]:
        self._materialize_all_records()
        nodes = list(self._nodes.values())
        nodes.sort(key=lambda item: (item.created_at, item.id))
        return [self._clone_node(node) for node in nodes]

    def all_edges(self) -> list[MemoryEdge]:
        self._materialize_all_records()
        edges = list(self._edges.values())
        edges.sort(key=lambda item: (item.created_at, item.id))
        return [self._clone_edge(edge) for edge in edges]

    def log_operation(self, op_type: str, payload: dict[str, Any]) -> None:
        self._ensure_writable()
        item = {
            "op_type": op_type,
            "payload": payload,
            "created_at": utcnow_iso(),
        }
        self._operation_log.append(item)
        if op_type == "usage_event":
            self._apply_usage_event(payload, created_at=str(item["created_at"]))
        self._commit({"kind": "operation", "value": dict(item)})

    def record_usage_event(self, query_text: str, ranked_nodes: Sequence[dict[str, Any]]) -> None:
        if not ranked_nodes:
            return
        payload = {
            "query": query_text,
            "nodes": [
                {
                    "id": str(item.get("id")),
                    "score": float(item.get("score", 0.0) or 0.0),
                    "activation": float(item.get("activation", 0.0) or 0.0),
                }
                for item in ranked_nodes
                if item.get("id")
            ],
        }
        if payload["nodes"]:
            created_at = utcnow_iso()
            self._apply_usage_event(payload, created_at=created_at)
            self._append_usage_journal_event(payload, created_at=created_at)

    def usage_for_node(self, node_id: str) -> dict[str, Any]:
        return dict(self._usage_by_node.get(node_id, {}))

    def _apply_usage_event(self, payload: dict[str, Any], *, created_at: str | None = None) -> None:
        now = created_at or utcnow_iso()
        for item in payload.get("nodes", []):
            if not isinstance(item, dict) or not item.get("id"):
                continue
            node_id = str(item["id"])
            self._journal_usage_before_change(node_id)
            score = max(0.0, min(1.0, float(item.get("score", 0.0) or 0.0)))
            activation = max(0.0, min(1.0, float(item.get("activation", 0.0) or 0.0)))
            current = dict(self._usage_by_node.get(node_id, {}))
            current["usage_count"] = int(current.get("usage_count", 0) or 0) + 1
            current["last_used_at"] = now
            current["utility_delta"] = float(current.get("utility_delta", 0.0) or 0.0) + (0.03 * score)
            current["activation"] = max(float(current.get("activation", 0.0) or 0.0), activation)
            self._usage_by_node[node_id] = current

    def export_json(self) -> dict[str, Any]:
        graph_nodes = list(self.all_nodes())
        graph_node_ids = {node.id for node in graph_nodes}
        graph_edges = [
            edge
            for edge in self.all_edges()
            if edge.from_id in graph_node_ids and edge.to_id in graph_node_ids
        ]
        return {
            "format": "reql-memory-export-v1",
            "storage": "reql-block-graph",
            "created_at": utcnow_iso(),
            "nodes": [node.to_dict() for node in graph_nodes],
            "edges": [edge.to_dict() for edge in graph_edges],
        }

    def import_json(self, payload: dict[str, Any]) -> None:
        with self.transaction():
            for node_payload in payload.get("nodes", []):
                self.upsert_node(MemoryNode.from_dict(node_payload))
            for edge_payload in payload.get("edges", []):
                self.upsert_edge(MemoryEdge.from_dict(edge_payload))


def _nodes_equivalent(left: MemoryNode, right: MemoryNode) -> bool:
    return _normalized_node_payload(left) == _normalized_node_payload(right)


def _edges_equivalent(left: MemoryEdge, right: MemoryEdge) -> bool:
    return _normalized_edge_payload(left) == _normalized_edge_payload(right)


def _normalized_node_payload(node: MemoryNode) -> dict[str, Any]:
    payload = node.to_dict()
    for field in _VOLATILE_RECORD_FIELDS:
        payload.pop(field, None)
    payload["properties"] = _normalized_properties(node.properties)
    return payload


def _normalized_edge_payload(edge: MemoryEdge) -> dict[str, Any]:
    payload = edge.to_dict()
    for field in _VOLATILE_RECORD_FIELDS:
        payload.pop(field, None)
    payload["properties"] = _normalized_properties(edge.properties)
    return payload


def _normalized_properties(properties: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in properties.items() if key not in _VOLATILE_PROPERTY_FIELDS}
