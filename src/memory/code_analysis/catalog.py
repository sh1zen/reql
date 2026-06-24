"""Language catalog and Tree-sitter grammar loading for code extraction."""
from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any, TypedDict


class CodeLanguageSpec(TypedDict, total=False):
    display: str
    aliases: tuple[str, ...]
    extensions: tuple[str, ...]
    basenames: tuple[str, ...]


CODE_LANGUAGE_CATALOG: dict[str, CodeLanguageSpec] = {
    "apex": {"display": "Apex", "aliases": ("apex",), "extensions": (".apex", ".cls", ".trigger")},
    "bash": {
        "display": "Bash",
        "aliases": ("bash", "bourne shell", "shell", "sh"),
        "extensions": (".bash", ".sh"),
        "basenames": ("bashrc",),
    },
    "c": {"display": "C", "aliases": ("c",), "extensions": (".c", ".h")},
    "cpp": {"display": "C++", "aliases": ("c++", "c/c++", "cpp"), "extensions": (".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".ipp")},
    "csharp": {"display": "C#", "aliases": ("c#", "cs", "csharp"), "extensions": (".cs", ".csx")},
    "dockerfile": {"display": "Dockerfile", "aliases": ("dockerfile",), "basenames": ("dockerfile",)},
    "elixir": {"display": "Elixir", "aliases": ("elixir",), "extensions": (".ex", ".exs")},
    "fortran": {"display": "Fortran", "aliases": ("fortran",), "extensions": (".f", ".f03", ".f08", ".f77", ".f90", ".f95", ".for", ".ftn")},
    "fsharp": {"display": "F#", "aliases": ("f#", "fsharp")},
    "go": {"display": "Go", "aliases": ("go", "golang"), "extensions": (".go",)},
    "java": {"display": "Java", "aliases": ("java",), "extensions": (".java",)},
    "javascript": {"display": "JavaScript", "aliases": ("javascript", "js", "jsx"), "extensions": (".cjs", ".js", ".jsx", ".mjs")},
    "julia": {"display": "Julia", "aliases": ("julia",), "extensions": (".jl",)},
    "just": {"display": "Just", "aliases": ("just",), "basenames": ("justfile",)},
    "kotlin": {"display": "Kotlin", "aliases": ("kotlin",), "extensions": (".kt", ".kts")},
    "lua": {"display": "Lua", "aliases": ("lua",), "extensions": (".lua",)},
    "makefile": {"display": "Makefile", "aliases": ("makefile",), "basenames": ("makefile",)},
    "pascal": {"display": "Pascal", "aliases": ("pascal",), "extensions": (".pas", ".pp")},
    "php": {"display": "PHP", "aliases": ("php",), "extensions": (".php", ".php3", ".php4", ".php5", ".phtml")},
    "powershell": {"display": "PowerShell", "aliases": ("powershell", "pwsh"), "extensions": (".ps1", ".psd1", ".psm1")},
    "python": {"display": "Python", "aliases": ("python", "py"), "extensions": (".py", ".pyi", ".pyw")},
    "razor": {"display": "Razor", "aliases": ("razor",), "extensions": (".cshtml", ".razor")},
    "ruby": {"display": "Ruby", "aliases": ("ruby",), "extensions": (".rake", ".rb"), "basenames": ("gemfile", "rakefile")},
    "rust": {"display": "Rust", "aliases": ("rust",), "extensions": (".rs",)},
    "scala": {"display": "Scala", "aliases": ("scala",), "extensions": (".scala", ".sc")},
    "solidity": {"display": "Solidity", "aliases": ("solidity", "sol"), "extensions": (".sol",)},
    "sql": {"display": "SQL", "aliases": ("sql",), "extensions": (".sql",)},
    "swift": {"display": "Swift", "aliases": ("swift",), "extensions": (".swift",)},
    "terraform": {"display": "Terraform", "aliases": ("terraform", "tf"), "extensions": (".tf", ".tfvars")},
    "tsx": {"display": "TSX", "aliases": ("tsx",)},
    "typescript": {"display": "TypeScript", "aliases": ("typescript", "ts"), "extensions": (".cts", ".mts", ".ts", ".tsx")},
    "verilog": {"display": "Verilog", "aliases": ("verilog",), "extensions": (".sv", ".svh", ".v", ".vh")},
    "zig": {"display": "Zig", "aliases": ("zig",), "extensions": (".zig",), "basenames": ("build.zig",)},
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


def normalize_language(language: str | None) -> str | None:
    if not language:
        return None
    value = language.casefold().strip().lstrip(".")
    for key, spec in CODE_LANGUAGE_CATALOG.items():
        if value == key:
            return key
        if value == spec["display"].casefold():
            return key
        if value in {alias.casefold() for alias in spec.get("aliases", ())}:
            return key
    return None


def display_language_for_path(path: str | Path) -> str | None:
    candidate = Path(path)
    suffix = candidate.suffix.casefold()
    basename = candidate.name.casefold()
    for spec in CODE_LANGUAGE_CATALOG.values():
        if suffix in spec.get("extensions", ()):
            return spec["display"]
        if basename in spec.get("basenames", ()):
            return spec["display"]
    return None


def detect_code_language(artifact: Any) -> str | None:
    detected = normalize_language(getattr(artifact, "language", None))
    if detected:
        return detected
    path = getattr(artifact, "path", "")
    return normalize_language(display_language_for_path(Path(path)))


def language_key(language: str | None) -> str:
    return normalize_language(language) or (language or "").casefold().strip().lstrip(".")


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
    from .factory import EXTRACTOR_BY_LANGUAGE

    extractor_cls = EXTRACTOR_BY_LANGUAGE.get(language)
    module_name = getattr(extractor_cls, "tree_sitter_module", None)
    if not module_name:
        return None
    function_name = getattr(extractor_cls, "tree_sitter_function", "language")
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
    for key in _language_pack_keys(language):
        try:
            return get_language(key)
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise ValueError(f"Tree-sitter language pack does not provide {language!r}") from last_error
    return None


def _language_pack_keys(language: str) -> tuple[str, ...]:
    aliases = LANGUAGE_PACK_ALIASES.get(language, ())
    display = CODE_LANGUAGE_CATALOG.get(language, {}).get("display", "")
    normalized_display = display.casefold().replace("#", "sharp").replace("+", "p").replace(" ", "_")
    candidates = [language, *aliases, normalized_display]
    return tuple(item for index, item in enumerate(candidates) if item and item not in candidates[:index])
