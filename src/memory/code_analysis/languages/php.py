"""PHP Tree-sitter extraction."""
from __future__ import annotations

import re
from typing import Any

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor
from ..base import _import, _reference


class PhpTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "php"
    tree_sitter_module = "tree_sitter_php"
    tree_sitter_function = "language_php"
    profile = AstProfile(
        name="php",
        languages=frozenset({"php"}),
        class_nodes=frozenset({"class_declaration", "enum_declaration", "interface_declaration", "trait_declaration"}),
        function_nodes=frozenset({"function_definition"}),
        method_nodes=frozenset({"method_declaration"}),
        variable_nodes=frozenset({"assignment_expression", "property_declaration"}),
        import_nodes=COMMON_IMPORTS
        | frozenset(
            {
                "include_expression",
                "include_once_expression",
                "namespace_definition",
                "namespace_use_declaration",
                "require_expression",
                "require_once_expression",
            }
        ),
        call_nodes=frozenset({"call_expression", "function_call_expression", "member_call_expression", "scoped_call_expression"}),
        assignment_nodes=frozenset({"assignment_expression"}),
        raise_nodes=frozenset({"throw_expression", "throw_statement"}),
        import_call_names=frozenset({"include", "include_once", "require", "require_once"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"array", "echo", "isset", "print", "require"}),
    )

    def _walk_root(self, root: Any) -> None:
        super()._walk_root(root)
        source = self.source.decode("utf-8", errors="replace")
        self._supplement_include_directives(source)
        self._supplement_compact_references(source)
        self._enrich_view_calls(source)

    def _supplement_compact_references(self, source: str) -> None:
        existing = {
            (reference.owner, reference.name, reference.line, reference.access)
            for reference in self.references
        }
        for call in self.calls:
            tail = re.split(r"[.>:]+", str(call.target or "").casefold())[-1]
            if tail != "compact" or not call.line:
                continue
            expression = _php_call_expression_at_line(source, tail, call.line)
            if expression is None:
                continue
            for name in re.findall(r"['\"]([A-Za-z_]\w*)['\"]", expression):
                key = (call.caller, name, call.line, "read")
                if key in existing:
                    continue
                self.references.append(
                    _reference(self.artifact, call.caller, name, call.line, int(call.column or 0), "read")
                )
                existing.add(key)

    def _supplement_include_directives(self, source: str) -> None:
        pattern = re.compile(
            r"\b(include|include_once|require|require_once)\b\s*(?:\(\s*)?(.+?)(?:\s*\))?\s*;",
            flags=re.IGNORECASE | re.DOTALL,
        )
        matches = [match for match in pattern.finditer(source) if _php_offset_is_code(source, match.start())]
        directive_lines = {source.count("\n", 0, match.start()) + 1 for match in matches}
        self.imports = [
            item
            for item in self.imports
            if not (
                item.line in directive_lines
                and str(item.metadata.get("ast_node") if isinstance(item.metadata, dict) else "").casefold()
                in {"include_expression", "include_once_expression", "require_expression", "require_once_expression"}
            )
        ]
        existing = {(item.line, str(item.module or "")) for item in self.imports}
        for match in matches:
            expression = match.group(2).strip()
            target, metadata = _php_include_target(expression)
            if not target:
                continue
            line = source.count("\n", 0, match.start()) + 1
            if (line, target) in existing:
                continue
            form = match.group(1).casefold()
            metadata.update(
                {
                    "language": self.language_key,
                    "profile": self.profile.name,
                    "import_form": form,
                    "path_expression": expression,
                    "is_partial": True,
                }
            )
            self.imports.append(
                _import(
                    self.artifact,
                    target,
                    None,
                    None,
                    line,
                    match.group(0).strip(),
                    metadata=metadata,
                )
            )
            existing.add((line, target))

    def _enrich_view_calls(self, source: str) -> None:
        render_names = {"display", "include_view", "partial", "render", "render_template", "template", "view"}
        for call in self.calls:
            tail = re.split(r"[.>:]+", str(call.target or "").casefold())[-1]
            if tail not in render_names or not call.line:
                continue
            expression = _php_call_expression_at_line(source, tail, call.line)
            if not expression:
                continue
            quoted = re.findall(r"['\"]([^'\"]+)['\"]", expression)
            if quoted:
                call.metadata["template_names"] = [quoted[0]]
            view_variables = set(re.findall(r"['\"]([A-Za-z_]\w*)['\"]\s*=>", expression))
            for compact in re.finditer(r"\bcompact\s*\((.*?)\)", expression, flags=re.IGNORECASE | re.DOTALL):
                view_variables.update(re.findall(r"['\"]([A-Za-z_]\w*)['\"]", compact.group(1)))
            call.metadata["view_variables"] = sorted(view_variables)
            variable_sources = sorted(set(re.findall(r"\$([A-Za-z_]\w*)", expression)))
            if variable_sources:
                call.metadata["view_variable_sources"] = variable_sources


def _php_include_target(expression: str) -> tuple[str | None, dict[str, object]]:
    quoted = re.findall(r"['\"]([^'\"]+)['\"]", expression)
    if not quoted:
        return None, {"dynamic": True}
    unquoted = re.sub(r"['\"][^'\"]*['\"]", "", expression)
    dynamic = bool(re.search(r"\$[A-Za-z_]\w*", unquoted))
    target = "".join(quoted).replace("\\", "/")
    metadata: dict[str, object] = {"dynamic": dynamic}
    lowered = unquoted.casefold()
    if "dirname" in lowered and "__dir__" in lowered:
        metadata["relative_to"] = "source_parent"
    elif "__dir__" in lowered:
        metadata["relative_to"] = "source_dir"
    else:
        metadata["relative_to"] = "source_or_project"
    return target, metadata


def _php_offset_is_code(source: str, offset: int) -> bool:
    in_php = "<?" not in source
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    index = 0
    while index < min(offset, len(source)):
        pair = source[index : index + 2]
        char = source[index]
        if not in_php:
            if pair == "<?":
                in_php = True
                index += 2
                continue
            index += 1
            continue
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if pair == "*/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if pair == "?>":
            in_php = False
            index += 2
        elif pair in {"//", "/*"}:
            line_comment = pair == "//"
            block_comment = pair == "/*"
            index += 2
        elif char == "#":
            line_comment = True
            index += 1
        elif char in {"'", '"'}:
            quote = char
            index += 1
        else:
            index += 1
    return in_php and not (quote or line_comment or block_comment)


def _php_call_expression_at_line(source: str, call_name: str, line: int) -> str | None:
    start_offset = sum(len(item) for item in source.splitlines(keepends=True)[: max(0, line - 1)])
    pattern = re.compile(rf"\b{re.escape(call_name)}\s*\(", flags=re.IGNORECASE)
    match = pattern.search(source, max(0, start_offset - 80))
    if match is None or source.count("\n", 0, match.start()) + 1 > line + 1:
        return None
    opening = source.find("(", match.start())
    depth = 0
    quote = ""
    escaped = False
    for index in range(opening, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : index]
    return None
