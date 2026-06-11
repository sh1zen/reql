"""Load and initialize REQL configuration files."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from .models import DEFAULT_CONFIG_DATA, REQLConfig, config_from_mapping, merge_config

CONFIG_FILENAME = "conf.yaml"
CONFIG_PATH_ENV = "REQL_CONFIG"
CONFIG_OVERRIDES_ENV = "REQL_CONFIG_OVERRIDES"


class ConfigError(ValueError):
    """Raised when a REQL configuration file is invalid."""


def find_config_path(start_dir: str | Path | None = None) -> Path | None:
    """Search upward from ``start_dir`` for a REQL config file."""

    current = Path(start_dir or Path.cwd()).expanduser().resolve(strict=False)
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        candidate = directory / CONFIG_FILENAME
        if candidate.exists():
            return candidate
    return None


def canonical_config_path() -> Path:
    """Return the repository canonical ``conf.yaml`` path."""

    for directory in Path(__file__).resolve().parents:
        candidate = directory / CONFIG_FILENAME
        if candidate.exists():
            return candidate
    raise ConfigError(f"Canonical {CONFIG_FILENAME} was not found")


def load_config(path: str | Path | None = None, *, start_dir: str | Path | None = None) -> REQLConfig:
    """Load a config file, falling back to the canonical project config."""

    if path:
        config_path = Path(path).expanduser().resolve(strict=False)
        if not config_path.exists():
            raise ConfigError(f"Configuration file not found: {config_path}")
    else:
        config_path = find_config_path(start_dir) or canonical_config_path()
    return _load_config_file(config_path)


def default_config() -> REQLConfig:
    """Return deterministic in-memory defaults without project-local opt-ins."""

    return config_from_mapping(DEFAULT_CONFIG_DATA)


def _load_config_file(config_path: Path) -> REQLConfig:
    try:
        if config_path.suffix.lower() not in {".yaml", ".yml"}:
            raise ConfigError(f"Unsupported configuration file type: {config_path}")
        data = _load_yaml(config_path)
        return config_from_mapping(data)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"Invalid configuration in {config_path}: {exc}") from exc


def load_effective_config(
    path: str | Path | None = None,
    *,
    start_dir: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> REQLConfig:
    """Load canonical config plus environment and caller-supplied overrides.

    Precedence is: canonical/discovered file config, environment overrides,
    explicit caller overrides.
    """

    env_values = os.environ if env is None else env
    env_path = env_values.get(CONFIG_PATH_ENV)
    config = load_config(path or env_path or None, start_dir=start_dir)

    env_overrides = env_values.get(CONFIG_OVERRIDES_ENV)
    try:
        if env_overrides:
            config = merge_config(config, parse_config_overrides(env_overrides))
        if overrides:
            config = merge_config(config, overrides)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    return config


def write_sample_config(path: str | Path = CONFIG_FILENAME, *, overwrite: bool = False) -> Path:
    """Copy the canonical ``conf.yaml`` if the target does not already exist."""

    target = Path(path).expanduser().resolve(strict=False)
    if target.exists() and not overwrite:
        raise FileExistsError(f"Config file already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(canonical_config_path().read_text(encoding="utf-8"), encoding="utf-8")
    return target


def merge_overrides(config: REQLConfig, overrides: Mapping[str, Any]) -> REQLConfig:
    return merge_config(config, overrides)


def parse_config_override_assignment(raw: str) -> dict[str, Any]:
    """Parse one ``section.option=value`` override assignment."""

    if "=" not in raw:
        raise ConfigError(f"Config override must use section.option=value: {raw}")
    key, value = raw.split("=", 1)
    key = key.strip()
    if "." not in key:
        raise ConfigError(f"Config override must use a dotted option name: {raw}")
    if not key:
        raise ConfigError(f"Config override has an empty option name: {raw}")
    return {key: _parse_override_value(value.strip())}


def parse_config_override_assignments(values: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Parse repeated CLI-style config override assignments."""

    overrides: dict[str, Any] = {}
    for raw in values:
        overrides.update(parse_config_override_assignment(raw))
    return overrides


def parse_config_overrides(raw: str) -> dict[str, Any]:
    """Parse JSON or assignment-style environment config overrides."""

    text = raw.strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Invalid {CONFIG_OVERRIDES_ENV} JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise ConfigError(f"{CONFIG_OVERRIDES_ENV} JSON must be an object")
        return value

    overrides: dict[str, Any] = {}
    for item in re.split(r"[;\n]+", text):
        item = item.strip()
        if item:
            overrides.update(parse_config_override_assignment(item))
    return overrides


def _load_yaml(path: Path) -> dict[str, Any]:
    """Parse the small YAML subset used by ``conf.yaml`` without PyYAML."""

    return _parse_basic_yaml(path.read_text(encoding="utf-8"), path)


YAML_SECTION_RE = re.compile(r"^([A-Za-z0-9_.-]+):\s*$")
YAML_ASSIGN_RE = re.compile(r"^([A-Za-z0-9_.-]+):(?:\s+(.*))?$")


def _parse_basic_yaml(text: str, path: Path) -> dict[str, Any]:
    data: dict[str, dict[str, Any]] = {}
    section: str | None = None
    list_key: str | None = None
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            raise ConfigError(f"Invalid YAML syntax in {path} at line {line_number}: tabs are not supported")
        line = _strip_comment(raw_line).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0:
            section_match = YAML_SECTION_RE.match(stripped)
            if not section_match:
                raise ConfigError(f"Invalid YAML syntax in {path} at line {line_number}: {raw_line.strip()}")
            section = section_match.group(1)
            data.setdefault(section, {})
            list_key = None
            continue
        if section is None:
            raise ConfigError(f"Invalid YAML syntax in {path} at line {line_number}: option outside a section")
        if stripped.startswith("- "):
            if list_key is None:
                raise ConfigError(f"Invalid YAML syntax in {path} at line {line_number}: list item without a list option")
            if indent not in {2, 4}:
                raise ConfigError(f"Invalid YAML syntax in {path} at line {line_number}: expected list indentation")
            data[section][list_key].append(_parse_yaml_value(stripped[2:].strip(), path, line_number))
            continue
        if indent != 2:
            raise ConfigError(f"Invalid YAML syntax in {path} at line {line_number}: expected two-space indentation")
        assign_match = YAML_ASSIGN_RE.match(stripped)
        if not assign_match:
            raise ConfigError(f"Invalid YAML syntax in {path} at line {line_number}: {raw_line.strip()}")
        key, value = assign_match.groups()
        if value is None:
            data[section][key] = []
            list_key = key
            continue
        data[section][key] = _parse_yaml_value(value.strip(), path, line_number)
        list_key = None
    return data


def _parse_yaml_value(raw: str, path: Path, line_number: int) -> Any:
    if raw in {"true", "false"}:
        return raw == "true"
    if raw in {"[]", "{}"}:
        return [] if raw == "[]" else {}
    if raw.startswith("[") or raw.startswith("{"):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Invalid JSON value in {path} at line {line_number}: {exc}") from exc
        return value
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        if raw.startswith('"'):
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"Invalid string value in {path} at line {line_number}: {exc}") from exc
        return raw[1:-1].replace("''", "'")
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        return raw


def _strip_comment(line: str) -> str:
    in_string = False
    escaped = False
    result: list[str] = []
    for char in line:
        if char == "\\" and in_string and not escaped:
            escaped = True
            result.append(char)
            continue
        if char == '"' and not escaped:
            in_string = not in_string
        if char == "#" and not in_string:
            break
        result.append(char)
        escaped = False
    return "".join(result)


def _parse_value(raw: str, path: Path, line_number: int) -> Any:
    if raw in {"true", "false"}:
        return raw == "true"
    if raw.startswith("[") or raw.startswith("{"):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Invalid JSON value in {path} at line {line_number}: {exc}") from exc
        return value
    if raw.startswith('"') and raw.endswith('"'):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Invalid string value in {path} at line {line_number}: {exc}") from exc
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError as exc:
        raise ConfigError(f"Unsupported value in {path} at line {line_number}: {raw}") from exc


def _parse_override_value(raw: str) -> Any:
    if raw == "":
        return ""
    try:
        return _parse_value(raw, Path("<config override>"), 1)
    except ConfigError:
        return raw
