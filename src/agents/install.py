"""Install REQL agent instructions for common coding assistants."""
from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import as_file, files
import importlib.util
from pathlib import Path
import json
import os
import platform as host_platform
import shutil
import shlex
import stat
import subprocess
import sys
from types import ModuleType
from typing import Iterable


SECTION_START = "<!-- REQL-INSTALL:START -->"
SECTION_END = "<!-- REQL-INSTALL:END -->"
HOOK_ID = "REQL_AGENT_HOOK_V1"
INSTALLER_VERSION = "1"
VERSION_FILE = ".reql_version"
COMMAND_MARKER = "REQL-COMMAND-SHIM:V1"
COMMAND_ENV = "REQL_COMMAND_DIR"
_SKILL_GENERATOR: ModuleType | None = None


@dataclass(frozen=True)
class InstallAction:
    platform: str
    scope: str
    kind: str
    path: Path
    status: str

    def to_dict(self) -> dict[str, str]:
        return {
            "platform": self.platform,
            "scope": self.scope,
            "kind": self.kind,
            "path": str(self.path),
            "status": self.status,
        }


@dataclass(frozen=True)
class InstallResult:
    platforms: tuple[str, ...]
    scope: str
    actions: tuple[InstallAction, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "platforms": list(self.platforms),
            "scope": self.scope,
            "actions": [action.to_dict() for action in self.actions],
        }


PLATFORMS_CONFIG = {
    "codex": {"label": "Codex", "is_all": True},
    "claude": {"label": "Claude Code", "is_all": True},
    "opencode": {"label": "OpenCode", "is_all": True},
    "kilo": {"label": "Kilo Code", "is_all": True},
    "cursor": {"label": "Cursor", "is_all": True},
    "gemini": {"label": "Gemini CLI", "is_all": True},
    "copilot": {
        "label": "GitHub Copilot CLI and VS Code Copilot Chat",
        "is_all": True,
    },
    "openclaw": {"label": "OpenClaw", "is_all": True},
    "hermes": {"label": "Hermes", "is_all": True},
    "kimi": {"label": "Kimi Code", "is_all": True},
    "antigravity": {"label": "Google Antigravity", "is_all": True},
    "agents": {"label": "generic AGENTS-compatible clients", "is_all": False},
}

def resolve_platforms(
    values: Iterable[str],
    *,
    install_all: bool = False,
    auto_detect: bool = False,
    project: bool = False,
    project_dir: Path | None = None,
    home_dir: Path | None = None,
) -> tuple[str, ...]:
    all_platforms = [name for name, cfg in PLATFORMS_CONFIG.items() if cfg["is_all"]]

    raw: list[str] = []
    if install_all:
        raw.extend(all_platforms)
    for value in values:
        raw.extend(part.strip().casefold() for part in value.split(",") if part.strip())
    if not raw:
        if auto_detect:
            raw.extend(detect_platforms(project=project, project_dir=project_dir, home_dir=home_dir))
        else:
            raw.append("codex")

    resolved: list[str] = []
    for name in raw:
        if name == "all":
            candidates = all_platforms
        else:
            candidates = (name,)
        for candidate in candidates:
            if candidate not in PLATFORMS_CONFIG:
                supported_list = ", ".join(PLATFORMS_CONFIG)
                raise ValueError(f"unknown platform '{candidate}'. Choose from: {supported_list}, all")
            if candidate not in resolved:
                resolved.append(candidate)
    return tuple(resolved)


def detect_platforms(*, project: bool = False, project_dir: Path | None = None, home_dir: Path | None = None) -> tuple[str, ...]:
    """Detect supported coding-agent profiles or commands already present locally."""
    home = (home_dir or Path.home()).expanduser()
    root = (project_dir or Path(".")).resolve()
    candidates: list[str] = []

    def add(
        name: str,
        *,
        project_paths: tuple[Path, ...] = (),
        user_paths: tuple[Path, ...] = (),
        commands: tuple[str, ...] = (),
    ) -> None:
        if name in candidates:
            return
        paths = project_paths if project else user_paths
        command_signals = () if project else commands
        if any(_is_agent_profile_signal(path) for path in paths) or any(shutil.which(command) for command in command_signals):
            candidates.append(name)

    claude_dir = _env_path("CLAUDE_CONFIG_DIR", home / ".claude")
    add(
        "codex",
        user_paths=(home / ".codex" / "skills",),
        project_paths=(root / ".codex" / "skills", root / ".codex" / "hooks.json"),
    )
    add(
        "claude",
        user_paths=(claude_dir / "skills", claude_dir / "CLAUDE.md"),
        project_paths=(root / ".claude" / "skills", root / ".claude" / "CLAUDE.md", root / ".claude" / "settings.json"),
        commands=("claude",),
    )
    add(
        "opencode",
        user_paths=(home / ".config" / "opencode" / "skills",),
        project_paths=(root / ".opencode" / "skills",),
        commands=("opencode",),
    )
    add(
        "kilo",
        user_paths=(home / ".kilocode" / "skills", home / ".config" / "kilo" / "skills"),
        project_paths=(root / ".kilocode" / "skills", root / ".config" / "kilo" / "skills"),
        commands=("kilo", "kilocode"),
    )
    add("cursor", user_paths=(home / ".cursor" / "rules",), project_paths=(root / ".cursor" / "rules",), commands=("cursor",))
    add(
        "gemini",
        user_paths=(_gemini_user_dir(home) / "skills", home / ".gemini" / "skills", home / "GEMINI.md"),
        project_paths=(root / ".gemini" / "skills", root / "GEMINI.md"),
        commands=("gemini",),
    )
    add(
        "copilot",
        user_paths=(home / ".copilot" / "skills", home / ".github" / "copilot-instructions.md", home / ".github" / "instructions"),
        project_paths=(root / ".github" / "copilot-instructions.md", root / ".github" / "instructions"),
        commands=("copilot",),
    )
    add("openclaw", user_paths=(home / ".openclaw" / "skills",), project_paths=(root / ".openclaw" / "skills",), commands=("openclaw",))
    add("hermes", user_paths=(home / ".hermes" / "skills",), project_paths=(root / ".hermes" / "skills",), commands=("hermes",))
    add("kimi", user_paths=(home / ".kimi" / "skills",), project_paths=(root / ".kimi" / "skills",), commands=("kimi",))
    add(
        "antigravity",
        user_paths=(home / ".gemini" / "config" / "skills",),
        project_paths=(root / ".agents" / "skills", root / ".agents" / "rules", root / ".agents" / "workflows"),
        commands=("antigravity",),
    )
    add("agents", user_paths=(home / ".agents" / "skills",), project_paths=(root / ".agents" / "skills",))

    if project:
        return tuple(candidates)
    return tuple(name for name in candidates if name != "agents" or (home / ".agents" / "skills").exists())


def _is_agent_profile_signal(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_dir():
        return _directory_has_agent_profile_signal(path)
    return not _is_reql_owned_file(path)


def _directory_has_agent_profile_signal(path: Path) -> bool:
    try:
        entries = list(path.iterdir())
    except OSError:
        return False
    if not entries:
        return True
    return any(not _is_reql_owned_path(entry) for entry in entries)


def _is_reql_owned_path(path: Path) -> bool:
    if path.name == "reql-agent":
        return True
    if path.is_file():
        return _is_reql_owned_file(path)
    return False


def _is_reql_owned_file(path: Path) -> bool:
    if path.name == VERSION_FILE:
        return _is_reql_version_file(path)
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if SECTION_START in text and not _remove_section(text).strip():
        return True
    if path.suffix.casefold() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return False
        return not bool(_strip_reql_hooks(data))
    return False


def _is_reql_version_file(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("installer") == "reql-install"


def _strip_reql_hooks(value: object, *, in_list: bool = False) -> object | None:
    if in_list and _is_reql_hook(value):
        return None
    if isinstance(value, dict):
        cleaned = {key: cleaned_value for key, item in value.items() if (cleaned_value := _strip_reql_hooks(item)) is not None}
        return cleaned or None
    if isinstance(value, list):
        cleaned_list = [cleaned_value for item in value if (cleaned_value := _strip_reql_hooks(item, in_list=True)) is not None]
        return cleaned_list or None
    if _is_reql_hook(value):
        return None
    return value


def install_agent_files(
    platforms: Iterable[str],
    *,
    project: bool = False,
    project_dir: Path | None = None,
    home_dir: Path | None = None,
    command_dir: Path | None = None,
    dry_run: bool = False,
    hooks: bool = True,
) -> InstallResult:
    selected = tuple(platforms)
    scope = "project" if project else "user"
    root = (project_dir or Path(".")).resolve()
    actions: list[InstallAction] = []
    command_plan = _command_plan(command_dir)

    for path, content in command_plan.files:
        status = _write_command_file(path, content, dry_run=dry_run)
        actions.append(InstallAction(platform="shared", scope=scope, kind="command", path=path, status=status))

    for name in selected:
        for kind, path, content in _planned_files(
            name,
            project=project,
            project_dir=root,
            home_dir=home_dir,
            command_name=command_plan.command_name,
            command_path=command_plan.primary_path,
            fallback_command=command_plan.fallback_command,
        ):
            status = _write_file(path, content, sectioned=kind == "instructions", dry_run=dry_run)
            actions.append(InstallAction(platform=name, scope=scope, kind=kind, path=path, status=status))
        if hooks:
            hook_action = _install_hook(name, project=project, project_dir=root, home_dir=home_dir, dry_run=dry_run)
            if hook_action is not None:
                actions.append(InstallAction(platform=name, scope=scope, kind="hook", path=hook_action[0], status=hook_action[1]))
        for stamp_path in _version_stamp_paths(name, project=project, project_dir=root, home_dir=home_dir):
            status = _write_file(
                stamp_path,
                _version_payload(name, scope, command_path=command_plan.primary_path),
                sectioned=False,
                dry_run=dry_run,
            )
            actions.append(InstallAction(platform=name, scope=scope, kind="version", path=stamp_path, status=status))

    return InstallResult(platforms=selected, scope=scope, actions=tuple(actions))


def uninstall_agent_files(
    platforms: Iterable[str],
    *,
    project: bool = False,
    project_dir: Path | None = None,
    home_dir: Path | None = None,
    command_dir: Path | None = None,
    dry_run: bool = False,
) -> InstallResult:
    selected = tuple(platforms)
    scope = "project" if project else "user"
    root = (project_dir or Path(".")).resolve()
    home = (home_dir or Path.home()).expanduser()
    actions: list[InstallAction] = []
    command_plan = _command_plan(command_dir)

    for path, _content in command_plan.files:
        status = _remove_command_file(path, dry_run=dry_run, stop=path.parent.parent)
        actions.append(InstallAction(platform="shared", scope=scope, kind="command", path=path, status=status))

    for name in selected:
        for kind, path, _content in _planned_files(name, project=project, project_dir=root, home_dir=home):
            if kind in {"instructions"}:
                status = _remove_section_file(path, dry_run=dry_run, stop=root if project else home)
            else:
                status = _remove_owned_file(path, dry_run=dry_run, stop=root if project else home)
            actions.append(InstallAction(platform=name, scope=scope, kind=kind, path=path, status=status))
        hook_action = _uninstall_hook(name, project=project, project_dir=root, home_dir=home, dry_run=dry_run)
        if hook_action is not None:
            actions.append(InstallAction(platform=name, scope=scope, kind="hook", path=hook_action[0], status=hook_action[1]))
        for stamp_path in _version_stamp_paths(name, project=project, project_dir=root, home_dir=home):
            status = _remove_owned_file(stamp_path, dry_run=dry_run, stop=root if project else home)
            actions.append(InstallAction(platform=name, scope=scope, kind="version", path=stamp_path, status=status))
            if not dry_run:
                _cleanup_empty_dirs(stamp_path.parent, stop=root if project else home)

    return InstallResult(platforms=selected, scope=scope, actions=tuple(actions))


def _planned_files(
    platform_name: str,
    *,
    project: bool,
    project_dir: Path,
    home_dir: Path | None = None,
    command_name: str = "reql",
    command_path: Path | None = None,
    fallback_command: str | None = None,
) -> list[tuple[str, Path, str]]:
    home = (home_dir or Path.home()).expanduser()
    command_path = command_path or _command_plan(None).primary_path
    fallback_command = fallback_command or _launcher_fallback_command()
    instructions = _instruction_section(platform_name, project=project, command_name=command_name, command_path=command_path, fallback_command=fallback_command)

    if platform_name == "codex":
        base = project_dir / ".codex" if project else home / ".codex"
        files = _skill_files(base=base, platform_name=platform_name, project=project, command_name=command_name, command_path=command_path, fallback_command=fallback_command)
        files.append(("instructions", (project_dir if project else home) / "AGENTS.md", instructions))
        return files

    if platform_name == "claude":
        base = project_dir / ".claude" if project else _env_path("CLAUDE_CONFIG_DIR", home / ".claude")
        files = _skill_files(base=base, platform_name=platform_name, project=project, command_name=command_name, command_path=command_path, fallback_command=fallback_command)
        claude_md = (project_dir / ".claude" / "CLAUDE.md") if project else base / "CLAUDE.md"
        files.append(("instructions", claude_md, instructions))
        return files

    if platform_name == "gemini":
        base = project_dir / ".gemini" if project else _gemini_user_dir(home)
        files = _skill_files(base=base, platform_name=platform_name, project=project, command_name=command_name, command_path=command_path, fallback_command=fallback_command)
        target = (project_dir / "GEMINI.md") if project else home / "GEMINI.md"
        files.append(("instructions", target, instructions))
        return files

    if platform_name == "cursor":
        base = project_dir / ".cursor" / "rules" if project else home / ".cursor" / "rules"
        return [("rule", base / "reql.mdc", _cursor_rule(command_name=command_name, command_path=command_path, fallback_command=fallback_command))]

    if platform_name == "copilot":
        target = project_dir / ".github" / "copilot-instructions.md" if project else home / ".github" / "copilot-instructions.md"
        vscode_target = project_dir / ".github" / "instructions" / "reql.instructions.md" if project else home / ".github" / "instructions" / "reql.instructions.md"
        return [
            ("instructions", target, instructions),
            ("rule", vscode_target, _vscode_copilot_rule(command_name=command_name, command_path=command_path, fallback_command=fallback_command)),
        ]

    if platform_name == "opencode":
        base = project_dir / ".opencode" if project else home / ".config" / "opencode"
        files = _skill_files(base=base, platform_name=platform_name, project=project, command_name=command_name, command_path=command_path, fallback_command=fallback_command)
        files.append(("instructions", (project_dir if project else base) / "AGENTS.md", instructions))
        return files

    if platform_name == "kilo":
        base = project_dir / ".kilocode" if project else home / ".kilocode"
        return [
            *_skill_files(base=base, platform_name=platform_name, project=project, command_name=command_name, command_path=command_path, fallback_command=fallback_command),
            ("rule", base / "rules" / "reql.md", _markdown_rule("Kilo Code", command_name=command_name, command_path=command_path, fallback_command=fallback_command)),
            ("instructions", (project_dir if project else base) / "AGENTS.md", instructions),
        ]

    if platform_name == "openclaw":
        base = project_dir / ".openclaw" if project else home / ".openclaw"
        files = _skill_files(base=base, platform_name=platform_name, project=project, command_name=command_name, command_path=command_path, fallback_command=fallback_command)
        files.append(("instructions", (project_dir if project else base) / "AGENTS.md", instructions))
        return files

    if platform_name == "hermes":
        base = project_dir / ".hermes" if project else home / ".hermes"
        files = _skill_files(base=base, platform_name=platform_name, project=project, command_name=command_name, command_path=command_path, fallback_command=fallback_command)
        files.append(("instructions", (project_dir if project else base) / "AGENTS.md", instructions))
        return files

    if platform_name == "kimi":
        base = project_dir / ".kimi" if project else home / ".kimi"
        files = [
            *_skill_files(base=base, platform_name=platform_name, project=project, command_name=command_name, command_path=command_path, fallback_command=fallback_command),
            ("rule", base / "rules" / "reql.md", _markdown_rule("Kimi Code", command_name=command_name, command_path=command_path, fallback_command=fallback_command)),
        ]
        files.append(("instructions", (project_dir if project else base) / "AGENTS.md", instructions))
        return files

    if platform_name == "antigravity":
        base = project_dir / ".antigravity" if project else home / ".antigravity"
        files = [
            *_skill_files(base=base, platform_name=platform_name, project=project, command_name=command_name, command_path=command_path, fallback_command=fallback_command),
            ("rule", base / "rules" / "reql.md", _markdown_rule("Google Antigravity", command_name=command_name, command_path=command_path, fallback_command=fallback_command)),
        ]
        files.append(("instructions", (project_dir if project else base) / "AGENTS.md", instructions))
        return files

    if platform_name == "agents":
        base = project_dir / ".agents" if project else home / ".agents"
        return [
            *_skill_files(base=base, platform_name=platform_name, project=project, command_name=command_name, command_path=command_path, fallback_command=fallback_command),
            ("instructions", (project_dir if project else base) / "AGENTS.md", instructions),
        ]

    raise ValueError(f"unknown platform '{platform_name}'")


def _version_stamp_paths(platform_name: str, *, project: bool, project_dir: Path, home_dir: Path | None = None) -> tuple[Path, ...]:
    planned = _planned_files(platform_name, project=project, project_dir=project_dir, home_dir=home_dir)
    paths = tuple(path.parent / VERSION_FILE for kind, path, _content in planned if kind == "skill")
    if paths:
        return paths
    for kind, path, _content in planned:
        return (path.parent / VERSION_FILE,)
    return ()


def _version_payload(platform_name: str, scope: str, *, command_path: Path | None = None) -> str:
    payload = {
        "installer": "reql-install",
        "installer_version": INSTALLER_VERSION,
        "package_version": _package_version(),
        "platform": platform_name,
        "scope": scope,
    }
    if command_path is not None:
        payload["command"] = "reql"
        payload["command_path"] = str(command_path)
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _package_version() -> str:
    try:
        return version("reql")
    except PackageNotFoundError:
        try:
            from memory import __version__

            return __version__
        except Exception:
            return "unknown"


def _env_path(name: str, fallback: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else fallback


@dataclass(frozen=True)
class CommandPlan:
    command_name: str
    primary_path: Path
    fallback_command: str
    files: tuple[tuple[Path, str], ...]


def _command_plan(command_dir: Path | None) -> CommandPlan:
    directory = _command_dir(command_dir)
    executable = "reql.cmd" if host_platform.system() == "Windows" else "reql"
    path = directory / executable
    return CommandPlan(
        command_name="reql",
        primary_path=path,
        fallback_command=_launcher_fallback_command(),
        files=((path, _command_script(path)),),
    )


def _command_dir(command_dir: Path | None) -> Path:
    if command_dir is not None:
        return command_dir.expanduser().resolve()
    env_dir = os.environ.get(COMMAND_ENV)
    if env_dir:
        return Path(env_dir).expanduser().resolve()

    existing = _find_owned_command_on_path()
    if existing is not None:
        return existing.parent

    path_dir = _first_writable_user_command_dir()
    if path_dir is not None:
        return path_dir

    home = Path.home()
    if host_platform.system() == "Windows":
        return home / ".reql" / "bin"
    return home / ".local" / "bin"


def _find_owned_command_on_path() -> Path | None:
    for directory in _path_entries():
        for name in _command_file_names():
            candidate = directory / name
            if candidate.exists() and _is_owned_command_file(candidate):
                return candidate
    return None


def _first_writable_user_command_dir() -> Path | None:
    home = Path.home().resolve(strict=False)
    for directory in _path_entries():
        try:
            resolved = directory.resolve(strict=False)
        except OSError:
            continue
        if not _is_relative_to(resolved, home):
            continue
        if _is_windows_apps_dir(resolved):
            continue
        if directory.exists() and os.access(directory, os.W_OK):
            target = directory / ("reql.cmd" if host_platform.system() == "Windows" else "reql")
            if target.exists() and not _is_owned_command_file(target):
                continue
            return directory
    return None


def _path_entries() -> list[Path]:
    entries: list[Path] = []
    for raw in os.environ.get("PATH", "").split(os.pathsep):
        if raw:
            path = Path(raw).expanduser()
            if path not in entries:
                entries.append(path)
    return entries


def _command_file_names() -> tuple[str, ...]:
    if host_platform.system() == "Windows":
        return ("reql.cmd", "reql.bat", "reql.exe", "reql")
    return ("reql",)


def _is_windows_apps_dir(path: Path) -> bool:
    parts = {part.casefold() for part in path.parts}
    return host_platform.system() == "Windows" and "windowsapps" in parts


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _launcher_fallback_command() -> str:
    args = _launcher_args()
    if host_platform.system() == "Windows":
        return subprocess.list2cmdline(args)
    return " ".join(shlex.quote(arg) for arg in args)


def _launcher_args() -> list[str]:
    python = sys.executable or "python"
    source_cli = Path(__file__).resolve().parents[2] / "cli.py"
    if source_cli.exists():
        return [python, str(source_cli)]
    return [python, "-m", "memory.cli"]


def _command_script(path: Path) -> str:
    args = _launcher_args()
    if path.suffix.casefold() in {".cmd", ".bat"}:
        command = subprocess.list2cmdline(args)
        return (
            "@echo off\n"
            f"REM {COMMAND_MARKER}\n"
            "REM Managed by reql install. Do not edit by hand.\n"
            f"{command} %*\n"
            "exit /b %ERRORLEVEL%\n"
        )
    command = " ".join(shlex.quote(arg) for arg in args)
    return (
        "#!/bin/sh\n"
        f"# {COMMAND_MARKER}\n"
        "# Managed by reql install. Do not edit by hand.\n"
        f"exec {command} \"$@\"\n"
    )


def _write_command_file(path: Path, content: str, *, dry_run: bool) -> str:
    if path.exists() and not _is_owned_command_file(path):
        return "not-owned"
    status = _write_file(path, content, sectioned=False, dry_run=dry_run)
    if not dry_run and status in {"created", "updated"} and path.suffix.casefold() not in {".cmd", ".bat"}:
        _make_executable(path)
    return status


def _remove_command_file(path: Path, *, dry_run: bool, stop: Path | None = None) -> str:
    if not path.exists():
        return "missing"
    if not _is_owned_command_file(path):
        return "not-owned"
    return _remove_owned_file(path, dry_run=dry_run, stop=stop)


def _is_owned_command_file(path: Path) -> bool:
    try:
        return COMMAND_MARKER in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def _make_executable(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _gemini_user_dir(home: Path) -> Path:
    if host_platform.system() == "Windows":
        return home / ".agents"
    return home / ".gemini"


def _write_file(path: Path, content: str, *, sectioned: bool, dry_run: bool) -> str:
    if sectioned and path.exists():
        existing = path.read_text(encoding="utf-8")
        desired = _replace_or_append_section(existing, content)
    else:
        desired = content

    if path.exists():
        current = path.read_text(encoding="utf-8")
        if current == desired:
            return "unchanged"
        status = "would-update" if dry_run else "updated"
    else:
        status = "would-create" if dry_run else "created"

    if dry_run:
        return status

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(desired, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return status


def _remove_owned_file(path: Path, *, dry_run: bool, stop: Path | None = None) -> str:
    if not path.exists():
        return "missing"
    if dry_run:
        return "would-remove"
    path.unlink()
    _cleanup_empty_dirs(path.parent, stop=stop)
    return "removed"


def _remove_section_file(path: Path, *, dry_run: bool, stop: Path | None = None) -> str:
    if not path.exists():
        return "missing"
    existing = path.read_text(encoding="utf-8")
    cleaned = _remove_section(existing)
    if cleaned == existing:
        return "unchanged"
    if dry_run:
        return "would-update" if cleaned.strip() else "would-remove"
    if cleaned.strip():
        path.write_text(cleaned, encoding="utf-8")
        return "updated"
    path.unlink()
    _cleanup_empty_dirs(path.parent, stop=stop)
    return "removed"


def _replace_or_append_section(existing: str, section: str) -> str:
    start = existing.find(SECTION_START)
    end = existing.find(SECTION_END)
    if start != -1 and end != -1 and end > start:
        end += len(SECTION_END)
        parts = [part for part in (existing[:start].rstrip(), section.strip(), existing[end:].strip()) if part]
        return "\n\n".join(parts) + "\n"
    if existing.strip():
        return existing.rstrip() + "\n\n" + section.strip() + "\n"
    return section.strip() + "\n"


def _remove_section(existing: str) -> str:
    start = existing.find(SECTION_START)
    end = existing.find(SECTION_END)
    if start == -1 or end == -1 or end <= start:
        return existing
    end += len(SECTION_END)
    parts = [part for part in (existing[:start].rstrip(), existing[end:].strip()) if part]
    return ("\n\n".join(parts) + "\n") if parts else ""


def _install_hook(platform_name: str, *, project: bool, project_dir: Path, dry_run: bool, home_dir: Path | None = None) -> tuple[Path, str] | None:
    plan = _hook_plan(platform_name, project=project, project_dir=project_dir, home_dir=home_dir)
    if plan is None:
        return None
    path, event, hook = plan
    settings = _read_json_object(path)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        settings["hooks"] = hooks
    existing = hooks.get(event, [])
    if not isinstance(existing, list):
        existing = []
    filtered = [item for item in existing if not _is_reql_hook(item)]
    desired = [*filtered, hook]
    hooks[event] = desired
    if path.exists() and _read_json_object(path) == settings:
        return path, "unchanged"
    existed = path.exists()
    if dry_run:
        return path, "would-update" if existed else "would-create"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path, "updated" if existed else "created"


def _uninstall_hook(platform_name: str, *, project: bool, project_dir: Path, dry_run: bool, home_dir: Path | None = None) -> tuple[Path, str] | None:
    plan = _hook_plan(platform_name, project=project, project_dir=project_dir, home_dir=home_dir)
    if plan is None:
        return None
    path, event, _hook = plan
    if not path.exists():
        return path, "missing"
    settings = _read_json_object(path)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return path, "unchanged"
    existing = hooks.get(event, [])
    if not isinstance(existing, list):
        return path, "unchanged"
    filtered = [item for item in existing if not _is_reql_hook(item)]
    if len(filtered) == len(existing):
        return path, "unchanged"
    if filtered:
        hooks[event] = filtered
    else:
        hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)
    if dry_run:
        return path, "would-update" if settings else "would-remove"
    if settings:
        path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path, "updated"
    path.unlink()
    _cleanup_empty_dirs(path.parent, stop=project_dir if project else (home_dir or Path.home()).expanduser())
    return path, "removed"


def _hook_plan(platform_name: str, *, project: bool, project_dir: Path, home_dir: Path | None = None) -> tuple[Path, str, dict[str, object]] | None:
    home = (home_dir or Path.home()).expanduser()
    if platform_name == "claude":
        base = project_dir / ".claude" if project else _env_path("CLAUDE_CONFIG_DIR", home / ".claude")
        return base / "settings.json", "PreToolUse", _claude_hook()
    if platform_name == "gemini":
        base = project_dir / ".gemini" if project else _gemini_user_dir(home)
        return base / "settings.json", "BeforeTool", _gemini_hook()
    return None


def _claude_hook() -> dict[str, object]:
    return {
        "matcher": "Read|Grep|Glob|Bash",
        "hooks": [
            {
                "type": "command",
                "command": _python_hook_command("claude"),
            }
        ],
    }


def _gemini_hook() -> dict[str, object]:
    return {
        "matcher": "read_file|list_directory|run_shell_command",
        "hooks": [
            {
                "type": "command",
                "command": _python_hook_command("gemini"),
            }
        ],
    }


def _python_hook_command(platform_name: str) -> str:
    message = (
        f"{HOOK_ID}: REQL graph context may be available. "
        "For repository context, run `reql project status .`, then build a query from the user request's own feature, "
        "behavior, file, command, error, field, endpoint, API, or symbol terms; preserve the user's language, "
        "identifiers, and exact errors. Use commands such as "
        "`reql query_memories --query \"<terms from user request>\"`, "
        "`reql query_context --query \"<terms from user request>\"`, or "
        "`reql query_explore --query \"<terms from user request>\"` for repository context; do not duplicate that context with broad "
        "`rg`, recursive directory listings, or custom scanners. If status reports "
        "`Project not found`, immediately run `reql project compile .` before broad raw file exploration. "
        "For automatic memory updates during active work, run one `reql project compile . --watch` monitor from the workspace after approval. "
        "If no watch process is running, run `reql project compile .` once after modifying project files before finishing."
    )
    if platform_name == "gemini":
        payload = {"decision": "allow", "additionalContext": message}
    else:
        payload = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": message}}
    encoded = json.dumps(payload, separators=(",", ":"))
    return (
        "python -c \"import json,pathlib,sys;"
        "e=pathlib.Path('.reql/memory.reql').exists() or pathlib.Path('conf.yaml').exists();"
        f"sys.stdout.write({encoded!r} if e else '')\""
    )


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _is_reql_hook(item: object) -> bool:
    try:
        return HOOK_ID in json.dumps(item, sort_keys=True)
    except TypeError:
        return HOOK_ID in str(item)


def _cleanup_empty_dirs(path: Path, *, stop: Path | None = None) -> None:
    stop_resolved = stop.resolve() if stop is not None else None
    current = path
    while True:
        try:
            if stop_resolved is not None and current.resolve() == stop_resolved:
                return
            current.rmdir()
        except OSError:
            return
        parent = current.parent
        if parent == current:
            return
        current = parent


def _skill_files(
    *,
    base: Path,
    platform_name: str,
    project: bool,
    command_name: str,
    command_path: Path,
    fallback_command: str,
) -> list[tuple[str, Path, str]]:
    files = [
        ("skill", base / "skills" / skill_dir / "SKILL.md", content)
        for skill_dir, content in _skill_markdowns(
            platform_name,
            project=project,
            command_name=command_name,
            command_path=command_path,
            fallback_command=fallback_command,
        )
    ]
    files.extend(
        ("skill-resource", base / "skills" / skill_dir / relative_path, content)
        for skill_dir, relative_path, content in _skill_resources(
            platform_name,
            project=project,
            command_name=command_name,
            command_path=command_path,
            fallback_command=fallback_command,
        )
    )
    return files


def _skill_markdowns(
    platform_name: str,
    *,
    project: bool,
    command_name: str,
    command_path: Path,
    fallback_command: str,
) -> tuple[tuple[str, str], ...]:
    return _skill_generator().skill_markdowns(
        platform_name=platform_name,
        project=project,
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
    )


def _skill_resources(
    platform_name: str,
    *,
    project: bool,
    command_name: str,
    command_path: Path,
    fallback_command: str,
) -> tuple[tuple[str, str, str], ...]:
    generator = _skill_generator()
    resources = getattr(generator, "skill_resources", None)
    if resources is None:
        return ()
    return resources(
        platform_name=platform_name,
        project=project,
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
    )


def _skill_generator() -> ModuleType:
    global _SKILL_GENERATOR
    if _SKILL_GENERATOR is not None:
        return _SKILL_GENERATOR

    resource = files("agents").joinpath("gen-skill.py")
    with as_file(resource) as path:
        spec = importlib.util.spec_from_file_location("agents.gen_skill_generated", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"could not load REQL skill generator from {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    _SKILL_GENERATOR = module
    return module


def _instruction_section(
    platform_name: str,
    *,
    project: bool,
    command_name: str,
    command_path: Path,
    fallback_command: str,
) -> str:
    supported = ", ".join(cfg["label"] for name, cfg in PLATFORMS_CONFIG.items() if cfg["is_all"])
    return _skill_generator().instruction_section(
        platform_name=platform_name,
        project=project,
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
        supported_clients=supported,
        section_start=SECTION_START,
        section_end=SECTION_END,
    )


def _cursor_rule(*, command_name: str, command_path: Path, fallback_command: str) -> str:
    return _skill_generator().cursor_rule(
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
        section_start=SECTION_START,
        section_end=SECTION_END,
    )


def _vscode_copilot_rule(*, command_name: str, command_path: Path, fallback_command: str) -> str:
    return _skill_generator().vscode_copilot_rule(
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
        section_start=SECTION_START,
        section_end=SECTION_END,
    )


def _markdown_rule(client_name: str, *, command_name: str, command_path: Path, fallback_command: str) -> str:
    return _skill_generator().markdown_rule(
        client_name,
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
        section_start=SECTION_START,
        section_end=SECTION_END,
    )


def available_platforms_text() -> str:
    return ", ".join(PLATFORMS_CONFIG) + ", all"
