"""TSX Tree-sitter extraction."""
from __future__ import annotations

from .typescript import TypeScriptTreeSitterExtractor


class TsxTreeSitterExtractor(TypeScriptTreeSitterExtractor):
    language_key = "tsx"
    tree_sitter_module = "tree_sitter_typescript"
    tree_sitter_function = "language_tsx"
