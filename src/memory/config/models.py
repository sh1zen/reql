"""Configuration domain objects for REQL."""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Mapping, get_args, get_origin, get_type_hints

from .path_rules import normalize_scan_exclude_pattern, resolve_scan_exclude_pattern


def normalize_scan_path_pattern(pattern: str) -> str:
    """Return the legacy canonical matching key for a scan include pattern.

    Exclusions use :func:`resolve_scan_exclude_pattern` so their ``./`` anchor
    remains significant.
    """

    normalized = pattern.strip().replace("\\", "/")
    previous = None
    while normalized != previous:
        previous = normalized
        normalized = normalized.lstrip("/")
        if normalized.startswith("./"):
            normalized = normalized[2:]
    return normalized.rstrip("/")


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    id: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id}


@dataclass(frozen=True, slots=True)
class ScanConfig:
    max_file_size_mb: float
    use_gitignore: bool
    ignore_defaults: bool
    include: list[str]
    exclude: list[str]

    @property
    def max_file_size_bytes(self) -> int:
        return max(0, int(self.max_file_size_mb * 1024 * 1024))

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_file_size_mb": self.max_file_size_mb,
            "use_gitignore": self.use_gitignore,
            "ignore_defaults": self.ignore_defaults,
            "include": list(self.include),
            "exclude": list(self.exclude),
        }


@dataclass(frozen=True, slots=True)
class CompileConfig:
    ingest_documents: bool
    documents: dict[str, bool]
    document_formats: dict[str, dict[str, list[str]]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ingest_documents": self.ingest_documents,
            "documents": dict(self.documents),
            "document_formats": {
                format_name: {key: list(values) for key, values in definition.items()}
                for format_name, definition in self.document_formats.items()
            },
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
        return merge_config(self, {**(overrides or {}), **kwargs})


SECTION_TYPES = {
    "project": ProjectConfig,
    "scan": ScanConfig,
    "compile": CompileConfig,
    "cache": CacheConfig,
    "analysis": AnalysisConfig,
    "reporting": ReportingConfig,
    "diagnostics": DiagnosticsConfig,
}

def merge_config(config: REQLConfig, overrides: Mapping[str, Any]) -> REQLConfig:
    """Apply scalar/list replacements except for protected scan-list joins."""

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
        scan_ignore_defaults = False
        if section == "scan":
            scan_ignore_defaults = bool(values.get("ignore_defaults", current[section].get("ignore_defaults", False)))
            if scan_ignore_defaults:
                current[section]["include"] = []
                current[section]["exclude"] = []
        for option, value in values.items():
            if option not in current[section]:
                raise ValueError(f"Unknown config option: {section}.{option}")
            current_value = current[section][option]
            if section == "compile" and option == "documents" and isinstance(value, Mapping):
                merged_mapping = dict(current_value) if isinstance(current_value, Mapping) else {}
                merged_mapping.update(_coerce_document_toggles(value))
                current[section][option] = merged_mapping
            elif section == "compile" and option == "document_formats" and isinstance(value, Mapping):
                merged_mapping = dict(current_value) if isinstance(current_value, Mapping) else {}
                merged_mapping.update(_coerce_document_formats(value))
                current[section][option] = merged_mapping
            elif isinstance(current_value, list) and isinstance(value, list):
                if section == "scan" and option in {"include", "exclude"} and scan_ignore_defaults:
                    current[section][option] = list(value)
                elif section == "scan" and option == "exclude":
                    current[section][option] = _join_scan_exclude_lists(current_value, value)
                else:
                    current[section][option] = _join_config_lists(current_value, value)
            else:
                current[section][option] = value
    return config_from_mapping(current)


def _join_config_lists(current: list[Any], additions: list[Any]) -> list[Any]:
    joined = list(current)
    for item in additions:
        if item not in joined:
            joined.append(item)
    return joined


def _join_scan_exclude_lists(current: list[Any], additions: list[Any]) -> list[Any]:
    joined: list[Any] = []
    seen: set[str] = set()
    for item in [*current, *additions]:
        key = normalize_scan_exclude_pattern(item)
        if key not in seen:
            joined.append(item)
            seen.add(key)
    return joined


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
    missing = option_names - set(raw_values)
    if missing:
        raise ValueError(f"Missing config option(s) in [{section_name}]: {', '.join(sorted(missing))}")
    values: dict[str, Any] = {}
    for key, value in raw_values.items():
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
        return _coerce_document_toggles(value)
    if section == "compile" and key == "document_formats":
        return _coerce_document_formats(value)
    return value


def _coerce_document_toggles(value: Any) -> dict[str, bool]:
    if not isinstance(value, Mapping):
        raise ValueError("Config option compile.documents must be a format-to-boolean mapping")
    toggles: dict[str, bool] = {}
    for raw_format, enabled in value.items():
        format_name = str(raw_format).strip().casefold()
        if not format_name:
            raise ValueError("Config option compile.documents format names must not be empty")
        if not isinstance(enabled, bool):
            raise ValueError(f"Config option compile.documents.{format_name} must be a boolean")
        toggles[format_name] = enabled
    return toggles


def _coerce_document_formats(value: Any) -> dict[str, dict[str, list[str]]]:
    if not isinstance(value, Mapping):
        raise ValueError("Config option compile.document_formats must be a mapping")
    formats: dict[str, dict[str, list[str]]] = {}
    for raw_format, item in value.items():
        format_name = str(raw_format).strip().casefold()
        if not format_name:
            raise ValueError("Config option compile.document_formats format names must not be empty")
        if not isinstance(item, Mapping):
            raise ValueError(f"Config option compile.document_formats.{format_name} must be an object")
        unknown = set(item) - {"extensions", "filenames"}
        if unknown:
            raise ValueError(
                f"Unknown compile.document_formats.{format_name} option(s): {', '.join(sorted(unknown))}"
            )
        extensions = item.get("extensions", [])
        filenames = item.get("filenames", [])
        if not isinstance(extensions, list) or not all(isinstance(value, str) for value in extensions):
            raise ValueError(f"Config option compile.document_formats.{format_name}.extensions must be a list of strings")
        if not isinstance(filenames, list) or not all(isinstance(value, str) for value in filenames):
            raise ValueError(f"Config option compile.document_formats.{format_name}.filenames must be a list of strings")
        normalized_extensions = []
        for extension in extensions:
            normalized = extension.strip().casefold()
            if not normalized.startswith("."):
                raise ValueError(
                    f"Config option compile.document_formats.{format_name}.extensions values must start with '.'"
                )
            normalized_extensions.append(normalized)
        normalized_filenames = [filename.strip() for filename in filenames if filename.strip()]
        if not normalized_extensions and not normalized_filenames:
            raise ValueError(
                f"Config option compile.document_formats.{format_name} must define extensions or filenames"
            )
        formats[format_name] = {
            "extensions": normalized_extensions,
            "filenames": normalized_filenames,
        }
    return formats


def _validate(config: REQLConfig) -> None:
    if not config.project.id.strip():
        raise ValueError("Config option project.id must not be empty")
    if config.scan.max_file_size_mb <= 0:
        raise ValueError("Config option scan.max_file_size_mb must be greater than zero")
    for pattern in config.scan.exclude:
        resolve_scan_exclude_pattern(pattern)
    if config.cache.fingerprint_strategy != "sha256":
        raise ValueError("Only cache.fingerprint_strategy = \"sha256\" is currently supported")
    unknown_document_formats = set(config.compile.documents) - set(config.compile.document_formats)
    if unknown_document_formats:
        raise ValueError(
            "Unknown compile.documents format(s): " + ", ".join(sorted(unknown_document_formats))
        )
    if config.diagnostics.enabled and not config.diagnostics.path.strip():
        raise ValueError("Config option diagnostics.path must not be empty when diagnostics.enabled is true")
