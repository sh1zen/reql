"""Optional semantic extraction extension point.

Integrators can implement ``SemanticExtractor`` and inject it into
``MemoryGraph``. The bundled project document path remains local and
deterministic.
"""
from __future__ import annotations

from ..storage.extractor import SemanticExtractor

__all__ = ["SemanticExtractor"]
