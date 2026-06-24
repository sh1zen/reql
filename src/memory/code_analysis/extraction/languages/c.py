"""C Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IDENTIFIER_NODES, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class CTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "c"
    tree_sitter_module = "tree_sitter_c"
    profile = AstProfile(
        name="c",
        languages=frozenset({"c"}),
        class_nodes=frozenset({"enum_specifier", "struct_specifier", "union_specifier"}),
        function_nodes=frozenset({"function_declaration", "function_definition"}),
        variable_nodes=frozenset({"declaration", "field_declaration", "variable_declarator"}),
        import_nodes=COMMON_IMPORTS,
        call_nodes=frozenset({"call_expression"}),
        assignment_nodes=frozenset({"assignment_expression", "update_expression"}),
        identifier_nodes=COMMON_IDENTIFIER_NODES | frozenset({"qualified_identifier"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"assert", "printf", "sizeof"}),
    )
