"""Bash Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class BashTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "bash"
    tree_sitter_module = "tree_sitter_bash"
    profile = AstProfile(
        name="bash",
        languages=frozenset({"bash"}),
        function_nodes=frozenset({"function_definition"}),
        variable_nodes=frozenset({"variable_assignment"}),
        import_nodes=COMMON_IMPORTS | frozenset({"dot_sourcing_statement"}),
        call_nodes=frozenset({"command"}),
        assignment_nodes=frozenset({"variable_assignment"}),
        import_call_names=frozenset({"source", "."}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"cd", "echo", "exit", "printf", "test"}),
    )
