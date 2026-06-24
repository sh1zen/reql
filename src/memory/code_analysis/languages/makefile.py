"""Makefile Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class MakefileTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "makefile"
    profile = AstProfile(
        name="makefile",
        languages=frozenset({"makefile"}),
        function_nodes=frozenset({"rule"}),
        variable_nodes=frozenset({"variable_assignment"}),
        import_nodes=COMMON_IMPORTS | frozenset({"include_directive"}),
        call_nodes=frozenset({"command"}),
        assignment_nodes=frozenset({"variable_assignment"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"echo"}),
    )
    tree_sitter_module = "tree_sitter_make"
