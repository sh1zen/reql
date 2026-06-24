"""Fortran Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class FortranTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "fortran"
    tree_sitter_module = "tree_sitter_fortran"
    profile = AstProfile(
        name="fortran",
        languages=frozenset({"fortran"}),
        class_nodes=frozenset({"module", "program", "type_definition"}),
        function_nodes=frozenset({"function", "function_subprogram", "subroutine", "subroutine_subprogram"}),
        variable_nodes=frozenset({"declaration", "variable_declaration"}),
        import_nodes=COMMON_IMPORTS | frozenset({"use_statement"}),
        call_nodes=frozenset({"call_expression", "subroutine_call"}),
        assignment_nodes=frozenset({"assignment_statement"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"allocated", "print", "present", "write"}),
    )
