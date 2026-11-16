"""TypeScript extraction using the shared JavaScript syntax walker."""
from __future__ import annotations

from .javascript import JavaScriptTreeSitterExtractor


class TypeScriptTreeSitterExtractor(JavaScriptTreeSitterExtractor):
    """Use the TypeScript grammar with the common ECMAScript extraction rules."""

    language_key = "typescript"
    tree_sitter_module = "tree_sitter_typescript"
    tree_sitter_function = "language_typescript"
