"""Terraform Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class TerraformTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "terraform"
    tree_sitter_module = "tree_sitter_hcl"
    profile = AstProfile(
        name="terraform",
        languages=frozenset({"terraform"}),
        class_nodes=frozenset({"block", "module", "resource"}),
        variable_nodes=frozenset({"attribute", "block"}),
        import_nodes=COMMON_IMPORTS,
        call_nodes=frozenset({"function_call"}),
        assignment_nodes=frozenset({"attribute"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"file", "join", "lookup", "templatefile"}),
    )
