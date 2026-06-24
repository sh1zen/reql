"""Rust Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class RustTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "rust"
    tree_sitter_module = "tree_sitter_rust"
    profile = AstProfile(
        name="rust",
        languages=frozenset({"rust"}),
        class_nodes=frozenset({"enum_item", "impl_item", "mod_item", "struct_item", "trait_item", "type_item", "union_item"}),
        function_nodes=frozenset({"function_item"}),
        variable_nodes=frozenset({"let_declaration"}),
        import_nodes=COMMON_IMPORTS | frozenset({"extern_crate_declaration", "use_declaration"}),
        assignment_nodes=frozenset({"assignment_expression", "let_declaration"}),
        raise_nodes=frozenset({"panic_expression"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"assert", "assert_eq", "dbg", "drop", "format", "panic", "println", "todo", "unimplemented", "vec"}),
    )
