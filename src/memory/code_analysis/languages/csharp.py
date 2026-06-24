"""C# Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IDENTIFIER_NODES, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class CSharpTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "csharp"
    tree_sitter_module = "tree_sitter_c_sharp"
    profile = AstProfile(
        name="csharp",
        languages=frozenset({"csharp"}),
        class_nodes=frozenset({"class_declaration", "enum_declaration", "interface_declaration", "struct_declaration"}),
        function_nodes=frozenset({"local_function_statement"}),
        method_nodes=frozenset({"constructor_declaration", "method_declaration"}),
        variable_nodes=frozenset({"declaration_expression", "local_declaration_statement", "variable_declarator"}),
        import_nodes=COMMON_IMPORTS | frozenset({"using_directive"}),
        call_nodes=frozenset({"invocation_expression"}),
        assignment_nodes=frozenset({"assignment_expression"}),
        identifier_nodes=COMMON_IDENTIFIER_NODES | frozenset({"qualified_name"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"Console", "nameof", "new", "this"}),
    )
