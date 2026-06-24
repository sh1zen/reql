"""Go Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class GoTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "go"
    tree_sitter_module = "tree_sitter_go"
    profile = AstProfile(
        name="go",
        languages=frozenset({"go"}),
        class_nodes=frozenset({"type_declaration"}),
        function_nodes=frozenset({"function_declaration"}),
        method_nodes=frozenset({"method_declaration"}),
        variable_nodes=frozenset({"const_declaration", "short_var_declaration", "var_declaration", "var_spec"}),
        import_nodes=COMMON_IMPORTS | frozenset({"import_declaration", "import_spec"}),
        assignment_nodes=frozenset({"assignment_statement", "short_var_declaration"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"append", "cap", "close", "copy", "delete", "len", "make", "new", "panic", "print", "println", "recover"}),
    )
