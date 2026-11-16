"""Stable path and content fingerprints for source artifacts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..domain.ids import stable_id

DEFAULT_PARSER_VERSION = "project-scan-v11"
DEFAULT_CHUNKING_VERSION = "uncompiled-v1"


def compilation_fingerprint(
    options_digest: str,
    *,
    parser_version: str = DEFAULT_PARSER_VERSION,
    chunking_version: str = DEFAULT_CHUNKING_VERSION,
) -> str:
    """Identify the compiler contract that produced a stored artifact snapshot."""
    return stable_id("compilation", parser_version, chunking_version, options_digest)


def normalize_path(path: str | Path) -> str:
    """Return an absolute normalized path string with POSIX separators."""
    return Path(path).expanduser().resolve(strict=False).as_posix()


def normalize_relative_lookup_path(path: str | Path) -> str:
    """Normalize a project-relative lookup path without altering dotfile names."""
    value = str(path).strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    if value.startswith("/") or (len(value) >= 2 and value[1] == ":"):
        raise ValueError("path must be project-relative")
    parts = []
    for part in value.rstrip("/").split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise ValueError("path must not traverse outside the project")
        parts.append(part)
    return "/".join(parts)


def relative_path_lookup_key(path: str | Path) -> str:
    return normalize_relative_lookup_path(path).casefold()


def relative_path(root: str | Path, path: str | Path) -> str:
    root_path = Path(root).expanduser().resolve(strict=False)
    target = Path(path).expanduser().resolve(strict=False)
    return target.relative_to(root_path).as_posix()


def options_hash(options: dict[str, Any] | None = None) -> str:
    payload = json.dumps(options or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def project_id(root_path: str) -> str:
    """Identify a project by its normalized filesystem root."""
    return stable_id("project", normalize_path(root_path))


def artifact_id(project_id_: str, relative_path_: str) -> str:
    """Identify an artifact within its owning project."""
    return stable_id("artifact", project_id_, relative_path_)
