"""Deterministic lexical + bounded graph retrieval."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
import re
from typing import Any, Sequence

from .context.models import _PathCandidate, _QueryProfile

from ...diagnostics import PerformanceLogger
from ...domain.constants import INACTIVE_STATUSES
from ...extraction.deterministic import DeterministicExtractor
from ...domain.ids import stable_id
from ...domain.models import MemoryEdge, MemoryNode, MemoryQuery, MemorySubgraph, RankedNode
from ...extraction.normalization import canonicalize, clamp, token_signal_score, tokenize
from ...domain.timeutils import utcnow_iso
from ...storage.extractor import SemanticExtractor
from ...storage.graph_store import GraphStore

TECHNICAL_NODE_TYPES = {"RetrievalTrace", "System", "Session", "Debug", "Log", "Comment", "Docstring", "Import"}
GRAPH_SEED_NODE_TYPES = {"Topic", "Entity", "Fact", "File", "SourceArtifact", "Module", "Function", "Class", "Interface", "Method", "Variable", "Endpoint", "Schema", "StaticAnalysisFinding"}
CODE_CONTEXT_NODE_TYPES = {
    "SourceArtifact",
    "SourceFragment",
    "File",
    "Module",
    "Function",
    "Class",
    "Interface",
    "Method",
    "Variable",
    "Endpoint",
    "Schema",
    "Config",
    "Test",
    "Dependency",
    "StaticAnalysisFinding",
}
CODE_CONTEXT_EXCLUDED_NODE_TYPES = {
    "Entity",
    "Fact",
    "Topic",
}
SEMANTIC_EDGE_TYPES = {
    "ABOUT",
    "HAS_TOPIC",
    "MENTIONS",
    "DERIVED_FROM",
    "RELATED_TO",
    "IS_A",
    "LIKES",
    "SUPPORTS",
    "EXPRESSES",
    "EXPLAINS",
    "EVIDENCED_BY",
    "SUPPORTED_BY",
    "PART_OF",
    "INSTANCE_OF",
    "CONFIRMS",
    "CORRECTS",
    "SUPERSEDES",
    "UPDATED_BY",
    "TRACKS",
}
TECHNICAL_EDGE_TYPES = {
    "GENERATED_BY_QUERY",
    "USED_IN_CONTEXT",
    "COMPILED_IN",
    "AFFECTED_BY_DELTA",
    "ASSOCIATED_WITH",
}
CODE_CONTEXT_EDGE_TYPES = {
    "CALLS",
    "CONTAINS",
    "DEFINES",
    "DEPENDS_ON",
    "EVIDENCED_BY",
    "HANDLES_ROUTE",
    "HAS_FINDING",
    "HAS_SECTION",
    "IMPORTS",
    "IMPLEMENTS",
    "IMPORTS_FROM",
    "INHERITS",
    "INSTANTIATES",
    "OVERRIDES",
    "METHOD",
    "RAISES",
    "READS",
    "REFERENCES",
    "RE_EXPORTS",
    "RETURNS",
    "WRITES",
    "WRAPS",
}
DEFAULT_CONTEXT_EDGE_TYPES = SEMANTIC_EDGE_TYPES | CODE_CONTEXT_EDGE_TYPES
SOURCE_NODE_TYPES = {"SourceFragment", "DocumentFragment"}
SOURCE_EDGE_TYPES = {"EVIDENCED_BY", "DERIVED_FROM", "SUPPORTED_BY", "CONTAINS_FRAGMENT", "HAS_SECTION", "HAS_DOCSTRING", "HAS_COMMENT"}
QUERY_EXPLORE_VIEWS = {"owners", "callers", "public_surface", "serialization_paths", "docs_mentions", "structural_duplicates", "code"}
QUERY_EXPLORE_DEFAULT_VIEWS = ("owners", "callers", "public_surface", "serialization_paths", "docs_mentions", "code")
QUERY_EXPLORE_ALL_VIEWS = (*QUERY_EXPLORE_DEFAULT_VIEWS[:-1], "structural_duplicates", "code")
QUERY_EXPLORE_EDGE_TYPES = CODE_CONTEXT_EDGE_TYPES | SOURCE_EDGE_TYPES | {"DECORATED_BY", "HAS_CODE_BLOCK", "HAS_COMMENT", "HAS_DOCSTRING", "TESTS"}
OWNER_EDGE_TYPES = {"CONTAINS", "DEFINES", "EVIDENCED_BY", "HAS_FINDING", "METHOD"}
CALLER_EDGE_TYPES = {"CALLS", "INSTANTIATES"}
PUBLIC_SURFACE_EDGE_TYPES = {"HANDLES_ROUTE", "IMPLEMENTS", "IMPORTS", "IMPORTS_FROM", "RE_EXPORTS"}
SERIALIZATION_EDGE_TYPES = {"READS", "WRITES", "RETURNS", "RAISES", "REFERENCES", "EVIDENCED_BY", "DEPENDS_ON", "IMPORTS_FROM"}
QUERY_CONTEXT_MODES = {"informative", "cleanup"}
QUERY_CONTEXT_SCOPES = {"code", "docs", "test"}
QUERY_CONTEXT_MIN_CONFIDENCE_SCORE = 0.25
QUERY_CONTEXT_MAX_RENDERED_FILES = 8
QUERY_CONTEXT_MAX_RENDERED_TEST_FILES = 3
STRUCTURED_SEARCH_FIELDS = (
    "name",
    "qualified_name",
    "symbol_name",
    "module",
    "target",
    "relative_path",
    "source_file",
    "language",
    "kind",
    "args",
    "bases",
    "returns",
    "semantic_roles",
    "wrapper_targets",
    "overrides",
    "re_exports",
)
CODE_CONTEXT_GENERIC_QUERY_TOKENS = {
    "agent",
    "base",
    "bug",
    "change",
    "changes",
    "code",
    "context",
    "edit",
    "edits",
    "fix",
    "fixes",
    "function",
    "functions",
    "implementation",
    "implement",
    "issue",
    "logic",
    "noise",
    "problem",
    "query",
    "request",
    "task",
    "unrelated",
    "workflow",
}
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_CANONICAL_TOKEN_RE = re.compile(r"[a-z0-9_][a-z0-9_'-]{1,}")



@lru_cache(maxsize=32768)
def _expanded_tokens(value: str) -> tuple[str, ...]:
    tokens: list[str] = []
    seen: set[str] = set()
    for candidate in (value, _identifier_expanded_text(value)):
        for token in tokenize(candidate):
            for variant in _token_variants(token):
                if variant not in seen:
                    seen.add(variant)
                    tokens.append(variant)
    return tuple(tokens)


def _code_context_query_tokens(value: str) -> set[str]:
    tokens = set(_expanded_tokens(value))
    filtered = {token for token in tokens if token not in CODE_CONTEXT_GENERIC_QUERY_TOKENS}
    return filtered or tokens


def _identifier_expanded_text(value: str) -> str:
    separated = str(value).replace("\\", " ").replace("/", " ").replace(".", " ")
    separated = separated.replace("_", " ").replace("-", " ").replace(":", " ")
    return _CAMEL_BOUNDARY_RE.sub(" ", separated)


def _token_variants(token: str) -> tuple[str, ...]:
    variants = [token]
    singular = _singular_token(token)
    if singular and singular != token:
        variants.append(singular)
    return tuple(variants)


def _singular_token(token: str) -> str | None:
    if len(token) <= 3:
        return None
    if token.endswith("ies") and len(token) > 4:
        return f"{token[:-3]}y"
    if token.endswith("sses"):
        return None
    if token.endswith("ses") and len(token) > 4:
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return None


def _canonical_token_overlap(value: str, query_tokens: set[str]) -> set[str]:
    """Return expanded query-token matches from already-canonical search text."""
    if not value or not query_tokens:
        return set()
    overlap: set[str] = set()
    for match in _CANONICAL_TOKEN_RE.finditer(value):
        token = match.group(0).strip("_-")
        if len(token) < 2:
            continue
        for variant in _token_variants(token):
            if variant in query_tokens:
                overlap.add(variant)
    return overlap


def _raw_query_token_overlap(parts: Sequence[str], query_tokens: set[str]) -> set[str] | None:
    """Return a conservative cheap overlap, or ``None`` when Unicode needs normalization."""
    raw = " ".join(parts)
    if not raw.isascii():
        return None
    folded = raw.casefold()
    overlap: set[str] = set()
    for token in query_tokens:
        needles = (token, f"{token[:-1]}ie") if token.endswith("y") and len(token) > 2 else (token,)
        if any(needle in folded for needle in needles):
            overlap.add(token)
    return overlap



__all__ = [name for name in globals() if not name.startswith("__")]
