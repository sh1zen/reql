"""Language-specific Tree-sitter AST extraction profiles."""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import SymbolKind


@dataclass(frozen=True, slots=True)
class AstProfile:
    name: str
    languages: frozenset[str]
    class_nodes: frozenset[str] = field(default_factory=frozenset)
    function_nodes: frozenset[str] = field(default_factory=frozenset)
    method_nodes: frozenset[str] = field(default_factory=frozenset)
    variable_function_nodes: frozenset[str] = field(default_factory=frozenset)
    import_nodes: frozenset[str] = field(default_factory=frozenset)
    call_nodes: frozenset[str] = field(default_factory=lambda: frozenset({"call", "call_expression"}))
    identifier_nodes: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "constant",
                "constant_identifier",
                "field_identifier",
                "identifier",
                "name",
                "property_identifier",
                "simple_identifier",
                "type_identifier",
            }
        )
    )
    name_fields: tuple[str, ...] = ("name", "declarator", "declaration", "path", "type")
    parameter_fields: tuple[str, ...] = ("parameters", "parameter_list")
    return_fields: tuple[str, ...] = ("return_type", "type")
    base_fields: tuple[str, ...] = ("superclass", "superclasses", "base_class", "interfaces")
    import_fields: tuple[str, ...] = ("path", "source", "module", "name")

    def symbol_kind(self, node_type: str, *, in_class_scope: bool) -> SymbolKind | None:
        if node_type in self.class_nodes:
            return "class"
        if node_type in self.method_nodes:
            return "method"
        if node_type in self.function_nodes:
            return "method" if in_class_scope else "function"
        return None

    def is_import_node(self, node_type: str) -> bool:
        return node_type in self.import_nodes

    def is_call_node(self, node_type: str) -> bool:
        return node_type in self.call_nodes

    def is_comment_node(self, node_type: str) -> bool:
        return node_type == "comment" or node_type.endswith("_comment")


COMMON_IMPORTS = frozenset(
    {
        "include_declaration",
        "import_declaration",
        "import_from_statement",
        "import_statement",
        "import_list",
        "preproc_include",
        "require",
        "use_declaration",
        "using_declaration",
    }
)

C_FAMILY = AstProfile(
    name="c-family",
    languages=frozenset({"c", "cpp", "csharp", "java", "kotlin", "swift"}),
    class_nodes=frozenset(
        {
            "class_declaration",
            "class_specifier",
            "enum_declaration",
            "enum_specifier",
            "interface_declaration",
            "object_declaration",
            "struct_declaration",
            "struct_specifier",
            "union_specifier",
        }
    ),
    function_nodes=frozenset({"function_declaration", "function_definition"}),
    method_nodes=frozenset({"constructor_declaration", "method_declaration", "method_definition"}),
    variable_function_nodes=frozenset({"variable_declarator"}),
    import_nodes=COMMON_IMPORTS | frozenset({"package_declaration", "using_directive"}),
)

SCRIPT_FAMILY = AstProfile(
    name="script-family",
    languages=frozenset({"bash", "elixir", "julia", "lua", "php", "powershell", "ruby"}),
    class_nodes=frozenset({"class", "class_declaration", "class_definition", "module", "module_definition"}),
    function_nodes=frozenset(
        {
            "anonymous_function",
            "function",
            "function_declaration",
            "function_definition",
            "function_statement",
            "method",
            "method_definition",
            "singleton_method",
        }
    ),
    import_nodes=COMMON_IMPORTS | frozenset({"dot_sourcing_statement"}),
)

RUST_PROFILE = AstProfile(
    name="rust",
    languages=frozenset({"rust"}),
    class_nodes=frozenset({"enum_item", "impl_item", "mod_item", "struct_item", "trait_item", "type_item", "union_item"}),
    function_nodes=frozenset({"function_item"}),
    import_nodes=COMMON_IMPORTS | frozenset({"extern_crate_declaration", "use_declaration"}),
)

GO_PROFILE = AstProfile(
    name="go",
    languages=frozenset({"go"}),
    class_nodes=frozenset({"type_declaration", "type_spec"}),
    function_nodes=frozenset({"function_declaration"}),
    method_nodes=frozenset({"method_declaration"}),
    import_nodes=COMMON_IMPORTS | frozenset({"import_declaration", "import_spec"}),
)

WEB_PROFILE = AstProfile(
    name="web",
    languages=frozenset({"javascript", "typescript", "tsx"}),
    class_nodes=frozenset({"class_declaration"}),
    function_nodes=frozenset({"function_declaration", "generator_function_declaration"}),
    method_nodes=frozenset({"method_definition", "method_signature"}),
    variable_function_nodes=frozenset({"public_field_definition", "variable_declarator"}),
    import_nodes=frozenset({"export_statement", "import_statement"}),
)

PYTHON_PROFILE = AstProfile(
    name="python",
    languages=frozenset({"python"}),
    class_nodes=frozenset({"class_definition"}),
    function_nodes=frozenset({"function_definition"}),
    import_nodes=frozenset({"import_from_statement", "import_statement"}),
)

DECLARATIVE_PROFILE = AstProfile(
    name="declarative",
    languages=frozenset({"apex", "dockerfile", "fsharp", "just", "makefile", "pascal", "razor", "sql", "terraform", "verilog", "zig"}),
    class_nodes=frozenset(
        {
            "class_declaration",
            "component",
            "contract_declaration",
            "entity_declaration",
            "module_declaration",
            "module_instantiation",
            "resource",
            "struct_declaration",
            "type_declaration",
        }
    ),
    function_nodes=frozenset({"function_declaration", "function_definition", "recipe", "rule", "task", "subroutine"}),
    import_nodes=COMMON_IMPORTS | frozenset({"copy_instruction", "from_instruction", "include", "module_instantiation"}),
)

SCALA_PROFILE = AstProfile(
    name="scala",
    languages=frozenset({"scala"}),
    class_nodes=frozenset({"class_definition", "enum_definition", "object_definition", "trait_definition", "type_definition"}),
    function_nodes=frozenset({"function_definition"}),
    import_nodes=COMMON_IMPORTS | frozenset({"import_declaration"}),
)

FORTRAN_PROFILE = AstProfile(
    name="fortran",
    languages=frozenset({"fortran"}),
    class_nodes=frozenset({"module", "program", "type_definition"}),
    function_nodes=frozenset({"function", "function_subprogram", "subroutine", "subroutine_subprogram"}),
    import_nodes=COMMON_IMPORTS | frozenset({"use_statement"}),
)

AST_PROFILES = (
    PYTHON_PROFILE,
    WEB_PROFILE,
    C_FAMILY,
    GO_PROFILE,
    RUST_PROFILE,
    SCALA_PROFILE,
    FORTRAN_PROFILE,
    SCRIPT_FAMILY,
    DECLARATIVE_PROFILE,
)

_PROFILE_BY_LANGUAGE = {language: profile for profile in AST_PROFILES for language in profile.languages}
DEFAULT_PROFILE = AstProfile(name="default", languages=frozenset())


def profile_for(language: str | None) -> AstProfile:
    return _PROFILE_BY_LANGUAGE.get(language or "", DEFAULT_PROFILE)
