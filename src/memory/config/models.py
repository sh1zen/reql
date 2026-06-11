"""Configuration domain objects for REQL."""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Mapping, get_args, get_origin, get_type_hints

DEFAULT_DOCUMENT_POLICIES: list[dict[str, Any]] = [
    {"format": "markdown", "extensions": [".md", ".markdown"], "ingest": True},
    {"format": "plain_text", "extensions": [".txt"], "filenames": ["LICENSE", "NOTICE"], "ingest": True},
    {"format": "restructured_text", "extensions": [".rst"], "ingest": True},
    {"format": "html", "extensions": [".html", ".htm"], "ingest": True},
    {"format": "log", "extensions": [".log"], "ingest": True},
    {"format": "pdf", "extensions": [".pdf"], "ingest": True},
    {"format": "json", "extensions": [".json"], "ingest": True},
    {"format": "toml", "extensions": [".toml"], "ingest": True},
    {"format": "yaml", "extensions": [".yaml", ".yml"], "ingest": True},
    {"format": "ini", "extensions": [".ini", ".cfg", ".conf"], "ingest": True},
    {"format": "csv", "extensions": [".csv"], "ingest": True},
    {"format": "tsv", "extensions": [".tsv"], "ingest": True},
    {"format": "xml", "extensions": [".xml"], "ingest": True},
    {"format": "ndjson", "extensions": [".ndjson"], "ingest": True},
]


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    id: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id}


@dataclass(frozen=True, slots=True)
class ScanConfig:
    max_file_size_mb: float
    include: list[str]
    exclude: list[str]

    @property
    def max_file_size_bytes(self) -> int:
        return max(0, int(self.max_file_size_mb * 1024 * 1024))

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_file_size_mb": self.max_file_size_mb,
            "include": list(self.include),
            "exclude": list(self.exclude),
        }


@dataclass(frozen=True, slots=True)
class CompileConfig:
    ingest_documents: bool
    documents: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ingest_documents": self.ingest_documents,
            "documents": [dict(item) for item in self.documents],
        }


@dataclass(frozen=True, slots=True)
class CacheConfig:
    enabled: bool
    fingerprint_strategy: str

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "fingerprint_strategy": self.fingerprint_strategy}


@dataclass(frozen=True, slots=True)
class AnalysisConfig:
    enable_hubs: bool
    enable_communities: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "enable_hubs": self.enable_hubs,
            "enable_communities": self.enable_communities,
        }


@dataclass(frozen=True, slots=True)
class ReportingConfig:
    output_dir: str

    def to_dict(self) -> dict[str, Any]:
        return {"output_dir": self.output_dir}


@dataclass(frozen=True, slots=True)
class DiagnosticsConfig:
    enabled: bool
    path: str

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "path": self.path}


@dataclass(frozen=True, slots=True)
class REQLConfig:
    project: ProjectConfig
    scan: ScanConfig
    compile: CompileConfig
    cache: CacheConfig
    analysis: AnalysisConfig
    reporting: ReportingConfig
    diagnostics: DiagnosticsConfig

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project.to_dict(),
            "scan": self.scan.to_dict(),
            "compile": self.compile.to_dict(),
            "cache": self.cache.to_dict(),
            "analysis": self.analysis.to_dict(),
            "reporting": self.reporting.to_dict(),
            "diagnostics": self.diagnostics.to_dict(),
        }

    def with_overrides(self, overrides: Mapping[str, Any] | None = None, **kwargs: Any) -> "REQLConfig":
        merged: dict[str, Any] = {}
        if overrides:
            merged.update(overrides)
        merged.update(kwargs)
        return merge_config(self, merged)


SECTION_TYPES = {
    "project": ProjectConfig,
    "scan": ScanConfig,
    "compile": CompileConfig,
    "cache": CacheConfig,
    "analysis": AnalysisConfig,
    "reporting": ReportingConfig,
    "diagnostics": DiagnosticsConfig,
}

DEFAULT_CONFIG_DATA: dict[str, dict[str, Any]] = {
    "project": {"id": "default"},
    "scan": {"max_file_size_mb": 10.0, "include": [], "exclude": []},
    "compile": {"ingest_documents": True, "documents": [dict(item) for item in DEFAULT_DOCUMENT_POLICIES]},
    "cache": {"enabled": True, "fingerprint_strategy": "sha256"},
    "analysis": {"enable_hubs": True, "enable_communities": True},
    "reporting": {"output_dir": "reports"},
    "diagnostics": {"enabled": False, "path": ""},
}


def merge_config(config: REQLConfig, overrides: Mapping[str, Any]) -> REQLConfig:
    """Return a config copy with dotted-key or nested override values applied."""

    nested: dict[str, dict[str, Any]] = {}
    for key, value in overrides.items():
        if value is None:
            continue
        if "." in key:
            section, option = key.split(".", 1)
            nested.setdefault(section, {})[option] = value
        elif isinstance(value, Mapping):
            nested.setdefault(key, {}).update(dict(value))
        else:
            raise ValueError(f"Config override must be dotted or nested: {key}")

    current = config.to_dict()
    for section, values in nested.items():
        if section not in SECTION_TYPES:
            raise ValueError(f"Unknown config section: {section}")
        for option, value in values.items():
            if option not in current[section]:
                raise ValueError(f"Unknown config option: {section}.{option}")
            current[section][option] = value
    return config_from_mapping(current)


def config_from_mapping(data: Mapping[str, Any]) -> REQLConfig:
    """Build a validated config object from parsed config data."""

    allowed_sections = set(SECTION_TYPES)
    unknown_sections = set(data) - allowed_sections
    if unknown_sections:
        raise ValueError(f"Unknown config section(s): {', '.join(sorted(unknown_sections))}")

    project = _section(ProjectConfig, data.get("project", {}), "project")
    scan = _section(ScanConfig, data.get("scan", {}), "scan")
    compile = _section(CompileConfig, data.get("compile", {}), "compile")
    cache = _section(CacheConfig, data.get("cache", {}), "cache")
    analysis = _section(AnalysisConfig, data.get("analysis", {}), "analysis")
    reporting = _section(ReportingConfig, data.get("reporting", {}), "reporting")
    diagnostics = _section(DiagnosticsConfig, data.get("diagnostics", {}), "diagnostics")
    cfg = REQLConfig(
        project=project,
        scan=scan,
        compile=compile,
        cache=cache,
        analysis=analysis,
        reporting=reporting,
        diagnostics=diagnostics,
    )
    _validate(cfg)
    return cfg


def _section(cls: type[Any], raw: object, section_name: str) -> Any:
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"Config section [{section_name}] must be a table")
    hints = get_type_hints(cls)
    option_names = {field.name for field in fields(cls)}
    raw_values = dict(raw)
    unknown = set(raw_values) - option_names
    if unknown:
        raise ValueError(f"Unknown config option(s) in [{section_name}]: {', '.join(sorted(unknown))}")
    default_values = dict(DEFAULT_CONFIG_DATA.get(section_name, {}))
    missing = option_names - set(raw_values) - set(default_values)
    if missing:
        raise ValueError(f"Missing config option(s) in [{section_name}]: {', '.join(sorted(missing))}")
    merged_values = {**default_values, **raw_values}
    values: dict[str, Any] = {}
    for key, value in merged_values.items():
        values[key] = _coerce_value(section_name, key, value, hints[key])
    return cls(**values)

def _coerce_value(section: str, key: str, value: Any, expected_type: Any) -> Any:
    if expected_type is bool:
        if not isinstance(value, bool):
            raise ValueError(f"Config option {section}.{key} must be a boolean")
        return value
    if expected_type is float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"Config option {section}.{key} must be a number")
        return float(value)
    if expected_type is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"Config option {section}.{key} must be an integer")
        return value
    if expected_type is str:
        if not isinstance(value, str):
            raise ValueError(f"Config option {section}.{key} must be a string")
        return value
    if get_origin(expected_type) is list and get_args(expected_type) == (str,):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"Config option {section}.{key} must be a list of strings")
        return list(value)
    if section == "compile" and key == "documents":
        return _coerce_document_policies(value)
    return value


def _coerce_document_policies(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("Config option compile.documents must be a list")
    policies: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            raise ValueError(f"Config option compile.documents[{index}] must be an object")
        unknown = set(item) - {"format", "extensions", "filenames", "ingest"}
        if unknown:
            raise ValueError(f"Unknown compile.documents[{index}] option(s): {', '.join(sorted(unknown))}")
        format_name = str(item.get("format") or "").strip().casefold()
        if not format_name:
            raise ValueError(f"Config option compile.documents[{index}].format must not be empty")
        if format_name in seen:
            raise ValueError(f"Duplicate compile.documents format: {format_name}")
        extensions = item.get("extensions", [])
        filenames = item.get("filenames", [])
        if not isinstance(extensions, list) or not all(isinstance(value, str) for value in extensions):
            raise ValueError(f"Config option compile.documents[{index}].extensions must be a list of strings")
        if not isinstance(filenames, list) or not all(isinstance(value, str) for value in filenames):
            raise ValueError(f"Config option compile.documents[{index}].filenames must be a list of strings")
        normalized_extensions = []
        for extension in extensions:
            normalized = extension.strip().casefold()
            if not normalized.startswith("."):
                raise ValueError(f"Config option compile.documents[{index}].extensions values must start with '.'")
            normalized_extensions.append(normalized)
        normalized_filenames = [filename.strip() for filename in filenames if filename.strip()]
        if not normalized_extensions and not normalized_filenames:
            raise ValueError(f"Config option compile.documents[{index}] must define extensions or filenames")
        ingest = item.get("ingest", True)
        if not isinstance(ingest, bool):
            raise ValueError(f"Config option compile.documents[{index}].ingest must be a boolean")
        policy = {"format": format_name, "extensions": normalized_extensions, "ingest": ingest}
        if normalized_filenames:
            policy["filenames"] = normalized_filenames
        policies.append(policy)
        seen.add(format_name)
    return policies


def _validate(config: REQLConfig) -> None:
    if not config.project.id.strip():
        raise ValueError("Config option project.id must not be empty")
    if config.scan.max_file_size_mb <= 0:
        raise ValueError("Config option scan.max_file_size_mb must be greater than zero")
    if config.cache.fingerprint_strategy != "sha256":
        raise ValueError("Only cache.fingerprint_strategy = \"sha256\" is currently supported")
    if config.diagnostics.enabled and not config.diagnostics.path.strip():
        raise ValueError("Config option diagnostics.path must not be empty when diagnostics.enabled is true")
