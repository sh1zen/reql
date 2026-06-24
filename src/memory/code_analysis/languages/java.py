"""Java Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IDENTIFIER_NODES, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class JavaTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "java"
    tree_sitter_module = "tree_sitter_java"
    profile = AstProfile(
        name="java",
        languages=frozenset({"java"}),
        class_nodes=frozenset({"class_declaration", "enum_declaration", "interface_declaration", "record_declaration"}),
        method_nodes=frozenset({"constructor_declaration", "method_declaration"}),
        variable_nodes=frozenset({"field_declaration", "local_variable_declaration", "variable_declarator"}),
        import_nodes=COMMON_IMPORTS | frozenset({"package_declaration"}),
        call_nodes=frozenset({"method_invocation", "object_creation_expression"}),
        assignment_nodes=frozenset({"assignment_expression", "update_expression"}),
        identifier_nodes=COMMON_IDENTIFIER_NODES | frozenset({"scoped_identifier"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"System", "new", "super", "this"}),
    )
