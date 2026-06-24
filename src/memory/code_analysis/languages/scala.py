"""Scala Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class ScalaTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "scala"
    tree_sitter_module = "tree_sitter_scala"
    profile = AstProfile(
        name="scala",
        languages=frozenset({"scala"}),
        class_nodes=frozenset({"class_definition", "enum_definition", "object_definition", "trait_definition", "type_definition"}),
        function_nodes=frozenset({"function_definition"}),
        variable_nodes=frozenset({"val_definition", "var_definition"}),
        import_nodes=COMMON_IMPORTS | frozenset({"import_declaration"}),
        assignment_nodes=frozenset({"assignment_expression", "val_definition", "var_definition"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"assert", "println", "require"}),
    )
