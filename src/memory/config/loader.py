"""Load and initialize REQL configuration files."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from .models import REQLConfig, config_from_mapping, merge_config

CONFIG_FILENAME = "conf.yaml"
PROJECT_CONFIG_FILENAME = "reql.conf"
LOCAL_CONFIG_FILENAME = PROJECT_CONFIG_FILENAME
CONFIG_PATH_ENV = "REQL_CONFIG"
CONFIG_OVERRIDES_ENV = "REQL_CONFIG_OVERRIDES"


class ConfigError(ValueError):
    """Raised when a REQL configuration file is invalid."""


def find_config_path(start_dir: str | Path | None = None) -> Path | None:
    """Search upward from ``start_dir`` for the nearest project ``reql.conf``."""

    current = Path(start_dir or Path.cwd()).expanduser().resolve(strict=False)
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        candidate = directory / PROJECT_CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def canonical_config_path() -> Path:
    """Return the repository canonical ``conf.yaml`` path."""

    candidate = Path(__file__).resolve().with_name(CONFIG_FILENAME)
    if candidate.is_file():
        return candidate
    raise ConfigError(f"Canonical {CONFIG_FILENAME} was not found")


def load_config(path: str | Path | None = None, *, start_dir: str | Path | None = None) -> REQLConfig:
    """Load protected internal defaults joined with one project config."""

    config = _load_config_file(canonical_config_path())
    if path:
        config_path = Path(path).expanduser().resolve(strict=False)
        if not config_path.exists():
            raise ConfigError(f"Configuration file not found: {config_path}")
    else:
        config_path = find_config_path(start_dir)
    if config_path is not None:
        config = _merge_config_file(config, config_path)
    return config


def default_config() -> REQLConfig:
    """Return deterministic defaults from the protected internal config."""

    return _load_config_file(canonical_config_path())


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


def _merge_config_file(config: REQLConfig, override_path: Path) -> REQLConfig:
    """Merge a partial YAML config file over an already validated config."""

    try:
        overrides = _load_yaml(override_path)
        return merge_config(config, overrides)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"Invalid configuration in {override_path}: {exc}") from exc


def load_project_config_data(path: str | Path) -> dict[str, Any]:
    """Load and validate the raw settings owned by one project config file."""

    config_path = Path(path).expanduser().resolve(strict=False)
    if not config_path.is_file():
        raise ConfigError(f"Configuration file not found: {config_path}")
    try:
        data = _load_yaml(config_path)
        merge_config(default_config(), data)
        return data
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
    selected_path = path or env_path or None
    config = load_config(selected_path, start_dir=start_dir)

    env_overrides = env_values.get(CONFIG_OVERRIDES_ENV)
    try:
        if env_overrides:
            config = merge_config(config, parse_config_overrides(env_overrides))
        if overrides:
            config = merge_config(config, overrides)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    return config


def write_sample_config(path: str | Path = PROJECT_CONFIG_FILENAME, *, overwrite: bool = False) -> Path:
    """Create a project ``reql.conf`` initialized from the internal defaults."""

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
    """Parse the small YAML subset used by REQL config files without PyYAML."""

    return _parse_basic_yaml(path.read_text(encoding="utf-8"), path)


YAML_ASSIGN_RE = re.compile(r"^([A-Za-z0-9_.-]+):(?:\s+(.*))?$")


def _parse_basic_yaml(text: str, path: Path) -> dict[str, Any]:
    lines: list[tuple[int, str, int, str]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            raise ConfigError(f"Invalid YAML syntax in {path} at line {line_number}: tabs are not supported")
        line = _strip_comment(raw_line).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent % 2:
            raise ConfigError(f"Invalid YAML syntax in {path} at line {line_number}: indentation must use two spaces")
        lines.append((indent, line.strip(), line_number, raw_line))

    if not lines:
        return {}

    def parse_block(index: int, expected_indent: int) -> tuple[Any, int]:
        is_list = lines[index][1].startswith("- ")
        container: Any = [] if is_list else {}
        while index < len(lines):
            indent, stripped, line_number, raw_line = lines[index]
            if indent < expected_indent:
                break
            if indent > expected_indent:
                raise ConfigError(
                    f"Invalid YAML syntax in {path} at line {line_number}: unexpected indentation"
                )
            if is_list:
                if not stripped.startswith("- "):
                    raise ConfigError(f"Invalid YAML syntax in {path} at line {line_number}: mixed list and mapping")
                item = stripped[2:].strip()
                if not item:
                    raise ConfigError(f"Invalid YAML syntax in {path} at line {line_number}: empty list item")
                container.append(_parse_yaml_value(item, path, line_number))
                index += 1
                continue
            if stripped.startswith("- "):
                raise ConfigError(f"Invalid YAML syntax in {path} at line {line_number}: list item without a list option")
            assign_match = YAML_ASSIGN_RE.match(stripped)
            if not assign_match:
                raise ConfigError(f"Invalid YAML syntax in {path} at line {line_number}: {raw_line.strip()}")
            key, value = assign_match.groups()
            index += 1
            if value is not None:
                container[key] = _parse_yaml_value(value.strip(), path, line_number)
                continue
            if index < len(lines) and lines[index][0] > expected_indent:
                child_indent = lines[index][0]
                if child_indent != expected_indent + 2:
                    child_line = lines[index][2]
                    raise ConfigError(
                        f"Invalid YAML syntax in {path} at line {child_line}: expected two-space indentation"
                    )
                container[key], index = parse_block(index, child_indent)
            else:
                container[key] = {}
        return container, index

    data, final_index = parse_block(0, 0)
    if final_index != len(lines) or not isinstance(data, dict):
        line_number = lines[final_index][2] if final_index < len(lines) else lines[0][2]
        raise ConfigError(f"Invalid YAML syntax in {path} at line {line_number}: top level must be a mapping")
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
