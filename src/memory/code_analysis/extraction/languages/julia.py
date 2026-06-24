"""Julia Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class JuliaTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "julia"
    tree_sitter_module = "tree_sitter_julia"
    profile = AstProfile(
        name="julia",
        languages=frozenset({"julia"}),
        class_nodes=frozenset({"module_definition", "struct_definition"}),
        function_nodes=frozenset({"function_definition"}),
        variable_nodes=frozenset({"assignment"}),
        variable_function_nodes=frozenset({"assignment"}),
        import_nodes=COMMON_IMPORTS | frozenset({"import_statement", "using_statement"}),
        call_nodes=frozenset({"call_expression"}),
        assignment_nodes=frozenset({"assignment"}),
        import_call_names=frozenset({"include"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"include", "println", "print"}),
    )
