"""Canonical typed options for artifact compilation."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from ..config.models import CompileConfig, REQLConfig, ScanConfig
from .models import SourceArtifact


@dataclass(frozen=True, slots=True)
class DocumentPolicy:
    """One normalized document-format policy used throughout compilation."""

    format: str
    extensions: tuple[str, ...] = ()
    filenames: tuple[str, ...] = ()
    ingest: bool = True

    def __post_init__(self) -> None:
        normalized_format = self.format.strip().casefold()
        if not normalized_format:
            raise ValueError("document policy format must not be empty")
        object.__setattr__(self, "format", normalized_format)
        object.__setattr__(
            self,
            "extensions",
            tuple(_normalized_values(self.extensions, casefold=True)),
        )
        object.__setattr__(
            self,
            "filenames",
            tuple(_normalized_values(self.filenames, casefold=True)),
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "format": self.format,
            "extensions": list(self.extensions),
            "ingest": self.ingest,
        }
        if self.filenames:
            payload["filenames"] = list(self.filenames)
        return payload


@dataclass(frozen=True, slots=True)
class CompilationOptions:
    """Single authoritative model for scan and artifact compilation policy."""

    ingest_documents: bool = True
    document_policies: tuple[DocumentPolicy, ...] = ()
    use_gitignore: bool = False
    ignore_defaults: bool = False
    _policies_by_format: Mapping[str, DocumentPolicy] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        policies = {policy.format: policy for policy in self.document_policies}
        normalized = tuple(policies.values())
        object.__setattr__(self, "document_policies", normalized)
        object.__setattr__(self, "_policies_by_format", policies)

    @classmethod
    def from_config(cls, config: REQLConfig) -> "CompilationOptions":
        return cls.from_sections(config.compile, config.scan)

    @classmethod
    def from_sections(cls, compile_config: CompileConfig, scan_config: ScanConfig) -> "CompilationOptions":
        policies = tuple(
            DocumentPolicy(
                format=format_name,
                extensions=tuple(definition.get("extensions", ())),
                filenames=tuple(definition.get("filenames", ())),
                ingest=bool(compile_config.documents.get(format_name, False)),
            )
            for format_name, definition in compile_config.document_formats.items()
        )
        return cls(
            ingest_documents=compile_config.ingest_documents,
            document_policies=policies,
            use_gitignore=scan_config.use_gitignore,
            ignore_defaults=scan_config.ignore_defaults,
        )

    def document_ingest_enabled(self, artifact: SourceArtifact) -> bool:
        if not self.ingest_documents:
            return False
        if not self.document_policies:
            return True
        policy = self.document_policy_for(artifact)
        return policy.ingest if policy is not None else False

    def document_policy_for(self, artifact: SourceArtifact) -> DocumentPolicy | None:
        path = Path(artifact.relative_path or artifact.path)
        filename = path.name.casefold()
        suffix = path.suffix.casefold()
        for policy in self.document_policies:
            if filename in policy.filenames:
                return policy
        detected = self._policies_by_format.get(artifact.artifact_type.casefold())
        if detected is not None:
            return detected
        for policy in self.document_policies:
            if suffix and suffix in policy.extensions:
                return policy
        return None

    def format_ingest_enabled(self, format_name: str) -> bool:
        if not self.document_policies:
            return self.ingest_documents
        policy = self._policies_by_format.get(format_name.strip().casefold())
        return bool(self.ingest_documents and policy is not None and policy.ingest)

    def cache_payload(self) -> dict[str, object]:
        """Return the sole serialized representation used for cache hashing."""
        return {
            "compile": {
                "ingest_documents": self.ingest_documents,
                "documents": [policy.to_dict() for policy in self.document_policies],
            }
        }


def normalize_compilation_options(value: CompilationOptions | None) -> CompilationOptions:
    """Resolve defaults and reject inputs outside the typed compilation contract."""
    if value is None:
        return CompilationOptions()
    if not isinstance(value, CompilationOptions):
        raise TypeError("compilation options must be CompilationOptions or None")
    return value


def _normalized_values(values: tuple[str, ...], *, casefold: bool) -> list[str]:
    normalized: list[str] = []
    for raw in values:
        value = str(raw).strip()
        if casefold:
            value = value.casefold()
        if value and value not in normalized:
            normalized.append(value)
    return normalized
