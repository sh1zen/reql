"""Elixir Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class ElixirTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "elixir"
    tree_sitter_module = "tree_sitter_elixir"
    profile = AstProfile(
        name="elixir",
        languages=frozenset({"elixir"}),
        class_nodes=frozenset({"module"}),
        function_nodes=frozenset({"function"}),
        import_nodes=COMMON_IMPORTS,
        call_nodes=frozenset({"call"}),
        assignment_nodes=frozenset({"match_operator"}),
        class_call_names=frozenset({"defmodule", "defprotocol"}),
        function_call_names=frozenset({"def", "defmacro"}),
        private_function_call_names=frozenset({"defmacrop", "defp"}),
        import_call_names=frozenset({"alias", "import", "require", "use"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"case", "cond", "fn", "receive", "raise", "send", "spawn"}),
    )
