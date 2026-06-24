"""Verilog Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class VerilogTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "verilog"
    tree_sitter_module = "tree_sitter_verilog"
    profile = AstProfile(
        name="verilog",
        languages=frozenset({"verilog"}),
        class_nodes=frozenset({"class_declaration", "interface_declaration", "module_declaration", "package_declaration", "program_declaration"}),
        function_nodes=frozenset({"function_declaration", "task_declaration"}),
        variable_nodes=frozenset({"data_declaration", "net_declaration", "variable_decl_assignment"}),
        import_nodes=COMMON_IMPORTS | frozenset({"include_compiler_directive", "import_declaration"}),
        call_nodes=frozenset({"module_instantiation", "subroutine_call_statement", "system_tf_call"}),
        assignment_nodes=frozenset({"blocking_assignment", "nonblocking_assignment", "variable_decl_assignment"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"$display", "$fatal", "$finish"}),
    )
