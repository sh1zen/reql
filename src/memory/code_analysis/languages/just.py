"""Just Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class JustTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "just"
    profile = AstProfile(
        name="just",
        languages=frozenset({"just"}),
        function_nodes=frozenset({"recipe"}),
        variable_nodes=frozenset({"assignment"}),
        import_nodes=COMMON_IMPORTS | frozenset({"import_statement"}),
        call_nodes=frozenset({"command"}),
        assignment_nodes=frozenset({"assignment"}),
        import_call_names=frozenset({"import"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"echo"}),
    )
