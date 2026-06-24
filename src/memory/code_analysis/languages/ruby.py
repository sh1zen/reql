"""Ruby Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class RubyTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "ruby"
    tree_sitter_module = "tree_sitter_ruby"
    profile = AstProfile(
        name="ruby",
        languages=frozenset({"ruby"}),
        class_nodes=frozenset({"class", "class_declaration", "module"}),
        function_nodes=frozenset({"method", "singleton_method"}),
        variable_nodes=frozenset({"assignment", "global_variable", "instance_variable"}),
        import_nodes=COMMON_IMPORTS,
        call_nodes=frozenset({"call"}),
        assignment_nodes=frozenset({"assignment", "operator_assignment"}),
        raise_nodes=frozenset({"raise"}),
        import_call_names=frozenset({"require", "require_relative", "load"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"puts", "print", "raise", "require"}),
    )
