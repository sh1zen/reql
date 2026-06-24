"""Profile-driven fallback Tree-sitter extraction."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...artifacts.models import SourceArtifact
from ..base import (
    TreeSitterExtractorBase,
    _call_owner,
    _call_target,
    _children,
    _clean_type_text,
    _column,
    _end_line,
    _field,
    _import,
    _line,
    _named_children,
    _node_text,
    _owner,
    _parent,
    _reference,
    _symbol,
    _text,
    _valid_symbol_name,
    stable_id,
)
from ..models import CodeCall, CodeSymbol, SymbolKind


COMMON_IDENTIFIER_NODES = frozenset(
    {
        "alias",
        "builtin_identifier",
        "constant",
        "constant_identifier",
        "command_name",
        "field_identifier",
        "function_name",
        "identifier",
        "name",
        "package_identifier",
        "property_identifier",
        "scoped_identifier",
        "simple_identifier",
        "type_identifier",
        "word",
    }
)


@dataclass(frozen=True, slots=True)
class AstProfile:
    name: str
    languages: frozenset[str]
    class_nodes: frozenset[str] = field(default_factory=frozenset)
    function_nodes: frozenset[str] = field(default_factory=frozenset)
    method_nodes: frozenset[str] = field(default_factory=frozenset)
    variable_nodes: frozenset[str] = field(default_factory=frozenset)
    variable_function_nodes: frozenset[str] = field(default_factory=frozenset)
    import_nodes: frozenset[str] = field(default_factory=frozenset)
    call_nodes: frozenset[str] = field(default_factory=lambda: frozenset({"call", "call_expression"}))
    assignment_nodes: frozenset[str] = field(default_factory=lambda: frozenset({"assignment", "assignment_expression", "assignment_statement"}))
    return_nodes: frozenset[str] = field(default_factory=lambda: frozenset({"return_statement"}))
    raise_nodes: frozenset[str] = field(default_factory=lambda: frozenset({"raise_statement", "throw_statement"}))
    identifier_nodes: frozenset[str] = field(default_factory=lambda: COMMON_IDENTIFIER_NODES)
    name_fields: tuple[str, ...] = ("name", "declarator", "declaration", "path", "type")
    parameter_fields: tuple[str, ...] = ("parameters", "parameter_list", "formal_parameters", "function_value_parameters")
    return_fields: tuple[str, ...] = ("return_type", "type", "result")
    base_fields: tuple[str, ...] = ("superclass", "superclasses", "base_class", "base_clause", "extends_clause", "interfaces", "super_interfaces")
    import_fields: tuple[str, ...] = ("path", "source", "module", "name")
    call_fields: tuple[str, ...] = ("function", "target", "command", "name")
    assignment_left_fields: tuple[str, ...] = ("left", "target", "name")
    assignment_right_fields: tuple[str, ...] = ("right", "value")
    import_call_names: frozenset[str] = field(default_factory=frozenset)
    class_call_names: frozenset[str] = field(default_factory=frozenset)
    function_call_names: frozenset[str] = field(default_factory=frozenset)
    private_function_call_names: frozenset[str] = field(default_factory=frozenset)
    builtin_call_names: frozenset[str] = field(default_factory=frozenset)

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

    def is_assignment_node(self, node_type: str) -> bool:
        return node_type in self.assignment_nodes

    def is_return_node(self, node_type: str) -> bool:
        return node_type in self.return_nodes

    def is_raise_node(self, node_type: str) -> bool:
        return node_type in self.raise_nodes

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
COMMON_CONTROL_CALLS = frozenset(
    {
        "catch",
        "class",
        "def",
        "defmodule",
        "defp",
        "do",
        "else",
        "elseif",
        "for",
        "foreach",
        "function",
        "if",
        "import",
        "lambda",
        "module",
        "return",
        "switch",
        "throw",
        "try",
        "while",
        "with",
    }
)

DEFAULT_PROFILE = AstProfile(name="default", languages=frozenset())


def profile_for(language: str | None) -> AstProfile:
    return DEFAULT_PROFILE


class GenericProfileTreeSitterExtractor(TreeSitterExtractorBase):
    profile: AstProfile = DEFAULT_PROFILE

    def __init__(self, artifact: SourceArtifact, source: bytes, language: str, language_key: str) -> None:
        super().__init__(artifact, source, language, language_key)
        self.profile = getattr(type(self), "profile", DEFAULT_PROFILE)

    def _walk_root(self, root: Any) -> None:
        self._walk_generic(root, [])

    def _walk_generic(self, node: Any, scope: list[CodeSymbol]) -> None:
        node_type = str(getattr(node, "type", ""))
        if self.profile.is_comment_node(node_type):
            self.comments.append(_text(self.artifact, _owner(scope), _node_text(self.source, node), _line(node), _end_line(node), "comment"))
            return
        macro_kind = _macro_symbol_kind(self.source, node, self.profile, in_class_scope=any(symbol.kind == "class" for symbol in scope))
        if macro_kind is not None:
            symbol = self._generic_macro_symbol(node, scope, macro_kind)
            if symbol is not None:
                self.symbols.append(symbol)
                self._walk_generic_macro_children(node, [*scope, symbol])
                return
        if _is_import_call(self.source, node, self.profile):
            self._generic_import_call(node)
            return
        if self.profile.is_import_node(node_type):
            self._generic_import(node)
        symbol_kind = self.profile.symbol_kind(node_type, in_class_scope=any(symbol.kind == "class" for symbol in scope))
        if symbol_kind is not None:
            symbol = self._generic_symbol(node, scope, symbol_kind)
            if symbol is not None:
                self.symbols.append(symbol)
                self._walk_generic_children(node, [*scope, symbol])
                return
        if node_type in self.profile.variable_function_nodes:
            symbol = self._generic_variable_function_symbol(node, scope)
            if symbol is not None:
                self.symbols.append(symbol)
                self._walk_generic_children(node, [*scope, symbol])
                return
        if node_type in self.profile.variable_nodes:
            symbol = self._generic_variable_container_symbol(node, scope)
            if symbol is not None:
                self.symbols.append(symbol)
                self._walk_generic_children(node, [*scope, symbol])
                return
            self._generic_variable_symbols(node, scope)
        if self.profile.is_assignment_node(node_type):
            self._generic_assignment_references(node, scope)
        if self.profile.is_return_node(node_type):
            self._generic_expression_references(node, scope, access="return")
        if self.profile.is_raise_node(node_type):
            self._generic_expression_references(node, scope, access="raise")
        if self.profile.is_call_node(node_type):
            self._call(node, scope)
        self._walk_generic_children(node, scope)

    def _walk_generic_children(self, node: Any, scope: list[CodeSymbol]) -> None:
        for child in _children(node):
            self._walk_generic(child, scope)

    def _walk_generic_macro_children(self, node: Any, scope: list[CodeSymbol]) -> None:
        for child in _children(node):
            if str(getattr(child, "type", "")) in {"arguments", "identifier"}:
                continue
            self._walk_generic(child, scope)

    def _generic_symbol(self, node: Any, scope: list[CodeSymbol], kind: SymbolKind) -> CodeSymbol | None:
        if self.language_key == "zig" and str(getattr(node, "type", "")) == "struct_declaration":
            return None
        name_node = _declaration_name_node(node, self.profile)
        if name_node is None:
            return None
        name = _node_text(self.source, name_node).strip()
        if not _valid_symbol_name(name):
            return None
        symbol = _symbol(
            self.artifact,
            kind,
            _parent(self.module, scope),
            name,
            _line(node),
            _end_line(node),
            bases=_generic_bases(self.source, node, self.profile),
            args=_generic_params(self.source, node, self.profile),
            returns=_clean_type_text(_node_text(self.source, _first_field(node, self.profile.return_fields))),
        )
        symbol.metadata.update({"language": self.language_key, "profile": self.profile.name, "ast_node": str(getattr(node, "type", ""))})
        return symbol

    def _generic_variable_container_symbol(self, node: Any, scope: list[CodeSymbol]) -> CodeSymbol | None:
        container = _variable_container_value(node)
        if container is None:
            return None
        name_node = _declaration_name_node(node, self.profile)
        if name_node is None:
            return None
        name = _node_text(self.source, name_node).strip()
        if not _valid_symbol_name(name) or _generic_name_is_noise(name, self.profile):
            return None
        symbol = _symbol(self.artifact, "class", _parent(self.module, scope), name, _line(node), _end_line(node))
        symbol.metadata.update(
            {
                "language": self.language_key,
                "profile": self.profile.name,
                "ast_node": str(getattr(node, "type", "")),
                "container_ast_node": str(getattr(container, "type", "")),
            }
        )
        return symbol

    def _generic_macro_symbol(self, node: Any, scope: list[CodeSymbol], kind: SymbolKind) -> CodeSymbol | None:
        name = _macro_symbol_name(self.source, node, self.profile)
        if not name or not _valid_symbol_name(name):
            return None
        symbol = _symbol(
            self.artifact,
            kind,
            _parent(self.module, scope),
            name,
            _line(node),
            _end_line(node),
            args=_generic_params(self.source, node, self.profile),
        )
        symbol.metadata.update({"language": self.language_key, "profile": self.profile.name, "ast_node": str(getattr(node, "type", ""))})
        return symbol

    def _generic_variable_function_symbol(self, node: Any, scope: list[CodeSymbol]) -> CodeSymbol | None:
        value = _field(node, "value")
        if value is None:
            value = _assignment_right(node, self.profile)
        if value is None or not _node_defines_callable(value):
            return None
        name_node = _declaration_name_node(node, self.profile)
        if name_node is None:
            return None
        name = _node_text(self.source, name_node).strip()
        if not _valid_symbol_name(name):
            return None
        symbol = _symbol(
            self.artifact,
            "method" if any(symbol.kind == "class" for symbol in scope) else "function",
            _parent(self.module, scope),
            name,
            _line(node),
            _end_line(value),
            args=_generic_params(self.source, value, self.profile),
            returns=_clean_type_text(_node_text(self.source, _first_field(value, self.profile.return_fields))),
        )
        symbol.metadata.update({"language": self.language_key, "profile": self.profile.name, "ast_node": str(getattr(node, "type", ""))})
        return symbol

    def _generic_variable_symbols(self, node: Any, scope: list[CodeSymbol]) -> None:
        if not scope or _node_contains_declaration_node(node, self.profile):
            return
        owner = _call_owner(scope)
        if owner is None:
            return
        for name_node in _variable_name_nodes(node, self.profile):
            name = _node_text(self.source, name_node).strip()
            if not _valid_symbol_name(name) or _generic_name_is_noise(name, self.profile):
                continue
            symbol = _symbol(self.artifact, "variable", owner, name, _line(name_node), _line(name_node))
            symbol.metadata.update({"language": self.language_key, "profile": self.profile.name, "ast_node": str(getattr(node, "type", ""))})
            if not any(existing.id == symbol.id for existing in self.symbols):
                self.symbols.append(symbol)
            self.references.append(_reference(self.artifact, owner, name, _line(name_node), _column(name_node), "write"))

    def _generic_import(self, node: Any) -> None:
        raw = _node_text(self.source, node).strip()
        module = _generic_import_target(self.source, node, self.profile)
        if module is None:
            return
        name, alias = _generic_import_name_alias(self.source, node, self.profile)
        metadata = {"language": self.language_key, "profile": self.profile.name, "ast_node": str(getattr(node, "type", ""))}
        self.imports.append(_import(self.artifact, module, name, alias, _line(node), raw, metadata=metadata))

    def _generic_import_call(self, node: Any) -> None:
        raw = _node_text(self.source, node).strip()
        target = _generic_import_call_target(self.source, node, self.profile)
        if not target:
            return
        name = _generic_import_call_head(self.source, node, self.profile)
        metadata = {"language": self.language_key, "profile": self.profile.name, "ast_node": str(getattr(node, "type", "")), "import_form": name}
        self.imports.append(_import(self.artifact, target, None, None, _line(node), raw, metadata=metadata))

    def _generic_assignment_references(self, node: Any, scope: list[CodeSymbol]) -> None:
        owner = _call_owner(scope) or _parent(self.module, scope)
        left = _assignment_left(node, self.profile)
        right = _assignment_right(node, self.profile)
        for name_node in _identifier_nodes(left, self.profile):
            name = _node_text(self.source, name_node).strip()
            if _valid_symbol_name(name) and not _generic_name_is_noise(name, self.profile):
                self.references.append(_reference(self.artifact, owner, name, _line(name_node), _column(name_node), "write"))
        if right is not None:
            self._generic_expression_references(right, scope, access="read")

    def _generic_expression_references(self, node: Any, scope: list[CodeSymbol], *, access: str) -> None:
        owner = _call_owner(scope) or _parent(self.module, scope)
        for name_node in _identifier_nodes(node, self.profile):
            name = _node_text(self.source, name_node).strip()
            if _valid_symbol_name(name) and not _generic_name_is_noise(name, self.profile):
                self.references.append(_reference(self.artifact, owner, name, _line(name_node), _column(name_node), access))

    def _call(self, node: Any, scope: list[CodeSymbol]) -> None:
        target_node = _call_target_node(node, self.profile)
        target = _call_target(self.source, target_node)
        if not target or _generic_name_is_noise(target, self.profile):
            return
        caller = _call_owner(scope)
        self.references.append(_reference(self.artifact, caller or _parent(self.module, scope), target, _line(target_node), _column(target_node), "read"))
        for argument in _call_argument_nodes(node, self.profile):
            self._generic_expression_references(argument, scope, access="read")
        self.calls.append(
            CodeCall(
                id=stable_id("call", self.artifact.id, caller or "", target, _line(node), _column(node)),
                artifact_id=self.artifact.id,
                caller=caller,
                target=target,
                line=_line(node),
                column=_column(node),
                metadata={"language": self.language_key, "profile": self.profile.name},
            )
        )


def _declaration_name_node(node: Any | None, profile: AstProfile) -> Any | None:
    if node is None:
        return None
    for field_name in profile.name_fields:
        candidate = _field(node, field_name)
        direct = _direct_identifier(candidate, profile)
        if direct is not None:
            return direct
        nested = _declaration_name_node(candidate, profile)
        if nested is not None:
            return nested
    return _first_identifier(node, profile)


def _direct_identifier(node: Any | None, profile: AstProfile) -> Any | None:
    if node is None:
        return None
    node_type = str(getattr(node, "type", ""))
    if node_type in profile.identifier_nodes:
        return node
    return None


def _first_identifier(node: Any | None, profile: AstProfile) -> Any | None:
    direct = _direct_identifier(node, profile)
    if direct is not None:
        return direct
    for child in _named_children(node):
        found = _first_identifier(child, profile)
        if found is not None:
            return found
    return None


def _generic_bases(source: bytes, node: Any, profile: AstProfile) -> list[str]:
    values: list[str] = []
    for field_name in profile.base_fields:
        field = _field(node, field_name)
        if field is not None:
            values.extend(_identifier_texts(source, field, profile))
    return [value for index, value in enumerate(values) if value and value not in values[:index]]


def _identifier_texts(source: bytes, node: Any | None, profile: AstProfile) -> list[str]:
    if node is None:
        return []
    node_type = str(getattr(node, "type", ""))
    if node_type in profile.identifier_nodes:
        return [_node_text(source, node).strip()]
    values: list[str] = []
    for child in _named_children(node):
        values.extend(_identifier_texts(source, child, profile))
    return values


def _generic_params(source: bytes, node: Any, profile: AstProfile) -> list[str]:
    params = _first_field(node, profile.parameter_fields)
    if params is None:
        return []
    names: list[str] = []
    for child in _named_children(params):
        values = _identifier_texts(source, child, profile)
        if values:
            names.append(values[0])
    return names


def _generic_import_target(source: bytes, node: Any, profile: AstProfile) -> str | None:
    for field_name in profile.import_fields:
        target = _field(node, field_name)
        value = _clean_import_text(_node_text(source, target))
        if value:
            return value
    for child in _named_children(node):
        child_type = str(getattr(child, "type", ""))
        raw = _node_text(source, child)
        if child_type in {"interpreted_string_literal", "raw_string_literal", "string", "string_literal", "system_lib_string"}:
            value = _clean_import_text(raw)
            if value:
                return value
        if child_type in profile.identifier_nodes:
            value = _clean_import_text(raw)
            if value:
                return value
    return _clean_import_text(_node_text(source, node))


def _generic_import_name_alias(source: bytes, node: Any, profile: AstProfile) -> tuple[str | None, str | None]:
    identifiers = [_clean_import_text(_node_text(source, item)) for item in _identifier_nodes(node, profile)]
    identifiers = [item for item in identifiers if item and item.casefold() not in {"as", "from", "import", "package", "use", "using"}]
    alias: str | None = None
    raw = _node_text(source, node)
    if " as " in raw:
        tail = raw.rsplit(" as ", 1)[-1].strip().strip(";")
        alias = _clean_import_text(tail)
    if len(identifiers) >= 2:
        return identifiers[-2], alias or identifiers[-1] if " as " in raw else None
    if identifiers:
        return identifiers[-1], alias
    return None, alias


def _macro_symbol_kind(source: bytes, node: Any, profile: AstProfile, *, in_class_scope: bool) -> SymbolKind | None:
    head = _generic_import_call_head(source, node, profile)
    if head in profile.class_call_names:
        return "class"
    if head in profile.function_call_names:
        return "method" if in_class_scope else "function"
    if head in profile.private_function_call_names:
        return "method" if in_class_scope else "function"
    return None


def _macro_symbol_name(source: bytes, node: Any, profile: AstProfile) -> str | None:
    arguments = _arguments_node(node)
    if arguments is None:
        return None
    first = _first_identifier(arguments, profile)
    if first is None:
        return None
    return _node_text(source, first).strip() or None


def _is_import_call(source: bytes, node: Any, profile: AstProfile) -> bool:
    return _generic_import_call_head(source, node, profile) in profile.import_call_names


def _generic_import_call_head(source: bytes, node: Any, profile: AstProfile) -> str | None:
    node_type = str(getattr(node, "type", ""))
    if node_type not in profile.call_nodes and node_type != "call":
        return None
    target = _call_target_node(node, profile)
    value = _call_target(source, target)
    if value:
        return value.strip()
    return None


def _generic_import_call_target(source: bytes, node: Any, profile: AstProfile) -> str | None:
    arguments = _arguments_node(node)
    if arguments is not None:
        quoted = _quoted_text_in_node(source, arguments)
        if quoted:
            return quoted
        first_identifier = _first_identifier(arguments, profile)
        if first_identifier is not None:
            return _node_text(source, first_identifier).strip() or None
    named = _named_children(node)
    for child in named[1:]:
        value = _clean_import_text(_node_text(source, child))
        if value:
            return value
    raw = _node_text(source, node).strip()
    head = _generic_import_call_head(source, node, profile)
    if head and raw.startswith(head):
        return _clean_import_text(raw[len(head) :])
    return None


def _call_target_node(node: Any, profile: AstProfile) -> Any | None:
    for field_name in profile.call_fields:
        target = _field(node, field_name)
        if target is not None:
            direct = _direct_identifier(target, profile)
            return direct or target
    for child in _named_children(node):
        child_type = str(getattr(child, "type", ""))
        if child_type in {"argument_list", "arguments", "block", "do_block", "string", "string_literal"}:
            continue
        direct = _direct_identifier(child, profile)
        if direct is not None:
            return direct
        nested = _first_identifier(child, profile)
        if nested is not None:
            return nested
    return None


def _call_argument_nodes(node: Any, profile: AstProfile) -> list[Any]:
    values: list[Any] = []
    for field_name in ("arguments", "argument", "argument_list"):
        field = _field(node, field_name)
        if field is not None:
            values.append(field)
    if values:
        return values
    arguments = _arguments_node(node)
    if arguments is not None:
        return [arguments]
    children = _named_children(node)
    return children[1:] if len(children) > 1 else []


def _assignment_left(node: Any, profile: AstProfile) -> Any | None:
    direct = _first_field(node, profile.assignment_left_fields)
    if direct is not None:
        return direct
    children = _named_children(node)
    if children:
        return children[0]
    return None


def _assignment_right(node: Any, profile: AstProfile) -> Any | None:
    direct = _first_field(node, profile.assignment_right_fields)
    if direct is not None:
        return direct
    children = _named_children(node)
    if len(children) >= 2:
        return children[-1]
    return None


def _node_defines_callable(node: Any) -> bool:
    node_type = str(getattr(node, "type", ""))
    if node_type in {"arrow_function", "function", "function_definition", "function_declaration", "function_expression", "lambda", "method"}:
        return True
    return any(_node_defines_callable(child) for child in _named_children(node))


def _variable_container_value(node: Any) -> Any | None:
    for child in _named_children(node):
        if str(getattr(child, "type", "")) in {
            "class_definition",
            "enum_declaration",
            "enum_definition",
            "object_definition",
            "struct_declaration",
            "struct_definition",
            "trait_definition",
            "type_definition",
        }:
            return child
    return None


def _node_contains_declaration_node(node: Any, profile: AstProfile) -> bool:
    nested_declarations = profile.class_nodes | profile.function_nodes | profile.method_nodes
    for child in _named_children(node):
        child_type = str(getattr(child, "type", ""))
        if child_type in nested_declarations:
            return True
    return False


def _variable_name_nodes(node: Any, profile: AstProfile) -> list[Any]:
    left = _assignment_left(node, profile) if str(getattr(node, "type", "")) in profile.assignment_nodes else None
    if left is not None:
        return _identifier_nodes(left, profile)
    for field_name in ("name", "declarator", "declaration", "left"):
        candidate = _field(node, field_name)
        name_node = _declaration_name_node(candidate, profile)
        if name_node is not None:
            return [name_node]
    name_node = _declaration_name_node(node, profile)
    return [name_node] if name_node is not None else []


def _identifier_nodes(node: Any | None, profile: AstProfile) -> list[Any]:
    if node is None:
        return []
    node_type = str(getattr(node, "type", ""))
    if node_type in profile.identifier_nodes:
        return [node]
    values: list[Any] = []
    for child in _named_children(node):
        values.extend(_identifier_nodes(child, profile))
    return values


def _generic_name_is_noise(name: str | None, profile: AstProfile) -> bool:
    if not name:
        return True
    value = name.strip().removeprefix("@").strip()
    if not value:
        return True
    head = value.split(".", 1)[0].split("::", 1)[0].split("(", 1)[0].strip()
    return head in profile.builtin_call_names or head.casefold() in {item.casefold() for item in profile.builtin_call_names}


def _quoted_text_in_node(source: bytes, node: Any | None) -> str | None:
    if node is None:
        return None
    raw = _node_text(source, node).strip()
    value = _clean_import_text(raw)
    if value and value != raw:
        return value
    for child in _named_children(node):
        child_type = str(getattr(child, "type", ""))
        if "string" in child_type:
            value = _clean_import_text(_node_text(source, child))
            if value:
                return value
        value = _quoted_text_in_node(source, child)
        if value:
            return value
    return None


def _arguments_node(node: Any | None) -> Any | None:
    if node is None:
        return None
    direct = _field(node, "arguments")
    if direct is not None:
        return direct
    for child in _named_children(node):
        if str(getattr(child, "type", "")) in {"argument_list", "arguments"}:
            return child
    return None


def _first_field(node: Any | None, names: tuple[str, ...]) -> Any | None:
    for name in names:
        field = _field(node, name)
        if field is not None:
            return field
    return None


def _clean_import_text(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    for token in ("import", "from", "include", "package", "require", "use", "using", "#include"):
        if value.casefold().startswith(token):
            value = value[len(token) :].strip()
    value = value.strip(";").strip()
    if value.startswith(("<", '"', "'", "`")) and value.endswith((">", '"', "'", "`")):
        value = value[1:-1].strip()
    return value or None
