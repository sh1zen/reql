"""Extraction port.

Extractors may be deterministic or statistical. The bundled implementation is
deterministic and has no external runtime dependency.
"""
from __future__ import annotations

from typing import Protocol

from ..extraction.deterministic import ExtractionResult


class SemanticExtractor(Protocol):
    def extract(self, text: str) -> ExtractionResult: ...
