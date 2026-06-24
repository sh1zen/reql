"""SQL Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class SqlTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "sql"
    tree_sitter_module = "tree_sitter_sql"
    profile = AstProfile(
        name="sql",
        languages=frozenset({"sql"}),
        class_nodes=frozenset({"create_table_statement", "create_view_statement", "table_definition", "view_definition"}),
        function_nodes=frozenset({"create_function_statement", "function_definition", "procedure_statement"}),
        variable_nodes=frozenset({"column_definition"}),
        import_nodes=COMMON_IMPORTS,
        call_nodes=frozenset({"function_call", "select_statement"}),
        assignment_nodes=frozenset({"assignment_statement"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"avg", "count", "max", "min", "select", "sum"}),
    )
