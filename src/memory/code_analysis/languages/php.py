"""PHP Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class PhpTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "php"
    tree_sitter_module = "tree_sitter_php"
    tree_sitter_function = "language_php"
    profile = AstProfile(
        name="php",
        languages=frozenset({"php"}),
        class_nodes=frozenset({"class_declaration", "enum_declaration", "interface_declaration", "trait_declaration"}),
        function_nodes=frozenset({"function_definition"}),
        method_nodes=frozenset({"method_declaration"}),
        variable_nodes=frozenset({"assignment_expression", "property_declaration"}),
        import_nodes=COMMON_IMPORTS | frozenset({"namespace_definition", "namespace_use_declaration"}),
        call_nodes=frozenset({"call_expression", "member_call_expression", "scoped_call_expression"}),
        assignment_nodes=frozenset({"assignment_expression"}),
        raise_nodes=frozenset({"throw_expression", "throw_statement"}),
        import_call_names=frozenset({"include", "include_once", "require", "require_once"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"array", "echo", "isset", "print", "require"}),
    )
