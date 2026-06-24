"""Lua Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class LuaTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "lua"
    tree_sitter_module = "tree_sitter_lua"
    profile = AstProfile(
        name="lua",
        languages=frozenset({"lua"}),
        function_nodes=frozenset({"function_declaration"}),
        variable_nodes=frozenset({"assignment_statement", "variable_declaration"}),
        variable_function_nodes=frozenset({"assignment_statement", "variable_declaration"}),
        import_nodes=COMMON_IMPORTS,
        call_nodes=frozenset({"function_call"}),
        assignment_nodes=frozenset({"assignment_statement", "variable_declaration"}),
        import_call_names=frozenset({"require"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"assert", "error", "ipairs", "pairs", "print", "require", "tonumber", "tostring", "type"}),
    )
