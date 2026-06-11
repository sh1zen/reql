"""Tree-sitter backed code parser for recognized project languages."""
from __future__ import annotations

from functools import lru_cache
from importlib import import_module
from pathlib import Path
from typing import Any

from ..artifacts.models import SourceArtifact
from ..domain.ids import stable_id
from .ast_profiles import AstProfile, profile_for
from .languages import SUPPORTED_AST_LANGUAGES
from .models import CodeCall, CodeImport, CodeModule, CodeParseResult, CodeReference, CodeSymbol, CodeText, CodeTextKind, SymbolKind
from .tree_sitter_languages import display_language_for_key, language_key, load_tree_sitter_language, tree_sitter_language_key


class TreeSitterCodeParser:
    parser_name = "tree_sitter"
    parser_version = "tree-sitter-code-v10"

    def supports(self, language: str) -> bool:
        return language_key(language) in SUPPORTED_AST_LANGUAGES

    def parse_artifact(self, artifact: SourceArtifact, text: str) -> CodeParseResult:
        language = _language(artifact)
        parser_language = tree_sitter_language_key(artifact.path, language)
        source = text.removeprefix("\ufeff").encode("utf-8", errors="replace")
        parser = _parser_for(parser_language)
        tree = parser.parse(source)
        root = tree.root_node
        if bool(getattr(root, "has_error", False)):
            return _empty_result(artifact, language, f"Tree-sitter syntax error in {artifact.relative_path}")
        return _TreeSitterExtractor(artifact, source, language, parser_language).extract(root)


class _TreeSitterExtractor:
    def __init__(self, artifact: SourceArtifact, source: bytes, language: str, language_key_: str) -> None:
        self.artifact = artifact
        self.source = source
        self.language = language
        self.language_key = language_key_
        self.profile = profile_for(language_key_)
        self.module = CodeModule(
            id=stable_id("module", artifact.id),
            artifact_id=artifact.id,
            name=_module_name(artifact.relative_path),
            path=artifact.relative_path,
            language=language,
            metadata={"tree_sitter": True},
        )
        self.symbols: list[CodeSymbol] = []
        self.imports: list[CodeImport] = []
        self.calls: list[CodeCall] = []
        self.references: list[CodeReference] = []
        self.comments: list[CodeText] = []
        self.docstrings: list[CodeText] = []

    def extract(self, root: Any) -> CodeParseResult:
        if self.language_key == "python":
            self._extract_python_module_docstring(root)
            self._walk_python(root, [])
        elif self.language_key in {"javascript", "typescript", "tsx"}:
            self._walk_js_ts(root, [])
        else:
            self._walk_generic(root, [])
        classes = [symbol for symbol in self.symbols if symbol.kind == "class"]
        functions = [symbol for symbol in self.symbols if symbol.kind in {"function", "async_function"}]
        methods = [symbol for symbol in self.symbols if symbol.kind in {"method", "async_method"}]
        return CodeParseResult(
            module=self.module,
            symbols=self.symbols,
            imports=self.imports,
            calls=self.calls,
            references=self.references,
            classes=classes,
            functions=functions,
            methods=methods,
            comments=self.comments,
            docstrings=self.docstrings,
            errors=[],
            parser_name=TreeSitterCodeParser.parser_name,
            parser_version=TreeSitterCodeParser.parser_version,
        )

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

    def _walk_js_ts(self, node: Any, scope: list[CodeSymbol]) -> None:
        node_type = str(getattr(node, "type", ""))
        if node_type == "comment":
            self.comments.append(_text(self.artifact, _owner(scope), _node_text(self.source, node), _line(node), _end_line(node), "comment"))
            return
        if node_type in {"import_statement", "export_statement"}:
            self._js_ts_import(node)
        if node_type in {"function_declaration", "generator_function_declaration"}:
            symbol = self._js_ts_function_symbol(node, scope)
            if symbol:
                self.symbols.append(symbol)
                self._walk_js_ts_children(node, [*scope, symbol])
                return
        if node_type == "class_declaration":
            symbol = self._js_ts_class_symbol(node, scope)
            if symbol:
                self.symbols.append(symbol)
                self._walk_js_ts_children(node, [*scope, symbol])
                return
        if node_type in {"method_definition", "method_signature"}:
            symbol = self._js_ts_method_symbol(node, scope)
            if symbol:
                self.symbols.append(symbol)
                self._walk_js_ts_children(node, [*scope, symbol])
                return
        if node_type in {"variable_declarator", "public_field_definition"}:
            symbol = self._js_ts_variable_function_symbol(node, scope)
            if symbol:
                self.symbols.append(symbol)
                self._walk_js_ts_children(node, [*scope, symbol])
                return
        if node_type == "call_expression":
            self._call(node, scope, function_field="function")
        self._walk_js_ts_children(node, scope)

    def _walk_js_ts_children(self, node: Any, scope: list[CodeSymbol]) -> None:
        for child in _children(node):
            self._walk_js_ts(child, scope)

    def _walk_generic(self, node: Any, scope: list[CodeSymbol]) -> None:
        node_type = str(getattr(node, "type", ""))
        if self.profile.is_comment_node(node_type):
            self.comments.append(_text(self.artifact, _owner(scope), _node_text(self.source, node), _line(node), _end_line(node), "comment"))
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
        if self.profile.is_call_node(node_type):
            self._call(node, scope, function_field="function")
        self._walk_generic_children(node, scope)

    def _walk_generic_children(self, node: Any, scope: list[CodeSymbol]) -> None:
        for child in _children(node):
            self._walk_generic(child, scope)

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

    def _js_ts_import(self, node: Any) -> None:
        source_node = _field(node, "source")
        module = _string_literal(_node_text(self.source, source_node)) if source_node is not None else None
        if module is None and str(getattr(node, "type", "")) == "import_statement":
            module = _last_quoted(_node_text(self.source, node))
        if module is None:
            return
        self.imports.append(_import(self.artifact, module, None, None, _line(node), _node_text(self.source, node).strip()))

    def _js_ts_class_symbol(self, node: Any, scope: list[CodeSymbol]) -> CodeSymbol | None:
        name_node = _field(node, "name")
        if name_node is None:
            return None
        base = _js_ts_class_base(self.source, node)
        return _symbol(self.artifact, "class", _parent(self.module, scope), _node_text(self.source, name_node), _line(node), _end_line(node), bases=[base] if base else [])

    def _js_ts_function_symbol(self, node: Any, scope: list[CodeSymbol]) -> CodeSymbol | None:
        name_node = _field(node, "name")
        if name_node is None:
            return None
        kind: SymbolKind = "async_function" if _has_direct_child(node, "async") else "function"
        return _symbol(self.artifact, kind, _parent(self.module, scope), _node_text(self.source, name_node), _line(node), _end_line(node), args=_params(self.source, _field(node, "parameters")), returns=_clean_type_text(_node_text(self.source, _field(node, "return_type"))))

    def _js_ts_method_symbol(self, node: Any, scope: list[CodeSymbol]) -> CodeSymbol | None:
        name_node = _field(node, "name")
        if name_node is None:
            return None
        in_class = any(symbol.kind == "class" for symbol in scope)
        kind: SymbolKind = "async_method" if _has_direct_child(node, "async") else "method" if in_class else "function"
        return _symbol(self.artifact, kind, _parent(self.module, scope), _node_text(self.source, name_node), _line(node), _end_line(node), args=_params(self.source, _field(node, "parameters")), returns=_clean_type_text(_node_text(self.source, _field(node, "return_type"))))

    def _js_ts_variable_function_symbol(self, node: Any, scope: list[CodeSymbol]) -> CodeSymbol | None:
        value = _field(node, "value")
        if value is None or str(getattr(value, "type", "")) not in {"arrow_function", "function", "function_expression"}:
            return None
        name_node = _field(node, "name")
        if name_node is None:
            return None
        kind: SymbolKind = "async_function" if _has_direct_child(value, "async") else "function"
        return _symbol(self.artifact, kind, _parent(self.module, scope), _node_text(self.source, name_node), _line(node), _end_line(value), args=_params(self.source, _field(value, "parameters")), returns=_clean_type_text(_node_text(self.source, _field(value, "return_type"))))

    def _generic_symbol(self, node: Any, scope: list[CodeSymbol], kind: SymbolKind) -> CodeSymbol | None:
        name_node = _declaration_name_node(node, self.profile)
        if name_node is None:
            return None
        name = _node_text(self.source, name_node).strip()
        if not _valid_symbol_name(name):
            return None
        bases = _generic_bases(self.source, node, self.profile)
        return _symbol(
            self.artifact,
            kind,
            _parent(self.module, scope),
            name,
            _line(node),
            _end_line(node),
            bases=bases,
            args=_generic_params(self.source, node, self.profile),
            returns=_clean_type_text(_node_text(self.source, _first_field(node, self.profile.return_fields))),
        )

    def _generic_variable_function_symbol(self, node: Any, scope: list[CodeSymbol]) -> CodeSymbol | None:
        value = _field(node, "value")
        if value is None or "function" not in str(getattr(value, "type", "")):
            return None
        name_node = _declaration_name_node(node, self.profile)
        if name_node is None:
            return None
        name = _node_text(self.source, name_node).strip()
        if not _valid_symbol_name(name):
            return None
        return _symbol(
            self.artifact,
            "method" if any(symbol.kind == "class" for symbol in scope) else "function",
            _parent(self.module, scope),
            name,
            _line(node),
            _end_line(value),
            args=_generic_params(self.source, value, self.profile),
            returns=_clean_type_text(_node_text(self.source, _first_field(value, self.profile.return_fields))),
        )

    def _generic_import(self, node: Any) -> None:
        raw = _node_text(self.source, node).strip()
        module = _generic_import_target(self.source, node, self.profile)
        if module is None:
            return
        self.imports.append(_import(self.artifact, module, None, None, _line(node), raw))

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


@lru_cache(maxsize=64)
def _parser_for(language: str) -> Any:
    tree_sitter = import_module("tree_sitter")
    language_obj = load_tree_sitter_language(tree_sitter, language)
    parser_cls = getattr(tree_sitter, "Parser")
    try:
        return parser_cls(language_obj)
    except TypeError:
        parser = parser_cls()
        try:
            parser.language = language_obj
        except AttributeError:
            parser.set_language(language_obj)
        return parser


def _language(artifact: SourceArtifact) -> str:
    key = language_key(artifact.language or Path(artifact.path).suffix)
    return display_language_for_key(key, artifact.language)


def _empty_result(artifact: SourceArtifact, language: str, error: str) -> CodeParseResult:
    module = CodeModule(id=stable_id("module", artifact.id), artifact_id=artifact.id, name=_module_name(artifact.relative_path), path=artifact.relative_path, language=language, metadata={"tree_sitter": True})
    return CodeParseResult(module=module, symbols=[], imports=[], calls=[], references=[], classes=[], functions=[], methods=[], comments=[], docstrings=[], errors=[error], parser_name=TreeSitterCodeParser.parser_name, parser_version=TreeSitterCodeParser.parser_version)


def _symbol(
    artifact: SourceArtifact,
    kind: SymbolKind,
    parent: str,
    name: str,
    line: int,
    end_line: int | None,
    *,
    decorators: list[str] | None = None,
    args: list[str] | None = None,
    bases: list[str] | None = None,
    returns: str | None = None,
) -> CodeSymbol:
    qualified = f"{parent}.{name}" if parent else name
    return CodeSymbol(id=stable_id("code-symbol", artifact.id, kind, qualified), artifact_id=artifact.id, kind=kind, name=name, qualified_name=qualified, start_line=line, end_line=end_line, parent_qualified_name=parent, decorators=decorators or [], bases=bases or [], args=args or [], returns=returns)


def _import(artifact: SourceArtifact, module: str | None, name: str | None, alias: str | None, line: int, raw: str, *, level: int = 0) -> CodeImport:
    return CodeImport(id=stable_id("import", artifact.id, line, module or "", name or "", alias or "", level), artifact_id=artifact.id, module=module, name=name, alias=alias, level=level, line=line, raw=raw)


def _text(artifact: SourceArtifact, owner: str | None, text: str, line: int, end_line: int | None, kind: CodeTextKind) -> CodeText:
    return CodeText(id=stable_id(kind, artifact.id, owner or "", line, text), artifact_id=artifact.id, owner=owner, text=text, start_line=line, end_line=end_line, kind=kind)


def _reference(artifact: SourceArtifact, owner: str | None, name: str, line: int, column: int, access: str) -> CodeReference:
    return CodeReference(id=stable_id("reference", artifact.id, owner or "", name, line, column, access), artifact_id=artifact.id, owner=owner, name=name, line=line, column=column, access=access)  # type: ignore[arg-type]


def _children(node: Any | None) -> list[Any]:
    if node is None:
        return []
    children = getattr(node, "children", None)
    if children is not None:
        return list(children)
    return list(getattr(node, "named_children", None) or [])


def _named_children(node: Any | None) -> list[Any]:
    if node is None:
        return []
    return list(getattr(node, "named_children", None) or [])


def _field(node: Any | None, name: str) -> Any | None:
    if node is None:
        return None
    child_by_field_name = getattr(node, "child_by_field_name", None)
    if child_by_field_name is None:
        return None
    try:
        return child_by_field_name(name)
    except Exception:
        return None


def _node_text(source: bytes, node: Any | None) -> str:
    if node is None:
        return ""
    return source[int(getattr(node, "start_byte", 0)) : int(getattr(node, "end_byte", 0))].decode("utf-8", errors="replace")


def _line(node: Any) -> int:
    point = getattr(node, "start_point", (0, 0))
    return int(point[0] if isinstance(point, tuple) else point.row) + 1


def _end_line(node: Any) -> int:
    point = getattr(node, "end_point", (0, 0))
    return int(point[0] if isinstance(point, tuple) else point.row) + 1


def _column(node: Any) -> int:
    point = getattr(node, "start_point", (0, 0))
    return int(point[1] if isinstance(point, tuple) else point.column)


def _parent(module: CodeModule, scope: list[CodeSymbol]) -> str:
    return scope[-1].qualified_name if scope else module.name


def _owner(scope: list[CodeSymbol]) -> str | None:
    return scope[-1].qualified_name if scope else None


def _call_owner(scope: list[CodeSymbol]) -> str | None:
    for symbol in reversed(scope):
        if symbol.kind in {"function", "async_function", "method", "async_method"}:
            return symbol.qualified_name
    return None


def _first_named_child(node: Any | None) -> Any | None:
    children = _named_children(node)
    return children[0] if children else None


def _last_named_child(node: Any | None) -> Any | None:
    children = _named_children(node)
    return children[-1] if children else None


def _first_child_of_type(node: Any | None, node_types: set[str]) -> Any | None:
    for child in _children(node):
        if str(getattr(child, "type", "")) in node_types:
            return child
    return None


def _has_direct_child(node: Any, child_type: str) -> bool:
    return any(str(getattr(child, "type", "")) == child_type for child in _children(node))


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


def _clean_type_text(raw: str) -> str | None:
    value = raw.strip()
    if value.startswith("->"):
        value = value[2:].strip()
    if value.startswith(":"):
        value = value[1:].strip()
    return value or None


def _params(source: bytes, node: Any | None) -> list[str]:
    names: list[str] = []
    for child in _named_children(node):
        name = _param_name(source, child)
        if name:
            names.append(name)
    return names


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


def _param_name(source: bytes, node: Any) -> str | None:
    node_type = str(getattr(node, "type", ""))
    if node_type in {"identifier", "property_identifier"}:
        return _node_text(source, node)
    pattern = _field(node, "pattern")
    if pattern is not None:
        return _param_name(source, pattern)
    for child in _named_children(node):
        name = _param_name(source, child)
        if name:
            return name
    return None


def _call_target(source: bytes, node: Any | None) -> str | None:
    if node is None:
        return None
    raw = _node_text(source, node).strip()
    raw = raw.replace("?.", ".")
    raw = "".join(raw.split())
    return raw or None


def _string_literal(raw: str) -> str | None:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] in {"'", '"', "`"} and raw[-1] == raw[0]:
        return raw[1:-1]
    return raw or None


def _last_quoted(raw: str) -> str | None:
    for quote in ("'", '"'):
        end = raw.rfind(quote)
        if end <= 0:
            continue
        start = raw.rfind(quote, 0, end)
        if start >= 0:
            return raw[start + 1 : end]
    return None


def _js_ts_class_base(source: bytes, node: Any) -> str | None:
    superclass = _field(node, "superclass")
    if superclass is not None:
        return _node_text(source, superclass).strip() or None
    for child in _named_children(node):
        if str(getattr(child, "type", "")) == "class_heritage":
            value = _node_text(source, child).replace("extends", "", 1).strip()
            return value or None
    return None


def _is_interface_class(name: str, bases: list[str]) -> bool:
    tails = {base.split(".")[-1] for base in bases}
    return name.endswith("Protocol") or "Protocol" in tails


def _is_schema_class(bases: list[str], decorators: list[str]) -> bool:
    tails = {base.split(".")[-1] for base in bases}
    decorator_tails = {decorator.split("(", 1)[0].split(".")[-1] for decorator in decorators}
    return bool(tails & {"BaseModel", "TypedDict", "Schema"}) or "dataclass" in decorator_tails


_SYMBOL_NAME_STOP_WORDS = {
    "class",
    "def",
    "enum",
    "fn",
    "func",
    "function",
    "import",
    "include",
    "interface",
    "package",
    "struct",
    "trait",
    "type",
    "use",
}


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


def _valid_symbol_name(name: str) -> bool:
    value = name.strip()
    if not value or value.casefold() in _SYMBOL_NAME_STOP_WORDS:
        return False
    return any(char.isalpha() or char == "_" for char in value)


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


def _module_name(relative_path: str) -> str:
    path = Path(relative_path)
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or path.stem
