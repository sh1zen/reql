"""Canonical typed options for artifact compilation."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, TypeAlias

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

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "DocumentPolicy":
        return cls(
            format=str(value.get("format") or ""),
            extensions=_string_tuple(value.get("extensions")),
            filenames=_string_tuple(value.get("filenames")),
            ingest=bool(value.get("ingest", True)),
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
        policies = []
        for format_name, definition in compile_config.document_formats.items():
            policies.append(
                DocumentPolicy(
                    format=format_name,
                    extensions=tuple(definition.get("extensions", ())),
                    filenames=tuple(definition.get("filenames", ())),
                    ingest=bool(compile_config.documents.get(format_name, False)),
                )
            )
        return cls(
            ingest_documents=compile_config.ingest_documents,
            document_policies=tuple(policies),
            use_gitignore=scan_config.use_gitignore,
            ignore_defaults=scan_config.ignore_defaults,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object] | None) -> "CompilationOptions":
        raw = value or {}
        raw_compile = raw.get("compile")
        compile_settings = raw_compile if isinstance(raw_compile, Mapping) else {}
        raw_scan = raw.get("scan")
        scan_settings = raw_scan if isinstance(raw_scan, Mapping) else {}
        return cls(
            ingest_documents=bool(compile_settings.get("ingest_documents", True)),
            document_policies=_policies_from_compile_mapping(compile_settings),
            use_gitignore=bool(scan_settings.get("use_gitignore", False)),
            ignore_defaults=bool(scan_settings.get("ignore_defaults", False)),
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


RawCompilationOptions: TypeAlias = CompilationOptions | Mapping[str, object] | None


def normalize_compilation_options(value: RawCompilationOptions) -> CompilationOptions:
    if isinstance(value, CompilationOptions):
        return value
    return CompilationOptions.from_mapping(value)


def _policies_from_compile_mapping(settings: Mapping[str, object]) -> tuple[DocumentPolicy, ...]:
    documents = settings.get("documents")
    if isinstance(documents, list):
        return tuple(
            DocumentPolicy.from_mapping(item)
            for item in documents
            if isinstance(item, Mapping) and str(item.get("format") or "").strip()
        )
    document_formats = settings.get("document_formats")
    if not isinstance(documents, Mapping) or not isinstance(document_formats, Mapping):
        return ()
    policies = []
    for format_name, raw_definition in document_formats.items():
        if not isinstance(raw_definition, Mapping):
            continue
        policies.append(
            DocumentPolicy(
                format=str(format_name),
                extensions=_string_tuple(raw_definition.get("extensions")),
                filenames=_string_tuple(raw_definition.get("filenames")),
                ingest=bool(documents.get(format_name, False)),
            )
        )
    return tuple(policies)


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(str(item) for item in value)


def _normalized_values(values: tuple[str, ...], *, casefold: bool) -> list[str]:
    normalized: list[str] = []
    for raw in values:
        value = str(raw).strip()
        if casefold:
            value = value.casefold()
        if value and value not in normalized:
            normalized.append(value)
    return normalized
