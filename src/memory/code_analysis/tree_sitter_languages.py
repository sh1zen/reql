"""Tree-sitter grammar loading and language-key helpers."""
from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

from .languages import CODE_LANGUAGE_CATALOG, normalize_language


DIRECT_LANGUAGE_LOADERS: dict[str, tuple[str, str]] = {
    "bash": ("tree_sitter_bash", "language"),
    "c": ("tree_sitter_c", "language"),
    "cpp": ("tree_sitter_cpp", "language"),
    "csharp": ("tree_sitter_c_sharp", "language"),
    "elixir": ("tree_sitter_elixir", "language"),
    "fortran": ("tree_sitter_fortran", "language"),
    "go": ("tree_sitter_go", "language"),
    "java": ("tree_sitter_java", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "julia": ("tree_sitter_julia", "language"),
    "kotlin": ("tree_sitter_kotlin", "language"),
    "lua": ("tree_sitter_lua", "language"),
    "makefile": ("tree_sitter_make", "language"),
    "php": ("tree_sitter_php", "language_php"),
    "powershell": ("tree_sitter_powershell", "language"),
    "python": ("tree_sitter_python", "language"),
    "ruby": ("tree_sitter_ruby", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "scala": ("tree_sitter_scala", "language"),
    "sql": ("tree_sitter_sql", "language"),
    "swift": ("tree_sitter_swift", "language"),
    "terraform": ("tree_sitter_hcl", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "verilog": ("tree_sitter_verilog", "language"),
    "zig": ("tree_sitter_zig", "language"),
}

LANGUAGE_PACK_ALIASES: dict[str, tuple[str, ...]] = {
    "csharp": ("c_sharp", "c-sharp", "csharp"),
    "cpp": ("cpp", "c++"),
    "dockerfile": ("dockerfile", "docker"),
    "fsharp": ("f_sharp", "f-sharp", "fsharp"),
    "makefile": ("make", "makefile"),
    "powershell": ("powershell", "power_shell"),
    "tsx": ("tsx", "typescript"),
    "typescript": ("typescript", "ts"),
}


def language_key(language: str | None) -> str:
    return normalize_language(language) or (language or "").casefold().strip().lstrip(".")


def display_language_for_key(key: str | None, fallback: str | None = None) -> str:
    if key:
        return CODE_LANGUAGE_CATALOG.get(key, {}).get("display", key)
    return fallback or "unknown"


def tree_sitter_language_key(path: str, language: str | None) -> str:
    if Path(path).suffix.casefold() == ".tsx":
        return "tsx"
    key = language_key(language)
    return "typescript" if key == "tsx" else key


def load_tree_sitter_language(tree_sitter: Any, language: str) -> Any:
    language_cls = getattr(tree_sitter, "Language")
    raw = _load_direct_language(language)
    if raw is None:
        raw = _load_language_pack_language(language)
    if raw is None:
        raise ValueError(f"Unsupported Tree-sitter language: {language}")
    try:
        return language_cls(raw)
    except TypeError:
        return raw


def _load_direct_language(language: str) -> Any | None:
    loader = DIRECT_LANGUAGE_LOADERS.get(language)
    if loader is None:
        return None
    module_name, function_name = loader
    try:
        module = import_module(module_name)
    except ModuleNotFoundError:
        return None
    return getattr(module, function_name)()


def _load_language_pack_language(language: str) -> Any | None:
    try:
        language_pack = import_module("tree_sitter_language_pack")
    except ModuleNotFoundError:
        return None
    get_language = getattr(language_pack, "get_language")
    last_error: Exception | None = None
    for key in language_pack_keys(language):
        try:
            return get_language(key)
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise ValueError(f"Tree-sitter language pack does not provide {language!r}") from last_error
    return None


def language_pack_keys(language: str) -> tuple[str, ...]:
    aliases = LANGUAGE_PACK_ALIASES.get(language, ())
    display = CODE_LANGUAGE_CATALOG.get(language, {}).get("display", "")
    normalized_display = display.casefold().replace("#", "sharp").replace("+", "p").replace(" ", "_")
    candidates = [language, *aliases, normalized_display]
    return tuple(item for index, item in enumerate(candidates) if item and item not in candidates[:index])
