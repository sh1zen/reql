"""Apex Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class ApexTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "apex"
    profile = AstProfile(
        name="apex",
        languages=frozenset({"apex"}),
        class_nodes=frozenset({"class_declaration", "enum_declaration", "interface_declaration", "trigger_declaration"}),
        method_nodes=frozenset({"constructor_declaration", "method_declaration"}),
        variable_nodes=frozenset({"field_declaration", "local_variable_declaration", "variable_declarator"}),
        import_nodes=COMMON_IMPORTS,
        call_nodes=frozenset({"method_invocation"}),
        assignment_nodes=frozenset({"assignment_expression"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"System", "Test", "this"}),
    )
