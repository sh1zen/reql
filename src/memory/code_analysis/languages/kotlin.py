"""Kotlin Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IDENTIFIER_NODES, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class KotlinTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "kotlin"
    tree_sitter_module = "tree_sitter_kotlin"
    profile = AstProfile(
        name="kotlin",
        languages=frozenset({"kotlin"}),
        class_nodes=frozenset({"class_declaration", "companion_object", "enum_class_body", "object_declaration", "type_alias"}),
        function_nodes=frozenset({"function_declaration"}),
        variable_nodes=frozenset({"property_declaration", "variable_declaration"}),
        import_nodes=COMMON_IMPORTS | frozenset({"import", "package_header"}),
        call_nodes=frozenset({"call_expression", "navigation_expression"}),
        assignment_nodes=frozenset({"assignment", "property_declaration"}),
        identifier_nodes=COMMON_IDENTIFIER_NODES | frozenset({"qualified_identifier"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"println", "require", "super", "this"}),
    )
