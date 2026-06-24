"""Solidity Tree-sitter extraction."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from ..base import (
    TreeSitterExtractorBase,
    _call_owner,
    _call_target,
    _children,
    _column,
    _end_line,
    _field,
    _first_child_of_type,
    _import,
    _last_quoted,
    _line,
    _named_children,
    _node_text,
    _owner,
    _parent,
    _reference,
    _string_literal,
    _symbol,
    _text,
    _valid_symbol_name,
)
from ...artifacts.models import SourceArtifact
from ...domain.ids import stable_id
from ..models import CodeCall, CodeSymbol, SymbolKind


class SolidityTreeSitterExtractor(TreeSitterExtractorBase):
    language_key = "solidity"
    tree_sitter_module = "tree_sitter_solidity"

    def __init__(self, artifact: SourceArtifact, source: bytes, language: str, language_key: str) -> None:
        super().__init__(artifact, source, language, language_key)
        self.project_root = _project_root_for_artifact(artifact)
        self.remappings = _foundry_remappings(self.project_root)

    def _walk_root(self, root: Any) -> None:
        self._walk_solidity(root, [])

    def _walk_solidity(self, node: Any, scope: list[CodeSymbol]) -> None:
        node_type = str(getattr(node, "type", ""))
        if node_type == "comment" or node_type.endswith("_comment"):
            self.comments.append(_text(self.artifact, _owner(scope), _node_text(self.source, node), _line(node), _end_line(node), "comment"))
            return
        if node_type == "import_directive":
            self._solidity_import(node)
            return
        if node_type in {"contract_declaration", "interface_declaration", "library_declaration"}:
            symbol = self._solidity_container_symbol(node, scope)
            if symbol is not None:
                self.symbols.append(symbol)
                self._walk_solidity_children(node, [*scope, symbol])
                return
        if node_type in {"struct_declaration", "enum_declaration", "event_definition"}:
            symbol = self._solidity_named_symbol(node, scope)
            if symbol is not None:
                self.symbols.append(symbol)
            return
        if node_type in {"function_definition", "constructor_definition", "modifier_definition", "fallback_receive_definition"}:
            symbol = self._solidity_callable_symbol(node, scope)
            if symbol is not None:
                self.symbols.append(symbol)
                self._walk_solidity_children(node, [*scope, symbol])
                return
        if node_type == "state_variable_declaration":
            self._solidity_state_variable(node, scope)
        if node_type == "using_directive":
            self._solidity_using_directive(node, scope)
        if node_type == "modifier_invocation":
            self._solidity_modifier_invocation(node, scope)
        if node_type == "emit_statement":
            self._solidity_emit(node, scope)
            self._walk_solidity_children(node, scope)
            return
        if node_type == "call_expression":
            self._solidity_call(node, scope)
        if node_type == "variable_declaration":
            self._solidity_type_reference(node, scope)
        self._walk_solidity_children(node, scope)

    def _walk_solidity_children(self, node: Any, scope: list[CodeSymbol]) -> None:
        for child in _children(node):
            self._walk_solidity(child, scope)

    def _solidity_container_symbol(self, node: Any, scope: list[CodeSymbol]) -> CodeSymbol | None:
        name_node = _first_direct_child_of_type(node, {"identifier"})
        if name_node is None:
            return None
        node_type = str(getattr(node, "type", ""))
        name = _node_text(self.source, name_node).strip()
        bases = [_solidity_inheritance_name(self.source, child) for child in _children(node) if str(getattr(child, "type", "")) == "inheritance_specifier"]
        solidity_kind = {"contract_declaration": "contract", "interface_declaration": "interface", "library_declaration": "library"}[node_type]
        symbol = _symbol(self.artifact, "class", _parent(self.module, scope), name, _line(node), _end_line(node), bases=[base for base in bases if base])
        symbol.metadata.update(
            {
                "language": "solidity",
                "solidity_kind": solidity_kind,
                "is_interface": solidity_kind == "interface",
            }
        )
        return symbol

    def _solidity_named_symbol(self, node: Any, scope: list[CodeSymbol]) -> CodeSymbol | None:
        name_node = _first_direct_child_of_type(node, {"identifier"})
        if name_node is None:
            return None
        node_type = str(getattr(node, "type", ""))
        solidity_kind = {
            "struct_declaration": "struct",
            "enum_declaration": "enum",
            "event_definition": "event",
        }[node_type]
        symbol = _symbol(self.artifact, "class", _parent(self.module, scope), _node_text(self.source, name_node), _line(node), _end_line(node))
        symbol.metadata.update({"language": "solidity", "solidity_kind": solidity_kind})
        return symbol

    def _solidity_callable_symbol(self, node: Any, scope: list[CodeSymbol]) -> CodeSymbol | None:
        node_type = str(getattr(node, "type", ""))
        name_node = _first_direct_child_of_type(node, {"identifier"})
        name = _node_text(self.source, name_node).strip() if name_node is not None else ""
        solidity_kind = "function"
        if node_type == "constructor_definition":
            name = "constructor"
            solidity_kind = "constructor"
        elif node_type == "modifier_definition":
            solidity_kind = "modifier"
        elif node_type == "fallback_receive_definition":
            raw = _node_text(self.source, node).lstrip()
            name = "receive" if raw.startswith("receive") else "fallback"
            solidity_kind = name
        if not name:
            return None
        kind: SymbolKind = "method" if scope else "function"
        symbol = _symbol(
            self.artifact,
            kind,
            _parent(self.module, scope),
            name,
            _line(node),
            _end_line(node),
            decorators=_solidity_modifier_names(self.source, node),
            args=_solidity_parameter_names(self.source, node),
            returns=_solidity_return_text(self.source, node),
        )
        symbol.metadata.update(
            {
                "language": "solidity",
                "solidity_kind": solidity_kind,
                "visibility": _solidity_visibility(self.source, node),
                "state_mutability": _solidity_state_mutability(self.source, node),
            }
        )
        return symbol

    def _solidity_state_variable(self, node: Any, scope: list[CodeSymbol]) -> None:
        declaration = next((child for child in _children(node) if str(getattr(child, "type", "")) == "variable_declaration"), node)
        name_node = _last_direct_child_of_type(declaration, {"identifier"})
        if name_node is None:
            return
        name = _node_text(self.source, name_node).strip()
        if not _valid_symbol_name(name):
            return
        type_name = _solidity_type_name_text(self.source, declaration)
        symbol = _symbol(self.artifact, "variable", _parent(self.module, scope), name, _line(node), _end_line(node), returns=type_name)
        symbol.metadata.update({"language": "solidity", "solidity_kind": "state_variable", "type": type_name})
        self.symbols.append(symbol)
        if type_name and not _solidity_builtin(type_name):
            self.references.append(_reference(self.artifact, _parent(self.module, scope), type_name, _line(node), _column(node), "read"))

    def _solidity_import(self, node: Any) -> None:
        raw = _node_text(self.source, node).strip()
        source_path = _solidity_import_source(self.source, node)
        if source_path is None:
            return
        resolved = _resolve_solidity_import(self.artifact, source_path, self.project_root, self.remappings)
        import_names = _solidity_import_names(self.source, node)
        if not import_names:
            self.imports.append(
                _import(
                    self.artifact,
                    _solidity_dependency_name(source_path, resolved),
                    None,
                    None,
                    _line(node),
                    raw,
                    metadata=_solidity_import_metadata(source_path, resolved),
                )
            )
            return
        for name, alias in import_names:
            self.imports.append(
                _import(
                    self.artifact,
                    _solidity_dependency_name(source_path, resolved),
                    name,
                    alias,
                    _line(node),
                    raw,
                    metadata=_solidity_import_metadata(source_path, resolved),
                )
            )

    def _solidity_using_directive(self, node: Any, scope: list[CodeSymbol]) -> None:
        library = _solidity_using_library(self.source, node)
        if not library or _solidity_builtin(library):
            return
        caller = _parent(self.module, scope)
        self.calls.append(
            CodeCall(
                id=stable_id("call", self.artifact.id, caller, "using", library, _line(node), _column(node)),
                artifact_id=self.artifact.id,
                caller=caller,
                target=library,
                line=_line(node),
                column=_column(node),
                metadata={"kind": "using_directive"},
            )
        )

    def _solidity_modifier_invocation(self, node: Any, scope: list[CodeSymbol]) -> None:
        target = _node_text(self.source, _first_direct_child_of_type(node, {"identifier"})).strip()
        if not target or _solidity_builtin(target):
            return
        caller = _call_owner(scope) or _parent(self.module, scope)
        self.calls.append(
            CodeCall(
                id=stable_id("call", self.artifact.id, caller, target, _line(node), _column(node)),
                artifact_id=self.artifact.id,
                caller=caller,
                target=target,
                line=_line(node),
                column=_column(node),
                metadata={"kind": "modifier_invocation"},
            )
        )

    def _solidity_emit(self, node: Any, scope: list[CodeSymbol]) -> None:
        target = _solidity_emit_target(self.source, node)
        if not target:
            return
        caller = _call_owner(scope) or _parent(self.module, scope)
        self.calls.append(
            CodeCall(
                id=stable_id("call", self.artifact.id, caller, "emit", target, _line(node), _column(node)),
                artifact_id=self.artifact.id,
                caller=caller,
                target=target,
                line=_line(node),
                column=_column(node),
                metadata={"kind": "emit"},
            )
        )

    def _solidity_call(self, node: Any, scope: list[CodeSymbol]) -> None:
        target, kind = _solidity_call_target(self.source, node)
        if not target or _solidity_builtin(target):
            return
        caller = _call_owner(scope) or _parent(self.module, scope)
        self.calls.append(
            CodeCall(
                id=stable_id("call", self.artifact.id, caller, target, _line(node), _column(node), kind),
                artifact_id=self.artifact.id,
                caller=caller,
                target=target,
                line=_line(node),
                column=_column(node),
                metadata={"kind": kind},
            )
        )

    def _solidity_type_reference(self, node: Any, scope: list[CodeSymbol]) -> None:
        type_name = _solidity_type_name_text(self.source, node)
        if not type_name or _solidity_builtin(type_name):
            return
        self.references.append(_reference(self.artifact, _parent(self.module, scope), type_name, _line(node), _column(node), "read"))


def _first_direct_child_of_type(node: Any | None, node_types: set[str]) -> Any | None:
    for child in _children(node):
        if str(getattr(child, "type", "")) in node_types:
            return child
    return None


def _last_direct_child_of_type(node: Any | None, node_types: set[str]) -> Any | None:
    for child in reversed(_children(node)):
        if str(getattr(child, "type", "")) in node_types:
            return child
    return None


def _project_root_for_artifact(artifact: SourceArtifact) -> Path:
    root = Path(artifact.path).expanduser().resolve(strict=False)
    for _ in Path(artifact.relative_path).parts:
        root = root.parent
    return root


def _foundry_remappings(project_root: Path) -> list[tuple[str, str]]:
    remappings: list[tuple[str, str]] = []
    remappings_txt = project_root / "remappings.txt"
    if remappings_txt.exists():
        try:
            remappings.extend(_parse_foundry_remapping_lines(remappings_txt.read_text(encoding="utf-8", errors="replace").splitlines()))
        except OSError:
            pass
    foundry_toml = project_root / "foundry.toml"
    if foundry_toml.exists():
        try:
            text = foundry_toml.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        remappings.extend(_parse_foundry_toml_remappings(text))
    deduped: list[tuple[str, str]] = []
    for item in remappings:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _parse_foundry_remapping_lines(lines: list[str]) -> list[tuple[str, str]]:
    remappings: list[tuple[str, str]] = []
    for line in lines:
        value = line.split("#", 1)[0].strip()
        if not value or "=" not in value:
            continue
        prefix, target = value.split("=", 1)
        prefix = prefix.strip()
        target = target.strip()
        if prefix and target:
            remappings.append((prefix, target))
    return remappings


def _parse_foundry_toml_remappings(text: str) -> list[tuple[str, str]]:
    remappings: list[tuple[str, str]] = []
    for match in re.finditer(r"(?m)^\s*remappings\s*=\s*\[(.*?)\]", text, flags=re.DOTALL):
        raw_items = re.findall(r"['\"]([^'\"]+=.+?)['\"]", match.group(1))
        remappings.extend(_parse_foundry_remapping_lines(raw_items))
    for match in re.finditer(r"(?m)^\s*remappings\s*=\s*['\"]([^'\"]+=.+?)['\"]", text):
        remappings.extend(_parse_foundry_remapping_lines([match.group(1)]))
    return remappings


def _resolve_solidity_import(
    artifact: SourceArtifact,
    source_path: str,
    project_root: Path,
    remappings: list[tuple[str, str]],
) -> str | None:
    candidates: list[Path] = []
    if source_path.startswith("."):
        candidates.append((Path(artifact.path).parent / source_path).resolve(strict=False))
    for prefix, target in sorted(remappings, key=lambda item: len(item[0]), reverse=True):
        if source_path.startswith(prefix):
            suffix = source_path[len(prefix) :]
            candidates.append((project_root / target / suffix).resolve(strict=False))
    for candidate in candidates:
        if candidate.exists():
            try:
                return candidate.relative_to(project_root).as_posix()
            except ValueError:
                return candidate.as_posix()
    return None


def _solidity_import_source(source: bytes, node: Any) -> str | None:
    for child in _children(node):
        if str(getattr(child, "type", "")) == "string":
            return _string_literal(_node_text(source, child))
    return _last_quoted(_node_text(source, node))


def _solidity_import_names(source: bytes, node: Any) -> list[tuple[str, str | None]]:
    raw = _node_text(source, node)
    braced = re.search(r"\{(?P<body>.*?)\}", raw)
    if braced:
        names: list[tuple[str, str | None]] = []
        for item in braced.group("body").split(","):
            value = item.strip()
            if not value:
                continue
            alias_match = re.match(r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+as\s+(?P<alias>[A-Za-z_][A-Za-z0-9_]*)$", value)
            if alias_match:
                names.append((alias_match.group("name"), alias_match.group("alias")))
            elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", value):
                names.append((value, None))
        return names
    alias_match = re.search(r"\bas\s+([A-Za-z_][A-Za-z0-9_]*)", raw)
    return [(alias_match.group(1), None)] if alias_match else []


def _solidity_dependency_name(source_path: str, resolved_relative_path: str | None) -> str:
    if resolved_relative_path:
        return resolved_relative_path
    stem = Path(source_path).stem
    return stem or source_path


def _solidity_import_metadata(source_path: str, resolved_relative_path: str | None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"language": "solidity", "import_path": source_path}
    if resolved_relative_path:
        metadata["resolved_relative_path"] = resolved_relative_path
    else:
        metadata["external_stub"] = True
    return metadata


def _solidity_inheritance_name(source: bytes, node: Any) -> str | None:
    identifier = _first_identifier(node)
    if identifier is not None:
        return _node_text(source, identifier).strip()
    value = _node_text(source, node).strip()
    return value.split("(", 1)[0].strip() or None


def _first_identifier(node: Any | None) -> Any | None:
    if node is None:
        return None
    if str(getattr(node, "type", "")) == "identifier":
        return node
    for child in _named_children(node):
        found = _first_identifier(child)
        if found is not None:
            return found
    return None


def _solidity_modifier_names(source: bytes, node: Any) -> list[str]:
    names: list[str] = []
    for child in _children(node):
        if str(getattr(child, "type", "")) != "modifier_invocation":
            continue
        identifier = _first_direct_child_of_type(child, {"identifier"})
        name = _node_text(source, identifier).strip()
        if name:
            names.append(name)
    return names


def _solidity_parameter_names(source: bytes, node: Any) -> list[str]:
    names: list[str] = []
    for child in _children(node):
        if str(getattr(child, "type", "")) != "parameter":
            continue
        identifier = _last_direct_child_of_type(child, {"identifier"})
        name = _node_text(source, identifier).strip()
        if name:
            names.append(name)
    return names


def _solidity_return_text(source: bytes, node: Any) -> str | None:
    returns = next((child for child in _children(node) if str(getattr(child, "type", "")) == "return_type_definition"), None)
    if returns is None:
        return None
    value = _node_text(source, returns).strip()
    return value or None


def _solidity_visibility(source: bytes, node: Any) -> str | None:
    visibility = next((child for child in _children(node) if str(getattr(child, "type", "")) == "visibility"), None)
    return _node_text(source, visibility).strip() or None


def _solidity_state_mutability(source: bytes, node: Any) -> str | None:
    mutability = next((child for child in _children(node) if str(getattr(child, "type", "")) == "state_mutability"), None)
    return _node_text(source, mutability).strip() or None


def _solidity_type_name_text(source: bytes, node: Any) -> str | None:
    type_node = _first_direct_child_of_type(node, {"type_name", "user_defined_type"})
    if type_node is None:
        type_node = _first_child_of_type(node, {"type_name", "user_defined_type"})
    value = _node_text(source, type_node).strip()
    return value.split("[", 1)[0].strip() or None


def _solidity_using_library(source: bytes, node: Any) -> str | None:
    type_alias = _first_direct_child_of_type(node, {"type_alias", "identifier"})
    identifier = _first_identifier(type_alias)
    return _node_text(source, identifier or type_alias).strip() or None


def _solidity_emit_target(source: bytes, node: Any) -> str | None:
    expression = _first_direct_child_of_type(node, {"expression"})
    identifier = _first_identifier(expression)
    return _node_text(source, identifier).strip() or None


def _solidity_call_target(source: bytes, node: Any) -> tuple[str | None, str]:
    expression = _first_direct_child_of_type(node, {"expression"})
    new_expression = _first_child_of_type(expression, {"new_expression"})
    if new_expression is not None:
        target = _solidity_type_name_text(source, new_expression)
        return target, "instantiates"
    target = _call_target(source, expression)
    return target, "call"


SOLIDITY_BUILTINS = {
    "abi",
    "addmod",
    "assert",
    "block",
    "blockhash",
    "bool",
    "bytes",
    "ecrecover",
    "false",
    "gasleft",
    "keccak256",
    "msg",
    "mulmod",
    "now",
    "payable",
    "revert",
    "require",
    "selfdestruct",
    "sha256",
    "string",
    "super",
    "this",
    "true",
    "tx",
    "type",
}


def _solidity_builtin(name: str | None) -> bool:
    if not name:
        return True
    value = name.strip()
    if value in SOLIDITY_BUILTINS:
        return True
    head = re.split(r"[\[.(]", value, maxsplit=1)[0]
    if head in SOLIDITY_BUILTINS:
        return True
    if re.fullmatch(r"u?int(8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?", head):
        return True
    if re.fullmatch(r"bytes([1-9]|[12][0-9]|3[0-2])?", head):
        return True
    return head in {"address", "mapping"}
