"""Programming language catalog used by scanners and code parsers."""
from __future__ import annotations

from pathlib import Path
from typing import TypedDict


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
    "sql": {"display": "SQL", "aliases": ("sql",), "extensions": (".sql",)},
    "swift": {"display": "Swift", "aliases": ("swift",), "extensions": (".swift",)},
    "terraform": {"display": "Terraform", "aliases": ("terraform", "tf"), "extensions": (".tf", ".tfvars")},
    "tsx": {"display": "TSX", "aliases": ("tsx",)},
    "typescript": {"display": "TypeScript", "aliases": ("typescript", "ts"), "extensions": (".cts", ".mts", ".ts", ".tsx")},
    "verilog": {"display": "Verilog", "aliases": ("verilog",), "extensions": (".sv", ".svh", ".v", ".vh")},
    "zig": {"display": "Zig", "aliases": ("zig",), "extensions": (".zig",), "basenames": ("build.zig",)},
}

SUPPORTED_AST_LANGUAGES = frozenset(CODE_LANGUAGE_CATALOG)


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
