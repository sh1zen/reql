from __future__ import annotations

import unittest

from memory.code_analysis.surface import analyze_css


class CssSurfaceAnalysisTests(unittest.TestCase):
    def test_extracts_selectors_without_treating_values_as_selectors(self) -> None:
        result = analyze_css('.card, #hero { color: #555; background: url("asset.png"); }')

        self.assertEqual(result.identifiers, {"class:card", "id:hero"})

    def test_finds_declarations_overridden_in_the_same_cascade_context(self) -> None:
        result = analyze_css(".card { color: red; color: blue; }\n.card { margin: 1px; margin: 2px; }")

        self.assertEqual(
            [(item.selector, item.property_name, item.value) for item in result.overridden_declarations],
            [(".card", "color", "red"), (".card", "margin", "1px")],
        )

    def test_important_declaration_is_not_overridden_by_normal_declaration(self) -> None:
        result = analyze_css(".card { color: red !important; color: blue; }")

        self.assertEqual(result.overridden_declarations, [])

    def test_partial_selector_overlap_is_not_reported_as_always_overridden(self) -> None:
        result = analyze_css(".card, .panel { color: red; } .card { color: blue; }")

        self.assertEqual(result.overridden_declarations, [])


if __name__ == "__main__":
    unittest.main()
