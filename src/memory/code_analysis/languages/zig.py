"""Zig Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class ZigTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "zig"
    tree_sitter_module = "tree_sitter_zig"
    profile = AstProfile(
        name="zig",
        languages=frozenset({"zig"}),
        class_nodes=frozenset({"enum_declaration", "struct_declaration", "union_declaration"}),
        function_nodes=frozenset({"function_declaration"}),
        variable_nodes=frozenset({"variable_declaration"}),
        import_nodes=COMMON_IMPORTS,
        call_nodes=frozenset({"builtin_function", "call_expression"}),
        assignment_nodes=frozenset({"assignment_expression", "variable_declaration"}),
        import_call_names=frozenset({"@import"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"@compileError", "@panic", "std"}),
    )
