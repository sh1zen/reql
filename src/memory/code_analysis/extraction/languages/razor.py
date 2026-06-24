"""Razor Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class RazorTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "razor"
    profile = AstProfile(
        name="razor",
        languages=frozenset({"razor"}),
        class_nodes=frozenset({"class_declaration", "component", "razor_directive"}),
        function_nodes=frozenset({"function_declaration"}),
        method_nodes=frozenset({"method_declaration"}),
        variable_nodes=frozenset({"code_block", "local_variable_declaration", "variable_declarator"}),
        import_nodes=COMMON_IMPORTS | frozenset({"using_directive"}),
        call_nodes=frozenset({"invocation_expression", "method_invocation"}),
        assignment_nodes=frozenset({"assignment_expression"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"RenderFragment", "this"}),
    )
