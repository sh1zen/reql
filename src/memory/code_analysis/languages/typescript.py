"""TypeScript Tree-sitter extraction."""
from __future__ import annotations

from typing import Any

from ..base import (
    TreeSitterExtractorBase,
    _call_owner,
    _call_target,
    _children,
    _clean_type_text,
    _column,
    _end_line,
    _field,
    _has_direct_child,
    _import,
    _last_quoted,
    _line,
    _named_children,
    _node_text,
    _owner,
    _params,
    _parent,
    _reference,
    _string_literal,
    _symbol,
    _text,
    stable_id,
)
from ..models import CodeCall, CodeSymbol, SymbolKind


class TypeScriptTreeSitterExtractor(TreeSitterExtractorBase):
    language_key = "typescript"
    tree_sitter_module = "tree_sitter_typescript"
    tree_sitter_function = "language_typescript"

    def _walk_root(self, root: Any) -> None:
        self._walk_typescript(root, [])

    def _walk_typescript(self, node: Any, scope: list[CodeSymbol]) -> None:
        node_type = str(getattr(node, "type", ""))
        if node_type == "comment":
            self.comments.append(_text(self.artifact, _owner(scope), _node_text(self.source, node), _line(node), _end_line(node), "comment"))
            return
        if node_type in {"import_statement", "export_statement"}:
            self._typescript_import(node)
        if node_type in {"function_declaration", "generator_function_declaration"}:
            symbol = self._typescript_function_symbol(node, scope)
            if symbol:
                self.symbols.append(symbol)
                self._walk_typescript_children(node, [*scope, symbol])
                return
        if node_type == "class_declaration":
            symbol = self._typescript_class_symbol(node, scope)
            if symbol:
                self.symbols.append(symbol)
                self._walk_typescript_children(node, [*scope, symbol])
                return
        if node_type in {"method_definition", "method_signature"}:
            symbol = self._typescript_method_symbol(node, scope)
            if symbol:
                self.symbols.append(symbol)
                self._walk_typescript_children(node, [*scope, symbol])
                return
        if node_type in {"variable_declarator", "public_field_definition"}:
            symbol = self._typescript_variable_function_symbol(node, scope)
            if symbol:
                self.symbols.append(symbol)
                self._walk_typescript_children(node, [*scope, symbol])
                return
        if node_type == "call_expression":
            self._call(node, scope, function_field="function")
        self._walk_typescript_children(node, scope)

    def _walk_typescript_children(self, node: Any, scope: list[CodeSymbol]) -> None:
        for child in _children(node):
            self._walk_typescript(child, scope)

    def _typescript_import(self, node: Any) -> None:
        source_node = _field(node, "source")
        module = _string_literal(_node_text(self.source, source_node)) if source_node is not None else None
        if module is None and str(getattr(node, "type", "")) == "import_statement":
            module = _last_quoted(_node_text(self.source, node))
        if module is None:
            return
        self.imports.append(_import(self.artifact, module, None, None, _line(node), _node_text(self.source, node).strip()))

    def _typescript_class_symbol(self, node: Any, scope: list[CodeSymbol]) -> CodeSymbol | None:
        name_node = _field(node, "name")
        if name_node is None:
            return None
        base = _typescript_class_base(self.source, node)
        return _symbol(self.artifact, "class", _parent(self.module, scope), _node_text(self.source, name_node), _line(node), _end_line(node), bases=[base] if base else [])

    def _typescript_function_symbol(self, node: Any, scope: list[CodeSymbol]) -> CodeSymbol | None:
        name_node = _field(node, "name")
        if name_node is None:
            return None
        kind: SymbolKind = "async_function" if _has_direct_child(node, "async") else "function"
        return _symbol(self.artifact, kind, _parent(self.module, scope), _node_text(self.source, name_node), _line(node), _end_line(node), args=_params(self.source, _field(node, "parameters")), returns=_clean_type_text(_node_text(self.source, _field(node, "return_type"))))

    def _typescript_method_symbol(self, node: Any, scope: list[CodeSymbol]) -> CodeSymbol | None:
        name_node = _field(node, "name")
        if name_node is None:
            return None
        in_class = any(symbol.kind == "class" for symbol in scope)
        kind: SymbolKind = "async_method" if _has_direct_child(node, "async") else "method" if in_class else "function"
        return _symbol(self.artifact, kind, _parent(self.module, scope), _node_text(self.source, name_node), _line(node), _end_line(node), args=_params(self.source, _field(node, "parameters")), returns=_clean_type_text(_node_text(self.source, _field(node, "return_type"))))

    def _typescript_variable_function_symbol(self, node: Any, scope: list[CodeSymbol]) -> CodeSymbol | None:
        value = _field(node, "value")
        if value is None or str(getattr(value, "type", "")) not in {"arrow_function", "function", "function_expression"}:
            return None
        name_node = _field(node, "name")
        if name_node is None:
            return None
        kind: SymbolKind = "async_function" if _has_direct_child(value, "async") else "function"
        return _symbol(self.artifact, kind, _parent(self.module, scope), _node_text(self.source, name_node), _line(node), _end_line(value), args=_params(self.source, _field(value, "parameters")), returns=_clean_type_text(_node_text(self.source, _field(value, "return_type"))))

    def _call(self, node: Any, scope: list[CodeSymbol], *, function_field: str) -> None:
        target_node = _field(node, function_field)
        target = _call_target(self.source, target_node)
        if not target or target.split(".")[0] in {"if", "for", "while", "switch", "catch", "function", "return", "class", "import", "export"}:
            return
        caller = _call_owner(scope)
        self.references.append(_reference(self.artifact, caller or _parent(self.module, scope), target, _line(target_node), _column(target_node), "read"))
        self.calls.append(
            CodeCall(
                id=stable_id("call", self.artifact.id, caller or "", target, _line(node), _column(node)),
                artifact_id=self.artifact.id,
                caller=caller,
                target=target,
                line=_line(node),
                column=_column(node),
            )
        )


def _typescript_class_base(source: bytes, node: Any) -> str | None:
    superclass = _field(node, "superclass")
    if superclass is not None:
        return _node_text(source, superclass).strip() or None
    for child in _named_children(node):
        if str(getattr(child, "type", "")) == "class_heritage":
            value = _node_text(source, child).replace("extends", "", 1).strip()
            return value or None
    return None
