"""F# Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class FSharpTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "fsharp"
    profile = AstProfile(
        name="fsharp",
        languages=frozenset({"fsharp"}),
        class_nodes=frozenset({"module_declaration", "namespace_declaration", "type_definition"}),
        function_nodes=frozenset({"function_declaration", "let_binding"}),
        variable_nodes=frozenset({"let_binding"}),
        import_nodes=COMMON_IMPORTS | frozenset({"open_statement"}),
        call_nodes=frozenset({"call_expression"}),
        assignment_nodes=frozenset({"let_binding"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"printfn"}),
    )
