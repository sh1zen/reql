"""Pascal Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class PascalTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "pascal"
    profile = AstProfile(
        name="pascal",
        languages=frozenset({"pascal"}),
        class_nodes=frozenset({"class_declaration", "program", "record_type", "type_declaration"}),
        function_nodes=frozenset({"function_declaration", "procedure_declaration"}),
        variable_nodes=frozenset({"variable_declaration"}),
        import_nodes=COMMON_IMPORTS | frozenset({"uses_clause"}),
        call_nodes=frozenset({"call_expression", "procedure_call"}),
        assignment_nodes=frozenset({"assignment_statement"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"Write", "WriteLn"}),
    )
