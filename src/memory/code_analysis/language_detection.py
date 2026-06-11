"""Code language normalization."""
from __future__ import annotations

from pathlib import Path

from ..artifacts.models import SourceArtifact
from .languages import display_language_for_path
from .languages import normalize_language as _normalize_language


def normalize_language(language: str | None) -> str | None:
    return _normalize_language(language)


def detect_code_language(artifact: SourceArtifact) -> str | None:
    detected = normalize_language(artifact.language)
    if detected:
        return detected
    return normalize_language(display_language_for_path(Path(artifact.path)))
