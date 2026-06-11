"""Stable path and content fingerprints for source artifacts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..domain.ids import stable_id
from .models import ArtifactFingerprint

DEFAULT_PARSER_VERSION = "project-scan-v9"
DEFAULT_CHUNKING_VERSION = "uncompiled-v1"


def normalize_path(path: str | Path) -> str:
    """Return an absolute normalized path string with POSIX separators."""
    return Path(path).expanduser().resolve(strict=False).as_posix()


def relative_path(root: str | Path, path: str | Path) -> str:
    root_path = Path(root).expanduser().resolve(strict=False)
    target = Path(path).expanduser().resolve(strict=False)
    return target.relative_to(root_path).as_posix()


def file_uri(path: str | Path) -> str:
    return Path(path).expanduser().resolve(strict=False).as_uri()


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def options_hash(options: dict[str, Any] | None = None) -> str:
    payload = json.dumps(options or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def project_id(*parts: str) -> str:
    root_path = parts[-1]
    return stable_id("project", normalize_path(root_path))


def artifact_id(*parts: str) -> str:
    project_id_, relative_path_ = parts[-2], parts[-1]
    return stable_id("artifact", project_id_, relative_path_)


def fingerprint_file(
    root: str | Path,
    path: str | Path,
    *,
    parser_version: str = DEFAULT_PARSER_VERSION,
    chunking_version: str = DEFAULT_CHUNKING_VERSION,
    options: dict[str, Any] | None = None,
) -> ArtifactFingerprint:
    target = Path(path)
    stat = target.stat()
    normalized = normalize_path(target)
    rel = relative_path(root, target)
    return ArtifactFingerprint(
        path=normalized,
        relative_path=rel,
        size_bytes=int(stat.st_size),
        mtime=float(stat.st_mtime),
        sha256=sha256_file(target),
        parser_version=parser_version,
        chunking_version=chunking_version,
        options_hash=options_hash(options),
    )
