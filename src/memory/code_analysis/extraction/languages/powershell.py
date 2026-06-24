"""PowerShell Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class PowerShellTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "powershell"
    tree_sitter_module = "tree_sitter_powershell"
    profile = AstProfile(
        name="powershell",
        languages=frozenset({"powershell"}),
        function_nodes=frozenset({"function_statement"}),
        variable_nodes=frozenset({"assignment_statement", "variable"}),
        import_nodes=COMMON_IMPORTS,
        call_nodes=frozenset({"command", "pipeline"}),
        assignment_nodes=frozenset({"assignment_statement"}),
        import_call_names=frozenset({"Import-Module", "."}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"Get-ChildItem", "Set-Location", "Write-Host", "Write-Output"}),
    )
