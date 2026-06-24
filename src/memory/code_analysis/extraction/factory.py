"""Factory for language-specific Tree-sitter extractors."""
from __future__ import annotations

from ...artifacts.models import SourceArtifact
from .base import TreeSitterExtractorBase
from .languages.apex import ApexTreeSitterExtractor
from .languages.bash import BashTreeSitterExtractor
from .languages.c import CTreeSitterExtractor
from .languages.cpp import CppTreeSitterExtractor
from .languages.csharp import CSharpTreeSitterExtractor
from .languages.dockerfile import DockerfileTreeSitterExtractor
from .languages.elixir import ElixirTreeSitterExtractor
from .languages.fortran import FortranTreeSitterExtractor
from .languages.fsharp import FSharpTreeSitterExtractor
from .languages.go import GoTreeSitterExtractor
from .languages.java import JavaTreeSitterExtractor
from .languages.javascript import JavaScriptTreeSitterExtractor
from .languages.julia import JuliaTreeSitterExtractor
from .languages.just import JustTreeSitterExtractor
from .languages.kotlin import KotlinTreeSitterExtractor
from .languages.lua import LuaTreeSitterExtractor
from .languages.makefile import MakefileTreeSitterExtractor
from .languages.pascal import PascalTreeSitterExtractor
from .languages.php import PhpTreeSitterExtractor
from .languages.powershell import PowerShellTreeSitterExtractor
from .languages.python import PythonTreeSitterExtractor
from .languages.razor import RazorTreeSitterExtractor
from .languages.ruby import RubyTreeSitterExtractor
from .languages.rust import RustTreeSitterExtractor
from .languages.scala import ScalaTreeSitterExtractor
from .languages.solidity import SolidityTreeSitterExtractor
from .languages.sql import SqlTreeSitterExtractor
from .languages.swift import SwiftTreeSitterExtractor
from .languages.terraform import TerraformTreeSitterExtractor
from .languages.tsx import TsxTreeSitterExtractor
from .languages.typescript import TypeScriptTreeSitterExtractor
from .languages.verilog import VerilogTreeSitterExtractor
from .languages.zig import ZigTreeSitterExtractor


EXTRACTOR_BY_LANGUAGE: dict[str, type[TreeSitterExtractorBase]] = {
    "apex": ApexTreeSitterExtractor,
    "bash": BashTreeSitterExtractor,
    "c": CTreeSitterExtractor,
    "cpp": CppTreeSitterExtractor,
    "csharp": CSharpTreeSitterExtractor,
    "dockerfile": DockerfileTreeSitterExtractor,
    "elixir": ElixirTreeSitterExtractor,
    "fortran": FortranTreeSitterExtractor,
    "fsharp": FSharpTreeSitterExtractor,
    "go": GoTreeSitterExtractor,
    "java": JavaTreeSitterExtractor,
    "javascript": JavaScriptTreeSitterExtractor,
    "julia": JuliaTreeSitterExtractor,
    "just": JustTreeSitterExtractor,
    "kotlin": KotlinTreeSitterExtractor,
    "lua": LuaTreeSitterExtractor,
    "makefile": MakefileTreeSitterExtractor,
    "pascal": PascalTreeSitterExtractor,
    "php": PhpTreeSitterExtractor,
    "powershell": PowerShellTreeSitterExtractor,
    "python": PythonTreeSitterExtractor,
    "razor": RazorTreeSitterExtractor,
    "ruby": RubyTreeSitterExtractor,
    "rust": RustTreeSitterExtractor,
    "scala": ScalaTreeSitterExtractor,
    "solidity": SolidityTreeSitterExtractor,
    "sql": SqlTreeSitterExtractor,
    "swift": SwiftTreeSitterExtractor,
    "terraform": TerraformTreeSitterExtractor,
    "tsx": TsxTreeSitterExtractor,
    "typescript": TypeScriptTreeSitterExtractor,
    "verilog": VerilogTreeSitterExtractor,
    "zig": ZigTreeSitterExtractor,
}


def extractor_for(
    artifact: SourceArtifact,
    source: bytes,
    language: str,
    language_key: str,
) -> TreeSitterExtractorBase:
    try:
        extractor_cls = EXTRACTOR_BY_LANGUAGE[language_key]
    except KeyError as exc:
        raise ValueError(f"No Tree-sitter extractor registered for language {language_key!r}") from exc
    return extractor_cls(artifact, source, language, language_key)
