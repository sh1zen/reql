"""Swift Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IDENTIFIER_NODES, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class SwiftTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "swift"
    tree_sitter_module = "tree_sitter_swift"
    profile = AstProfile(
        name="swift",
        languages=frozenset({"swift"}),
        class_nodes=frozenset({"class_declaration", "enum_declaration", "extension_declaration", "protocol_declaration", "struct_declaration"}),
        function_nodes=frozenset({"function_declaration"}),
        method_nodes=frozenset({"function_declaration"}),
        variable_nodes=frozenset({"property_declaration", "value_binding_pattern"}),
        import_nodes=COMMON_IMPORTS | frozenset({"import_declaration"}),
        call_nodes=frozenset({"call_expression"}),
        assignment_nodes=frozenset({"assignment"}),
        identifier_nodes=COMMON_IDENTIFIER_NODES | frozenset({"user_type"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"print", "self", "super"}),
    )
