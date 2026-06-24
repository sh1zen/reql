"""C++ Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IDENTIFIER_NODES, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class CppTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "cpp"
    tree_sitter_module = "tree_sitter_cpp"
    profile = AstProfile(
        name="cpp",
        languages=frozenset({"cpp"}),
        class_nodes=frozenset({"class_specifier", "enum_specifier", "struct_specifier", "union_specifier"}),
        function_nodes=frozenset({"function_declaration", "function_definition"}),
        method_nodes=frozenset({"constructor_declaration", "destructor_declaration", "method_declaration", "method_definition"}),
        variable_nodes=frozenset({"declaration", "field_declaration", "variable_declarator"}),
        import_nodes=COMMON_IMPORTS | frozenset({"using_declaration", "using_directive"}),
        call_nodes=frozenset({"call_expression"}),
        assignment_nodes=frozenset({"assignment_expression", "update_expression"}),
        identifier_nodes=COMMON_IDENTIFIER_NODES | frozenset({"qualified_identifier", "scoped_identifier"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"assert", "delete", "new", "sizeof", "std"}),
    )
