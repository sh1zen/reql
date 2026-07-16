"""Deterministic CSS surface analysis used by the project graph compiler."""
from __future__ import annotations

from dataclasses import dataclass, field
import re


@dataclass(frozen=True, slots=True)
class CssDeclaration:
    selector: str
    property_name: str
    value: str
    line: int
    important: bool = False
    context: tuple[str, ...] = ()


@dataclass(slots=True)
class CssAnalysis:
    identifiers: set[str] = field(default_factory=set)
    classes: dict[str, list[int]] = field(default_factory=dict)
    selectors: dict[str, list[int]] = field(default_factory=dict)
    overridden_declarations: list[CssDeclaration] = field(default_factory=list)


_NESTED_RULE_AT_RULES = ("@container", "@document", "@layer", "@media", "@scope", "@supports")


def analyze_css(text: str) -> CssAnalysis:
    """Extract selectors and declarations with high-confidence cascade findings.

    The parser is intentionally dependency-free, but it still respects strings,
    comments, parentheses, brackets, nested conditional rules, and ``!important``.
    A declaration is reported as overridden only when a later declaration has the
    same selector and at-rule context (or repeats the property in the same block).
    """

    cleaned = _strip_comments_preserving_lines(text)
    analysis = CssAnalysis()
    latest: dict[tuple[tuple[str, ...], str, str], CssDeclaration] = {}
    _parse_rule_region(cleaned, 1, (), analysis, latest)
    return analysis


def _parse_rule_region(
    text: str,
    base_line: int,
    context: tuple[str, ...],
    analysis: CssAnalysis,
    latest: dict[tuple[tuple[str, ...], str, str], CssDeclaration],
) -> None:
    cursor = 0
    while cursor < len(text):
        opening = _find_top_level(text, "{", cursor)
        if opening < 0:
            return
        closing = _matching_brace(text, opening)
        if closing < 0:
            return
        prelude_start = _statement_start(text, opening)
        raw_prelude = text[prelude_start:opening].strip()
        block = text[opening + 1 : closing]
        normalized_prelude = _normalize_space(raw_prelude)
        lowered = normalized_prelude.casefold()
        if lowered.startswith(_NESTED_RULE_AT_RULES):
            _parse_rule_region(
                block,
                base_line + text.count("\n", 0, opening + 1),
                (*context, normalized_prelude),
                analysis,
                latest,
            )
        elif raw_prelude and not raw_prelude.startswith("@"):
            selectors = _split_top_level(raw_prelude, ",")
            declarations = _parse_declarations(block, base_line + text.count("\n", 0, opening + 1))
            normalized_selectors: list[str] = []
            for selector in selectors:
                normalized_selector = _normalize_space(selector)
                if not normalized_selector:
                    continue
                normalized_selectors.append(normalized_selector)
                selector_line = base_line + text.count("\n", 0, prelude_start)
                analysis.selectors.setdefault(normalized_selector, []).append(selector_line)
                for class_name in re.findall(r"\.([A-Za-z_][\w-]*)", selector):
                    analysis.identifiers.add(f"class:{class_name.casefold()}")
                    analysis.classes.setdefault(class_name.casefold(), []).append(selector_line)
                for element_id in re.findall(r"#([A-Za-z_][\w-]*)", selector):
                    analysis.identifiers.add(f"id:{element_id.casefold()}")
            rule_selector = ", ".join(normalized_selectors)
            for declaration in declarations:
                item = CssDeclaration(
                    selector=rule_selector,
                    property_name=declaration.property_name,
                    value=declaration.value,
                    line=declaration.line,
                    important=declaration.important,
                    context=context,
                )
                key = (context, rule_selector, declaration.property_name)
                previous = latest.get(key)
                if previous is not None and (item.important or not previous.important):
                    if previous not in analysis.overridden_declarations:
                        analysis.overridden_declarations.append(previous)
                latest[key] = item
        cursor = closing + 1


def _parse_declarations(block: str, base_line: int) -> list[CssDeclaration]:
    values: list[CssDeclaration] = []
    for start, raw in _split_top_level_with_offsets(block, ";"):
        colon = _find_top_level(raw, ":", 0)
        if colon <= 0:
            continue
        property_name = raw[:colon].strip()
        value = raw[colon + 1 :].strip()
        if not property_name or not value or not re.match(r"^(?:--[\w-]+|[A-Za-z-]+)$", property_name):
            continue
        important = bool(re.search(r"!\s*important\s*$", value, flags=re.IGNORECASE))
        normalized_property = property_name if property_name.startswith("--") else property_name.casefold()
        values.append(
            CssDeclaration(
                selector="",
                property_name=normalized_property,
                value=value,
                line=base_line + block.count("\n", 0, start),
                important=important,
            )
        )
    return values


def _strip_comments_preserving_lines(text: str) -> str:
    return re.sub(r"/\*.*?\*/", lambda match: "\n" * match.group(0).count("\n"), text, flags=re.DOTALL)


def _find_top_level(text: str, needle: str, start: int) -> int:
    quote = ""
    escaped = False
    parens = 0
    brackets = 0
    for index in range(start, len(text)):
        char = text[index]
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
            parens += 1
        elif char == ")":
            parens = max(0, parens - 1)
        elif char == "[":
            brackets += 1
        elif char == "]":
            brackets = max(0, brackets - 1)
        elif char == needle and parens == 0 and brackets == 0:
            return index
    return -1


def _matching_brace(text: str, opening: int) -> int:
    depth = 0
    quote = ""
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
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
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _statement_start(text: str, opening: int) -> int:
    boundary = max(text.rfind("}", 0, opening), text.rfind(";", 0, opening))
    return boundary + 1


def _split_top_level(text: str, delimiter: str) -> list[str]:
    return [value for _, value in _split_top_level_with_offsets(text, delimiter)]


def _split_top_level_with_offsets(text: str, delimiter: str) -> list[tuple[int, str]]:
    values: list[tuple[int, str]] = []
    start = 0
    quote = ""
    escaped = False
    parens = brackets = braces = 0
    for index, char in enumerate(text):
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
            parens += 1
        elif char == ")":
            parens = max(0, parens - 1)
        elif char == "[":
            brackets += 1
        elif char == "]":
            brackets = max(0, brackets - 1)
        elif char == "{":
            braces += 1
        elif char == "}":
            braces = max(0, braces - 1)
        elif char == delimiter and parens == 0 and brackets == 0 and braces == 0:
            values.append((start, text[start:index]))
            start = index + 1
    values.append((start, text[start:]))
    return values


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())
