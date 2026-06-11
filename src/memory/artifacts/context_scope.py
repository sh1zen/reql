"""Canonical compile-time scope labels for project artifacts."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from .models import SourceArtifact

ContextScope = Literal["code", "docs", "test"]


def artifact_context_scope(artifact: SourceArtifact) -> ContextScope:
    rel = artifact.relative_path.replace("\\", "/").lstrip("/")
    name = Path(rel).name
    if artifact.artifact_type == "code" and (rel.startswith("tests/") or name.startswith("test_") or name.endswith("_test.py")):
        return "test"
    if artifact.artifact_type == "code":
        return "code"
    return "docs"
