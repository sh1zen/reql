"""Python Tree-sitter extraction."""
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
    _first_child_of_type,
    _first_named_child,
    _has_direct_child,
    _import,
    _last_named_child,
    _line,
    _named_children,
    _node_text,
    _owner,
    _params,
    _parent,
    _reference,
    _symbol,
    _text,
    stable_id,
)
from ...models import CodeCall, CodeSymbol, SymbolKind


class PythonTreeSitterExtractor(TreeSitterExtractorBase):
    language_key = "python"
    tree_sitter_module = "tree_sitter_python"

    def _walk_root(self, root: Any) -> None:
        self._extract_python_module_docstring(root)
        self._walk_python(root, [])

    def _walk_python(self, node: Any, scope: list[CodeSymbol]) -> None:
        node_type = str(getattr(node, "type", ""))
        if node_type == "comment":
            self.comments.append(_text(self.artifact, _owner(scope), _node_text(self.source, node), _line(node), _end_line(node), "comment"))
            return
        if node_type == "decorated_definition":
            decorators = [_decorator_text(self.source, child) for child in _children(node) if str(getattr(child, "type", "")) == "decorator"]
            definition = _field(node, "definition") or _last_named_child(node)
            symbol = self._python_symbol(definition, scope, decorators=[item for item in decorators if item])
            if symbol:
                self.symbols.append(symbol)
                self._add_python_docstring(symbol, definition)
                if str(getattr(definition, "type", "")) == "function_definition":
                    self._python_signature_references(definition, scope)
                self._walk_python_body(definition, [*scope, symbol])
            return
        if node_type in {"function_definition", "class_definition"}:
            symbol = self._python_symbol(node, scope, decorators=[])
            if symbol:
                self.symbols.append(symbol)
                self._add_python_docstring(symbol, node)
                if node_type == "function_definition":
                    self._python_signature_references(node, scope)
                self._walk_python_body(node, [*scope, symbol])
            return
        if node_type in {"import_statement", "import_from_statement"}:
            self._python_import(node)
            return
        if node_type in {"assignment", "augmented_assignment"}:
            self._python_assignment(node, scope)
            return
        if node_type == "call":
            self._call(node, scope, function_field="function")
            self._python_call_argument_references(node, scope)
        if node_type in {"if_statement", "elif_clause", "while_statement"}:
            self._python_condition_references(node, scope)
        if node_type == "for_statement":
            self._python_for_references(node, scope)
        if node_type == "return_statement":
            self._python_return_or_raise(node, scope, access="return")
        if node_type == "raise_statement":
            self._python_return_or_raise(node, scope, access="raise")
        if node_type == "except_clause":
            self._python_exception_references(node, scope)
        self._walk_python_children(node, scope)

    def _walk_python_children(self, node: Any, scope: list[CodeSymbol]) -> None:
        for child in _children(node):
            self._walk_python(child, scope)

    def _walk_python_body(self, node: Any, scope: list[CodeSymbol]) -> None:
        body = _field(node, "body")
        if body is None:
            self._walk_python_children(node, scope)
            return
        for child in _children(body):
            if _is_docstring_statement(child):
                continue
            self._walk_python(child, scope)

    def _python_symbol(self, node: Any | None, scope: list[CodeSymbol], *, decorators: list[str]) -> CodeSymbol | None:
        if node is None:
            return None
        node_type = str(getattr(node, "type", ""))
        if node_type == "class_definition":
            name_node = _field(node, "name")
            if name_node is None:
                return None
            bases = _python_bases(self.source, node)
            name = _node_text(self.source, name_node)
            symbol = _symbol(self.artifact, "class", _parent(self.module, scope), name, _line(node), _end_line(node), decorators=decorators, bases=bases)
            symbol.metadata["is_interface"] = _is_interface_class(symbol.name, bases)
            symbol.metadata["is_schema"] = _is_schema_class(bases, symbol.decorators)
            return symbol
        if node_type == "function_definition":
            name_node = _field(node, "name")
            if name_node is None:
                return None
            in_class = any(symbol.kind == "class" for symbol in scope)
            is_async = _has_direct_child(node, "async")
            kind: SymbolKind = "async_method" if in_class and is_async else "method" if in_class else "async_function" if is_async else "function"
            parameters = _field(node, "parameters")
            symbol = _symbol(
                self.artifact,
                kind,
                _parent(self.module, scope),
                _node_text(self.source, name_node),
                _line(node),
                _end_line(node),
                decorators=decorators,
                args=_params(self.source, parameters),
                returns=_clean_type_text(_node_text(self.source, _field(node, "return_type"))),
            )
            param_annotations = _param_annotations(self.source, parameters)
            if param_annotations:
                symbol.metadata["param_annotations"] = param_annotations
            return symbol
        return None

    def _extract_python_module_docstring(self, root: Any) -> None:
        for child in _children(root):
            if str(getattr(child, "type", "")) == "comment":
                continue
            docstring = _docstring_text(self.source, child)
            if docstring is None:
                return
            self.module.docstring = docstring
            self.docstrings.append(_text(self.artifact, self.module.name, docstring, _line(child), _end_line(child), "docstring"))
            return

    def _add_python_docstring(self, symbol: CodeSymbol, node: Any) -> None:
        body = _field(node, "body")
        if body is None:
            return
        for child in _children(body):
            if str(getattr(child, "type", "")) == "comment":
                continue
            docstring = _docstring_text(self.source, child)
            if docstring is None:
                return
            symbol.docstring = docstring
            self.docstrings.append(_text(self.artifact, symbol.qualified_name, docstring, _line(child), _end_line(child), "docstring"))
            return

    def _python_import(self, node: Any) -> None:
        raw = _node_text(self.source, node).strip()
        if str(getattr(node, "type", "")) == "import_statement":
            for child in _children(node):
                if str(getattr(child, "type", "")) == "aliased_import":
                    name = _node_text(self.source, _field(child, "name")).strip()
                    alias = _node_text(self.source, _field(child, "alias")).strip() or None
                    self.imports.append(_import(self.artifact, name, None, alias, _line(node), raw))
                elif str(getattr(child, "type", "")) == "dotted_name":
                    self.imports.append(_import(self.artifact, _node_text(self.source, child), None, None, _line(node), raw))
            return
        module = _node_text(self.source, _field(node, "module_name")).strip() or None
        level = _relative_import_level(raw)
        names = _python_import_names(self.source, node)
        if not names:
            self.imports.append(_import(self.artifact, module, None, None, _line(node), raw, level=level))
            return
        for name, alias in names:
            self.imports.append(_import(self.artifact, module, name, alias, _line(node), raw, level=level))

    def _python_assignment(self, node: Any, scope: list[CodeSymbol]) -> None:
        target_node = _field(node, "left") or _first_named_child(node)
        value_node = _field(node, "right")
        annotation = _clean_type_text(_node_text(self.source, _field(node, "type")))
        for name, target in _assignment_names(self.source, target_node):
            parent = _parent(self.module, scope)
            qualified = f"{parent}.{name}" if parent else name
            symbol = CodeSymbol(
                id=stable_id("code-symbol", self.artifact.id, "variable", qualified, _line(target)),
                artifact_id=self.artifact.id,
                kind="variable",
                name=name,
                qualified_name=qualified,
                start_line=_line(target),
                end_line=_end_line(target),
                parent_qualified_name=parent,
                returns=annotation,
                metadata={"annotation": annotation} if annotation else {},
            )
            if symbol.id not in {existing.id for existing in self.symbols}:
                self.symbols.append(symbol)
            self.references.append(_reference(self.artifact, _call_owner(scope) or parent, name, _line(target), _column(target), "write"))
        if str(getattr(node, "type", "")) == "augmented_assignment":
            owner = _call_owner(scope) or _parent(self.module, scope)
            for name, ref_node in _read_reference_names(self.source, target_node):
                self.references.append(_reference(self.artifact, owner, name, _line(ref_node), _column(ref_node), "read"))
        for name, ref_node in _read_reference_names(self.source, value_node):
            self.references.append(_reference(self.artifact, _call_owner(scope) or _parent(self.module, scope), name, _line(ref_node), _column(ref_node), "read"))
        self._walk_python(value_node, scope) if value_node is not None else None

    def _python_return_or_raise(self, node: Any, scope: list[CodeSymbol], *, access: str) -> None:
        target_node = _first_named_child(node)
        for name, ref_node in _read_reference_names(self.source, target_node):
            self.references.append(_reference(self.artifact, _call_owner(scope) or _parent(self.module, scope), name, _line(ref_node), _column(ref_node), access))

    def _python_call_argument_references(self, node: Any, scope: list[CodeSymbol]) -> None:
        args = _field(node, "arguments") or _first_child_of_type(node, {"argument_list", "arguments"})
        owner = _call_owner(scope) or _parent(self.module, scope)
        for name, ref_node in _read_reference_names(self.source, args):
            self.references.append(_reference(self.artifact, owner, name, _line(ref_node), _column(ref_node), "read"))

    def _python_signature_references(self, node: Any, scope: list[CodeSymbol]) -> None:
        parameters = _field(node, "parameters")
        owner = _parent(self.module, scope)
        for name, ref_node in _python_parameter_usage_names(self.source, parameters):
            self.references.append(_reference(self.artifact, owner, name, _line(ref_node), _column(ref_node), "read"))

    def _python_exception_references(self, node: Any, scope: list[CodeSymbol]) -> None:
        owner = _call_owner(scope) or _parent(self.module, scope)
        for name, ref_node in _python_exception_usage_names(self.source, node):
            self.references.append(_reference(self.artifact, owner, name, _line(ref_node), _column(ref_node), "read"))

    def _python_condition_references(self, node: Any, scope: list[CodeSymbol]) -> None:
        condition = _field(node, "condition") or _first_named_child(node)
        owner = _call_owner(scope) or _parent(self.module, scope)
        for name, ref_node in _read_reference_names(self.source, condition):
            self.references.append(_reference(self.artifact, owner, name, _line(ref_node), _column(ref_node), "read"))

    def _python_for_references(self, node: Any, scope: list[CodeSymbol]) -> None:
        iterable = _field(node, "right") or _field(node, "iterable")
        owner = _call_owner(scope) or _parent(self.module, scope)
        for name, ref_node in _read_reference_names(self.source, iterable):
            self.references.append(_reference(self.artifact, owner, name, _line(ref_node), _column(ref_node), "read"))

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


def _is_docstring_statement(node: Any) -> bool:
    return _docstring_text(b"", node, decode=False) is not None


def _docstring_text(source: bytes, node: Any, *, decode: bool = True) -> str | None:
    if str(getattr(node, "type", "")) != "expression_statement":
        return None
    child = _first_named_child(node)
    if child is None or str(getattr(child, "type", "")) != "string":
        return None
    return _string_value(source, child) if decode else ""


def _string_value(source: bytes, node: Any) -> str:
    parts = [_node_text(source, child) for child in _named_children(node) if str(getattr(child, "type", "")) == "string_content"]
    if parts:
        return "".join(parts)
    raw = _node_text(source, node).strip()
    if len(raw) >= 2 and raw[0] in {"'", '"'}:
        quote = raw[:3] if raw.startswith(("'''", '"""')) else raw[0]
        return raw.removeprefix(quote).removesuffix(quote)
    return raw


def _decorator_text(source: bytes, node: Any) -> str:
    return _node_text(source, node).strip().removeprefix("@").strip()


def _relative_import_level(raw: str) -> int:
    stripped = raw.strip()
    if not stripped.startswith("from "):
        return 0
    tail = stripped[5:]
    return len(tail) - len(tail.lstrip("."))


def _python_import_names(source: bytes, node: Any) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    module_node = _field(node, "module_name")
    for child in _children(node):
        child_type = str(getattr(child, "type", ""))
        if child_type == "aliased_import":
            out.append((_node_text(source, _field(child, "name")).strip(), _node_text(source, _field(child, "alias")).strip() or None))
        elif child_type in {"dotted_name", "identifier"} and not _same_node(child, module_node):
            out.append((_node_text(source, child).strip(), None))
        elif child_type == "wildcard_import":
            out.append(("*", None))
    return [(name, alias) for name, alias in out if name]


def _same_node(left: Any | None, right: Any | None) -> bool:
    if left is None or right is None:
        return False
    return int(getattr(left, "start_byte", -1)) == int(getattr(right, "start_byte", -2)) and int(getattr(left, "end_byte", -1)) == int(getattr(right, "end_byte", -2))


def _python_bases(source: bytes, node: Any) -> list[str]:
    superclasses = _field(node, "superclasses")
    if superclasses is None:
        return []
    return [_node_text(source, child).strip() for child in _named_children(superclasses) if _node_text(source, child).strip()]


def _assignment_names(source: bytes, node: Any | None) -> list[tuple[str, Any]]:
    if node is None:
        return []
    node_type = str(getattr(node, "type", ""))
    if node_type == "identifier":
        return [(_node_text(source, node), node)]
    if node_type == "attribute":
        name = _reference_name(source, node)
        return [(name, node)] if name else []
    if node_type == "subscript":
        target = _field(node, "value") or _field(node, "object") or _first_named_child(node)
        return _assignment_names(source, target)
    names: list[tuple[str, Any]] = []
    for child in _named_children(node):
        names.extend(_assignment_names(source, child))
    return names


def _read_reference_names(source: bytes, node: Any | None) -> list[tuple[str, Any]]:
    if node is None:
        return []
    node_type = str(getattr(node, "type", ""))
    if node_type == "identifier":
        return [(_node_text(source, node), node)]
    if node_type == "attribute":
        name = _reference_name(source, node)
        return [(name, node)] if name else []
    names: list[tuple[str, Any]] = []
    for child in _named_children(node):
        if str(getattr(child, "type", "")) in {"argument_list", "parameters"}:
            continue
        names.extend(_read_reference_names(source, child))
    return names


def _python_parameter_usage_names(source: bytes, node: Any | None) -> list[tuple[str, Any]]:
    names: list[tuple[str, Any]] = []
    for child in _named_children(node):
        child_type = str(getattr(child, "type", ""))
        if child_type in {"default_parameter", "typed_default_parameter", "keyword_separator"}:
            for field_name in ("type", "value"):
                names.extend(_read_reference_names(source, _field(child, field_name)))
            continue
        if child_type in {"typed_parameter", "list_splat_pattern", "dictionary_splat_pattern"}:
            names.extend(_read_reference_names(source, _field(child, "type")))
            continue
        names.extend(_python_parameter_usage_names(source, child))
    return names


def _python_exception_usage_names(source: bytes, node: Any | None) -> list[tuple[str, Any]]:
    if node is None:
        return []
    target = _first_named_child(node)
    if target is None:
        return []
    if str(getattr(target, "type", "")) == "block":
        return []
    if str(getattr(target, "type", "")) == "as_pattern":
        exception_node = _first_named_child(target)
        return _read_reference_names(source, exception_node)
    return _read_reference_names(source, target)


def _reference_name(source: bytes, node: Any | None) -> str | None:
    if node is None:
        return None
    node_type = str(getattr(node, "type", ""))
    if node_type in {"identifier", "property_identifier", "type_identifier"}:
        return _node_text(source, node)
    if node_type == "attribute":
        obj = _reference_name(source, _field(node, "object"))
        attr = _reference_name(source, _field(node, "attribute"))
        return f"{obj}.{attr}" if obj and attr else attr or obj
    if node_type in {"call", "call_expression"}:
        return _reference_name(source, _field(node, "function"))
    return _reference_name(source, _first_named_child(node))


def _param_annotations(source: bytes, node: Any | None) -> list[str]:
    annotations: list[str] = []
    for child in _named_children(node):
        annotations.extend(_type_annotations(source, child))
    return annotations


def _type_annotations(source: bytes, node: Any | None) -> list[str]:
    if node is None:
        return []
    annotations: list[str] = []
    annotation = _clean_type_text(_node_text(source, _field(node, "type")))
    if annotation:
        annotations.append(annotation)
    for child in _named_children(node):
        annotations.extend(_type_annotations(source, child))
    return annotations


def _is_interface_class(name: str, bases: list[str]) -> bool:
    tails = {base.split(".")[-1] for base in bases}
    return name.endswith("Protocol") or "Protocol" in tails


def _is_schema_class(bases: list[str], decorators: list[str]) -> bool:
    tails = {base.split(".")[-1] for base in bases}
    decorator_tails = {decorator.split("(", 1)[0].split(".")[-1] for decorator in decorators}
    return bool(tails & {"BaseModel", "TypedDict", "Schema"}) or "dataclass" in decorator_tails
